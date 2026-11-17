import os
import re
import json
import time
import secrets
import base64
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

import settle
import sheets
import shopping

# -------------------------------------------------
#  載入環境變數 (.env)
# -------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_IDS_RAW = os.getenv("ALLOWED_CHANNEL_IDS", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")  # Google 試算表網址裡 /d/ 後面那一段
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # OAuth 憑證檔：client_id / client_secret / refresh_token

if not DISCORD_TOKEN:
    raise RuntimeError("缺少必要的環境變數：DISCORD_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("缺少必要的環境變數：GROQ_API_KEY")

# 將 ALLOWED_CHANNEL_IDS 解析成一組 int set（空字串代表不限制）
ALLOWED_CHANNEL_IDS = set()
for part in ALLOWED_CHANNEL_IDS_RAW.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        ALLOWED_CHANNEL_IDS.add(int(part))
    except ValueError:
        print(f"[WARN] 無法解析頻道 ID：{part!r}（已略過）")

# 抓第一個網址用的 regex（不吃空白、CJK 字元與全形標點——淘寶/京東分享文常把網址和中文擠在一起）
URL_REGEX = re.compile(r"https?://[^\s　-〿一-鿿＀-￯「-』]+")

# 網址結尾常見的標點（regex 可能把它們一起吃進來，要剝掉）
URL_TRAILING_PUNCT = ".,;:!?)]>\"'"

# Discord 會把貼上的長連結顯示成 [顯示文字](真實網址)。淘寶那種帶 utparam=%7B...%7D
# 的網址貼進來就會變成這樣，括號裡才是真正能點的網址。
MD_LINK_RE = re.compile(r"\]\((https?://[^\s()]+)\)")


def extract_first_url(text: str) -> str | None:
    """從文字中抓出第一個網址，並剝掉結尾誤吃的標點符號。

    刻意不靠 LLM 抽網址：LLM 會把淘寶的長網址截斷（實測 507 字被剪成 256 字），
    存進表格就變成點不開的連結，而且每次截的位置不一定一樣。
    """
    # markdown 包裝優先，括號裡才是真正可以點的網址
    md = MD_LINK_RE.search(text)
    if md:
        return md.group(1).rstrip(URL_TRAILING_PUNCT)

    match = URL_REGEX.search(text)
    if not match:
        return None
    # 保險：真的吃到 markdown 的分隔時只留前半段
    return match.group(0).split("](")[0].rstrip(URL_TRAILING_PUNCT)


def safe_quantity(value) -> int:
    """把 LLM 回傳的數量安全轉成正整數，失敗一律回 1。"""
    try:
        qty = int(float(value))
    except (TypeError, ValueError):
        return 1
    return qty if qty > 0 else 1


# -------------------------------------------------
#  Google 試算表
# -------------------------------------------------
SHEET_LOCK = asyncio.Lock()  # 表格的每個操作都是「先讀再寫」，一次只做一個才不會互相踩到
_store = None


def get_store() -> sheets.Store:
    global _store
    if _store is None:
        if not SHEET_ID or not GOOGLE_CREDENTIALS:
            raise RuntimeError("缺少必要的環境變數：SHEET_ID 或 GOOGLE_CREDENTIALS")
        _store = sheets.Store(sheets.Sheets(SHEET_ID, json.loads(Path(GOOGLE_CREDENTIALS).read_text())))
    return _store


class Reply:
    """表格操作的結果。status_code 沿用 HTTP 的習慣：200 成功、404 找不到、409 有衝突。"""

    def __init__(self, status: int, body: dict):
        self.status_code, self.ok, self.body = status, status < 400, body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self) -> dict:
        return self.body


def _choice(n: int, row: dict) -> dict:
    return {"n": n, "id": row["id"], "sheet": row["tab"], "itemName": row["name"],
            "spec": row["spec"], "qty": row["qty"], "url": row["url"]}


def sheet_action(store: sheets.Store, p: dict) -> Reply:
    """bot 對表格的所有操作都走這裡（同步，在 thread 裡跑）。"""
    action = p.get("action")
    if action in ("list", "all_rows"):
        rows = store.rows()
        if action == "all_rows":
            return Reply(200, {"rows": rows})
        open_rows = [r for r in rows if not r["batch"]]
        return Reply(200, {"status": "ok", "rows": open_rows, "settled": len(rows) - len(open_rows)})

    if action == "add":
        mine = [r for r in store.rows() if r["buyer"] == p["senderName"]]
        dups = shopping.find_duplicates(p["url"], mine)
        if dups and not p.get("force"):
            return Reply(409, {"status": "duplicate", "rows": [_choice(i, r) for i, r in enumerate(dups, 1)]})
        tab = shopping.platform(p.get("shareText", ""), p["url"])
        row = store.add(tab, {"name": p["itemName"], "spec": p.get("modelSpec", ""), "url": p["url"],
                              "qty": p.get("quantity", 1), "buyer": p["senderName"]})
        return Reply(200, {"status": "added", "id": row["id"], "sheet": tab})

    if action == "delete":
        mine = [r for r in store.rows() if r["buyer"] == p["senderName"]]
        if p.get("rowId"):  # 使用者從選項裡選好的那一筆
            hits = [r for r in mine if r["id"] == p["rowId"]]
            status = "one" if hits else "changed"
        else:
            status, hits = shopping.find_delete_target(p.get("url", ""), p.get("itemName", ""), mine)
        if status == "one":
            gone = store.delete(hits[0]["id"], expected_buyer=p["senderName"], expected=hits[0])
            if gone is not None:
                return Reply(200, {"status": "deleted", "itemName": gone["name"]})
            status = "changed"
        if status == "choose":
            return Reply(404, {"status": "choose", "choices": [_choice(i, r) for i, r in enumerate(hits, 1)]})
        return Reply(404, {"status": "changed" if status == "changed" else "not_found"})

    if action == "settle":
        problems = store.settle(p["batch"], p["rows"])
        if problems:
            return Reply(409, {"status": "conflict", "problems": problems})
        return Reply(200, {"status": "settled", "batch": p["batch"], "count": len(p["rows"])})

    if action == "validate_settle":
        problems = store.validate_settlement(p["batch"], p["rows"])
        return Reply(409 if problems else 200, {"status": "conflict" if problems else "ok", "problems": problems})

    if action == "unsettle":
        return Reply(200, {"status": "ok", "count": store.unsettle(p["batch"], p["ids"])})

    if action == "ledger":
        store.save_settlement(p["batch"], p["state"], p["block"], release_ids=p.get("released_ids", ()))
        return Reply(200, {"status": "ok"})

    if action == "states":
        return Reply(200, {"status": "ok", "states": store.read_states()})

    if action == "layout":
        return Reply(200, {"status": "ok", "done": store.ensure_layout()})

    return Reply(400, {"status": "error", "message": f"不認得的 action：{action}"})


async def sheet_call(payload: dict) -> Reply:
    async with SHEET_LOCK:
        return await asyncio.to_thread(sheet_action, get_store(), payload)


def sheet_failure_reply(verb: str, done: str, err: Exception) -> str:
    """回應逾時不等於失敗：請求可能已經送到 Google，這筆也許已經寫進去了。"""
    if isinstance(err, requests.exceptions.Timeout):
        return (
            f"{verb}請求已送出，但 Google 表格太久沒回應 ⏱️\n"
            f"這筆**可能已經{done}**，請先看一下清單，沒有再重送一次。"
        )
    return f"{verb}時我撞牆了 QQ 請幫我看一下 log"

# -------------------------------------------------
#  初始化 Groq（OpenAI 相容 API）
# -------------------------------------------------
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)
# Llama 3.3 70B 已於 2026-08-16 對免費／Developer 方案下線（打了會回 404）。
# 官方替代：openai/gpt-oss-120b（正式版）或 qwen/qwen3.6-27b（預覽，中文較強）
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MAX_RETRIES = 3

# 結算時讀截圖用的視覺模型。有設 LOCAL_VISION_URL 就先用本地模型（截圖不離開自己的機器），
# 太慢、失敗或讀不完整時才退回 Groq 的 VISION_MODEL。
VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
LOCAL_VISION_URL = os.getenv("LOCAL_VISION_URL")  # OpenAI 相容的服務，例如 Ollama：http://host:11434/v1
LOCAL_VISION_MODEL = os.getenv("LOCAL_VISION_MODEL", "qwen3-vl:8b-instruct")
# 正常一張約 4 秒，模型剛載入約 25 秒。顯示卡記憶體被別的程式擠滿時一張要好幾分鐘，那就不等了
LOCAL_VISION_TIMEOUT = float(os.getenv("LOCAL_VISION_TIMEOUT", "45"))
LOCAL_VISION_COOLDOWN = 300  # 本地連不上或逾時後，這段時間直接用 Groq，不讓每張圖都等到逾時
local_vision_client = (
    AsyncOpenAI(base_url=LOCAL_VISION_URL, api_key="local", max_retries=0) if LOCAL_VISION_URL else None
)
local_vision_down_until = 0.0
STATE_DIR = os.getenv("STATE_DIR")  # 有設才會把結算紀錄存檔，重啟後還能認領

SYSTEM_PROMPT = (
    "你是一個購物清單助手。使用者會在 Discord 傳購物相關訊息，你要判斷意圖並提取資訊。\n"
    "請嚴格回傳以下 JSON 格式：\n"
    '{"action": "add", "itemName": "商品名稱", "quantity": 1, "modelSpec": "型號規格", "url": "網址"}\n\n'
    "規則：\n"
    '- action：判斷使用者意圖，只能是 "add"、"delete" 或 "other"。\n'
    '  - "add"：想購買/新增商品（例如：買 兩台白色的小米風扇 https://...）\n'
    '  - "delete"：想刪除/取消已加入清單的商品（例如：刪除電動牙刷、幫我把風扇刪掉、電動牙刷不用買了）\n'
    '  - "other"：跟購物清單無關，或無法判斷。\n'
    "- itemName：商品名稱。add 時是要購買的商品；delete 時是要刪除的商品。"
    "如果訊息包含「...」格式的商品名，優先使用。沒有就留空字串。\n"
    "- quantity：數量，必須是整數。如果使用者用中文數字（如 兩台、三個），請轉換為阿拉伯數字。預設為 1。\n"
    "- modelSpec：型號、規格、顏色等描述（如 白色、256GB）。如果沒有就留空字串。\n"
    "- url：訊息中的網址（https:// 開頭）。如果沒有就留空字串，要注意不該有空格。\n"
    "- 如果資訊缺失請留空字串，數量預設為 1。\n"
    "- 只輸出 JSON，不要有其他解釋文字。"
)

VALID_ACTIONS = {"add", "delete", "other"}

# 帶有「刪除意圖」的關鍵字：訊息不是「買」開頭時，符合這個才會送 LLM 判斷
DELETE_INTENT_RE = re.compile(r"刪除|刪掉|移除|取消|不用買|不想買|不買了|別買|退掉")


async def parse_with_llm(message_content: str) -> dict | None:
    """呼叫 Groq API 解析購物訊息，含 429 自動重試，回傳 dict 或 None。"""
    raw_text = ""

    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        try:
            response = await groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                # gpt-oss 是 reasoning model，會先產生推理 token 再輸出 JSON。
                # 上限太小的話推理就把額度用完，JSON 還沒生成完 Groq 就回
                # 400 json_validate_failed（512 遇到長分享文必炸，實測要 ~700）。
                # 上限只是安全網，不影響實際用量。
                max_tokens=2048,
                # 刻意不設 reasoning_effort="low"：省 6 成 token，但遇到網址緊貼
                # 中文的長分享文會抽不出商品名（實測 3 次有 2 次回空字串），
                # 品質比那點額度重要。
            )
            raw_text = response.choices[0].message.content.strip()
            parsed = json.loads(raw_text)

            action = str(parsed.get("action", "")).lower()
            if action not in VALID_ACTIONS:
                action = "other"

            return {
                "action": action,
                "itemName": str(parsed.get("itemName", "")),
                "quantity": safe_quantity(parsed.get("quantity")),
                "modelSpec": str(parsed.get("modelSpec", "")),
                "url": str(parsed.get("url", "")),
            }

        except json.JSONDecodeError as e:
            print(f"[ERROR] LLM 回傳的不是有效 JSON: {e}")
            print(f"[ERROR] 原始回傳: {raw_text!r}")
            return None

        except RateLimitError:
            if attempt < GROQ_MAX_RETRIES:
                wait_sec = min(10 * attempt, 30)
                print(f"[WARN] Groq 429 rate limit，第 {attempt} 次重試，等待 {wait_sec} 秒...")
                await asyncio.sleep(wait_sec)
                continue
            print(f"[ERROR] Groq rate limit，重試 {GROQ_MAX_RETRIES} 次後放棄")
            return None

        except Exception as e:
            print(f"[ERROR] 呼叫 Groq 時發生錯誤 (attempt {attempt}/{GROQ_MAX_RETRIES}): {e}")
            return None

    return None


# -------------------------------------------------
#  Discord Bot 初始化
# -------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # 一定要開，不然讀不到訊息內容

bot = commands.Bot(command_prefix="!", intents=intents)


# -------------------------------------------------
#  按鈕與選單
# -------------------------------------------------
# 按下去之後要做什麼，全部寫在識別碼（custom_id）裡，由 on_component 統一處理。
# 這樣 bot 重啟後，舊訊息上的按鈕照樣有效。
class SLButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sl:.+"):
    def __init__(self, label: str, custom_id: str, style=discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, custom_id=custom_id, style=style))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(item.label, item.custom_id, item.style)

    async def callback(self, interaction):
        await on_component(interaction, self.item.custom_id, [])


class SLSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"sls:.+"):
    def __init__(self, custom_id: str, options: list, placeholder: str = "", max_values: int = 1):
        super().__init__(discord.ui.Select(custom_id=custom_id, options=options, placeholder=placeholder or None,
                                           min_values=1, max_values=max_values))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(item.custom_id, item.options, item.placeholder or "", item.max_values)

    async def callback(self, interaction):
        await on_component(interaction, self.item.custom_id, list(self.item.values))


class SLUserSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=r"slu:.+"):
    def __init__(self, custom_id: str, placeholder: str = ""):
        super().__init__(discord.ui.UserSelect(custom_id=custom_id, placeholder=placeholder or None, min_values=1, max_values=1))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(item.custom_id, item.placeholder or "")

    async def callback(self, interaction):
        await on_component(interaction, self.item.custom_id, list(self.item.values))


bot.add_dynamic_items(SLButton, SLSelect, SLUserSelect)
GREEN, RED, BLUE, GREY = (discord.ButtonStyle.success, discord.ButtonStyle.danger,
                          discord.ButtonStyle.primary, discord.ButtonStyle.secondary)


def view_of(*items) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def ephemeral(interaction, text: str, view=None):
    """只有按的人看得到的回覆。"""
    kw = {"view": view} if view is not None else {}
    await interaction.response.send_message(text, ephemeral=True, **kw)


async def safe_react(message: discord.Message, emoji: str, remove: bool = False):
    """加/移除反應失敗（缺權限、網路暫時不通…）時不要讓整個訊息處理中斷。"""
    try:
        if remove:
            await message.remove_reaction(emoji, bot.user)
        else:
            await message.add_reaction(emoji)
    except Exception as e:
        print(f"[WARN] 無法{'移除' if remove else '加上'}反應 {emoji}: {e}")


# (頻道, 使用者) → 還沒選的刪除選項。只在記憶體，重啟就消失。
DELETE_CHOICES: dict[tuple[int, int], dict] = {}
DELETE_CHOICE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
DELETE_CHOICE_SECONDS = 600
# 「刪除 2」「第2個」「2」；純數字只在回覆選項那則訊息時算數
DELETE_PICK_RE = re.compile(r"^(?:(?:刪除|刪掉|移除|選)\s*)?(?:第\s*)?#?\s*(\d{1,2})\s*(?:個)?$")


def delete_choice_lock(key: tuple[int, int]) -> asyncio.Lock:
    return DELETE_CHOICE_LOCKS.setdefault(key, asyncio.Lock())


def delete_pick_number(text: str, message: discord.Message) -> int | None:
    """有待選的刪除時，認出選編號。沒有待選就回 None，讓原本的刪除流程繼續。"""
    pending = DELETE_CHOICES.get((message.channel.id, message.author.id))
    if not pending:
        return None
    matched = DELETE_PICK_RE.fullmatch(text.strip())
    if not matched:
        return None
    raw = text.strip()
    replied = bool(message.reference and message.reference.message_id == pending.get("prompt_id"))
    explicit = bool(re.match(r"^(?:刪除|刪掉|移除|選|第)", raw))
    if replied or explicit:
        return int(matched.group(1))
    return None


def _drop_delete_choice(key: tuple[int, int], choice_id: str | None):
    pending = DELETE_CHOICES.get(key)
    if pending and choice_id is not None and pending["id"] == choice_id:
        DELETE_CHOICES.pop(key, None)


async def offer_delete_choices(message: discord.Message, choices: list[dict]):
    """多筆對得上時列出來讓他選，每輪使用獨立識別碼並取代舊選項。"""
    key = (message.channel.id, message.author.id)
    token = secrets.token_hex(8)
    lines = []
    for choice in choices:
        extra = f"（{choice['spec']}）" if choice.get("spec") else ""
        qty = choice.get("qty")
        qty_text = f" ×{qty}" if qty not in ("", None) else ""
        lines.append(f"{choice['n']}. 「{choice['itemName']}」{extra}{qty_text}")
    view = view_of(*[SLButton(f"刪除 {c['n']}", f"sl:del:{token}:{c['n']}", RED) for c in choices[:20]],
                   SLButton("都不要刪", f"sl:del:{token}:0", GREY))
    prompt = await message.reply(
        "你有好幾筆對得上，我先不刪。按下面的按鈕選一筆（也可以打「刪除 2」）：\n" + "\n".join(lines), view=view
    )
    async with delete_choice_lock(key):
        DELETE_CHOICES[key] = {
            "id": token,
            "expires": time.monotonic() + DELETE_CHOICE_SECONDS,
            "choices": choices,
            "prompt_id": prompt.id,
        }


async def confirm_delete_choice(message, number: int, expected_choice_id=None):
    key = (message.channel.id, message.author.id)
    async with delete_choice_lock(key):
        pending = DELETE_CHOICES.get(key)
        if pending and expected_choice_id is not None and str(pending["id"]) != str(expected_choice_id):
            await message.reply("這張刪除選項已經被新的選項取代，請使用最新的選項。")
            return
        if not pending or pending["expires"] <= time.monotonic():
            DELETE_CHOICES.pop(key, None)
            await message.reply("這次的刪除選項已經過期，請重新說一次要刪什麼。")
            return
        choices = pending["choices"]
        if number == 0:
            DELETE_CHOICES.pop(key, None)
            await message.reply("好，什麼都沒刪。")
            return
        if not 1 <= number <= len(choices):
            await message.reply(f"請回 1 到 {len(choices)} 的編號。")
            return
        choice_id, row_id = pending["id"], choices[number - 1]["id"]
    await send_delete(message, "", "", message.content, row_id=row_id, choice_id=choice_id)


async def send_delete(message, url: str, item_name: str, full_text: str, row_id: str = "", choice_id: int | None = None):
    """刪除並回覆結果。可用網址、商品名稱，或使用者從選項裡選好的列 ID。"""
    key = (message.channel.id, message.author.id)
    if choice_id is None:
        pending = DELETE_CHOICES.get(key)
        choice_id = pending["id"] if pending else None
    payload = {"action": "delete", "url": url, "itemName": item_name, "fullText": full_text,
               "senderId": str(message.author.id), "senderName": message.author.name}
    if row_id:
        payload["rowId"] = row_id

    try:
        resp = await sheet_call(payload)
        body = resp.json() or {}
        print("DELETE:", resp.status_code, resp.text[:200])

        if resp.status_code == 404 and body.get("status") == "choose" and body.get("choices"):
            await offer_delete_choices(message, body["choices"])
            return
        if resp.status_code == 404 and body.get("status") == "changed":
            _drop_delete_choice(key, choice_id)
            await message.reply("你選的那一筆已經不在清單裡了（可能剛被刪掉），所以**沒有刪任何東西**。\n還要刪的話請重新說一次。")
            return

        if resp.status_code == 200:
            _drop_delete_choice(key, choice_id)
            deleted_name = str(body.get("itemName", ""))
            if deleted_name:
                await message.reply(f"已把「{deleted_name}」從購物清單刪掉 🗑️")
            else:
                await message.reply("已刪除你加進清單的那筆商品 🗑️")
        elif resp.status_code in (403, 404):
            _drop_delete_choice(key, choice_id)
            await message.reply(
                "在你加入的清單裡找不到這筆商品 🙅\n"
                "（只能刪自己加的，名稱也要對得上喔）"
            )
        else:
            await message.reply("刪除失敗 QQ 表格那邊出錯了，請稍後再試。")
    except Exception as e:
        print("Error deleting:", repr(e))
        await message.reply(sheet_failure_reply("刪除", "從表格刪掉", e))


# -------------------------------------------------
#  新增：同一件商品已經登記過的話先問一下
# -------------------------------------------------
ADD_PENDING: dict[str, dict] = {}  # 序號 → 等使用者決定要不要重複登記的新增
ADD_PENDING_SECONDS = 600


def added_text(payload: dict) -> str:
    parts = [f"{payload['quantity']} 件"] + ([payload["modelSpec"]] if payload["modelSpec"] else []) + [f"「{payload['itemName']}」"]
    return f"已把 {' '.join(parts)} 加入購物清單 🧾"


async def send_add(message, payload: dict):
    """寫進清單並回覆。同一個人已經登記過同一件商品（還沒結算）時先問要不要再加一筆。"""
    try:
        resp = await sheet_call(payload)
        print("ADD:", resp.status_code, resp.text[:200])
        if resp.status_code == 409 and resp.json().get("status") == "duplicate":
            token = secrets.token_hex(8)
            had = "\n".join(f"・「{r['itemName']}」{'（' + r['spec'] + '）' if r['spec'] else ''} ×{r['qty']}" for r in resp.json()["rows"])
            prompt = await message.reply(
                f"這件商品你已經登記過了：\n{had}\n還要再加一筆嗎？（想改數量的話，請先刪掉舊的再重新登記）",
                view=view_of(SLButton("再加一筆", f"sl:dup:{token}:y", BLUE), SLButton("不用了", f"sl:dup:{token}:n", GREY)),
            )
            ADD_PENDING[token] = {"payload": payload, "user_id": message.author.id,
                                  "channel_id": message.channel.id, "prompt_id": prompt.id,
                                  "expires": time.monotonic() + ADD_PENDING_SECONDS}
        elif resp.ok:
            await message.reply(added_text(payload))
        else:
            await message.reply("新增失敗 QQ 表格那邊出錯了，請稍後再試。")
    except Exception as e:
        print("Error adding:", repr(e))
        await message.reply(sheet_failure_reply("新增", "寫進表格", e))


# -------------------------------------------------
#  結算：下單截圖 → 每人應付金額
# -------------------------------------------------
SETTLE_RE = re.compile(r"^(結算|结算)")
CLAIM_RE = re.compile(r"^(認領|认领)\s*#?\s*\d")
MAX_CLAIMS = 30
SETTLE_CONFIRM_SECONDS = 600
CLOSE_EMOJI = "🏁"
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 3_000_000  # Groq 的 base64 圖片上限是 4MB，編碼後會膨脹三分之一
MATCH_CHUNK = 6  # 一次請 LLM 比對幾個商品；太多會超過每分鐘的 token 額度
LLM_RETRY_WAIT = 15
SETTLEMENTS: dict[str, dict] = {}  # batch → 已公布的結算，照建立順序
PENDING: dict[tuple[int, int], dict] = {}  # (頻道, 傳圖的人) → 還沒按 ✅ 的草稿；追加的截圖會併進來
SETTLEMENT_LOCKS: dict[str, asyncio.Lock] = {}
CHANNEL_LOCKS: dict[int, asyncio.Lock] = {}


def draft_view() -> discord.ui.View:
    return view_of(SLButton("✅ 確認", "sl:draft:ok", GREEN), SLButton("❌ 取消", "sl:draft:no", GREY))


def settlement_view(state: dict) -> discord.ui.View:
    batch = state["batch"]
    claim = [SLButton("🙋 認領我的商品", f"sl:claim:{batch}", BLUE)] if settle.has_unclassified(state) else []
    return view_of(*claim, SLButton("✏️ 改歸屬", f"sl:re:{batch}", GREY), SLButton("🏁 結束認領", f"sl:close:{batch}", RED))


def can_manage(state: dict, user_id: int) -> bool:
    """下單人和代傳的人可以確認、改歸屬、結束認領。"""
    return user_id in (state["payer_id"], state.get("poster_id", state["payer_id"]))


class SettleError(Exception):
    """可以直接回給使用者看的錯誤。"""


def settlement_lock(batch: str) -> asyncio.Lock:
    return SETTLEMENT_LOCKS.setdefault(batch, asyncio.Lock())


def replied_settlement(message: discord.Message) -> dict | None:
    message_id = message.reference.message_id if message.reference else None
    return next((s for s in SETTLEMENTS.values()
                 if s["channel_id"] == message.channel.id and message_id in s["message_ids"]), None)


def named_user(message: discord.Message):
    """內文裡 @ 的第一個真人。回覆別人時 Discord 會把對方也放進 mentions，所以要以內文為準。"""
    people = {u.id: u for u in message.mentions if not u.bot}
    return next((people[i] for i in message.raw_mentions if i in people), None)


def load_settlements():
    if STATE_DIR and (Path(STATE_DIR) / "settlements.json").exists():
        try:
            SETTLEMENTS.update(json.loads((Path(STATE_DIR) / "settlements.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as error:
            print("[WARN] 本機結算快取讀取失敗，啟動時改從表格還原:", repr(error))


LEDGER_SYNC = True
LEDGER_DELAY = 2.0  # 連續幾個動作（一次認領好幾件）併成一次寫入
LEDGER_RETRY_SECONDS = 5
_ledger_synced: dict[str, str] = {}  # batch → 上次寫進表格時的內容
_ledger_uncertain: dict[str, dict] = {}  # batch → 寫入結果待核對的請求及前一版雲端狀態
_ledger_task = None


def _snapshot(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, sort_keys=True)


def schedule_ledger_sync():
    global _ledger_task
    if not LEDGER_SYNC or not _sheet_ready:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _ledger_task is None or _ledger_task.done():
        _ledger_task = loop.create_task(sync_ledger())


async def sync_ledger(delay: float | None = None):
    """把有變動的結算寫進表格的「結算紀錄」分頁（給人看）和隱藏的狀態分頁（主機壞了可以還原）。

    寫不進去只記 log，不影響 Discord 上的操作；下次有變動時會再試。
    """
    if not LEDGER_SYNC:
        return
    await asyncio.sleep(LEDGER_DELAY if delay is None else delay)
    retry_wait = LEDGER_RETRY_SECONDS
    while True:
        dirty = [(b, s) for b, s in list(SETTLEMENTS.items())
                 if b in _ledger_uncertain or _ledger_synced.get(b) != _snapshot(s)]
        if not dirty:
            return
        for batch, state in dirty:
            try:
                async with settlement_lock(batch):
                    if batch not in _ledger_uncertain and _ledger_synced.get(batch) == _snapshot(state):
                        continue
                    proposed = deepcopy(state)
                    await persist_settlement(proposed)
                    state["_version"] = proposed["_version"]
                    state["_updated_at"] = proposed["_updated_at"]
                    save_settlements()
            except Exception as e:
                print(f"[WARN] 結算 {batch} 寫不進表格的結算紀錄:", repr(e))
                await asyncio.sleep(retry_wait)
                retry_wait = min(60, retry_wait * 2)
                break


async def reconcile_settlement(proposed: dict) -> bool:
    """先核對前次未確認的寫入，回傳這次核准紀錄是否已在雲端完成。"""
    batch = proposed["batch"]
    uncertain = _ledger_uncertain.get(batch)
    if uncertain is None:
        return False
    response = await sheet_call({"action": "states"})
    if not response.ok:
        raise sheets.SheetsError("前次結算更新的寫入結果仍待確認，請稍後重試")
    canonical = response.json()["states"].get(batch)
    if canonical is None or canonical == uncertain["previous"]:
        _ledger_uncertain.pop(batch)
        return False
    if not isinstance(canonical, dict):
        raise sheets.SheetsError("雲端結算狀態無法確認，請稍後重試")
    desired, restored = deepcopy(proposed), deepcopy(canonical)
    for key in ("_version", "_updated_at"):
        desired.pop(key, None)
        restored.pop(key, None)
    resume_journal = (canonical == uncertain["state"] and desired == restored
                      and any(key in canonical for key in ("commit_attempt", "append_attempt")))
    current = SETTLEMENTS.get(batch)
    if current is not None:
        current.clear()
        current.update(deepcopy(canonical))
    proposed.clear()
    proposed.update(deepcopy(canonical))
    _ledger_uncertain.pop(batch)
    _ledger_synced[batch] = _snapshot(canonical)
    save_settlements()
    if resume_journal:
        return True
    raise sheets.SheetsError("已從雲端確認前次結算更新，請依最新歸屬重試這次操作")


async def persist_settlement(proposed: dict, released_ids=()):
    """先將帳本、完整狀態與配對清理一起寫入，再由呼叫端提交記憶體變更。"""
    if not LEDGER_SYNC and not released_ids:
        return
    if await reconcile_settlement(proposed):
        return
    snapshot = deepcopy(proposed)
    current = SETTLEMENTS.get(proposed["batch"], {})
    snapshot["_version"] = max(int(current.get("_version", 0)), int(snapshot.get("_version", 0))) + 1
    snapshot["_updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = {"action": "ledger", "batch": snapshot["batch"], "state": snapshot,
               "block": settle.ledger_block(snapshot, datetime.now().strftime("%Y-%m-%d %H:%M"))}
    if released_ids:
        payload["released_ids"] = list(dict.fromkeys(released_ids))
    try:
        response = await sheet_call(payload)
        if not response.ok:
            raise sheets.SheetsError("結算紀錄寫入失敗")
    except Exception:
        confirmed = False
        try:
            response = await sheet_call({"action": "states"})
            confirmed = response.ok and response.json().get("states", {}).get(snapshot["batch"]) == snapshot
        except Exception:
            pass
        if not confirmed:
            _ledger_uncertain[snapshot["batch"]] = {
                "state": snapshot,
                "previous": json.loads(_ledger_synced[snapshot["batch"]])
                if snapshot["batch"] in _ledger_synced else deepcopy(current),
            }
            raise
    proposed.update(snapshot)
    _ledger_synced[proposed["batch"]] = _snapshot(proposed)


def save_settlements():
    schedule_ledger_sync()
    if not STATE_DIR:
        return True
    try:
        tmp = Path(STATE_DIR) / "settlements.json.tmp"
        tmp.write_text(json.dumps(SETTLEMENTS, ensure_ascii=False), encoding="utf-8")
        tmp.replace(Path(STATE_DIR) / "settlements.json")
        return True
    except OSError as e:
        print("[WARN] 本機結算快取存檔失敗:", e)
        return False


async def llm_json(client, **kwargs) -> dict:
    """呼叫 LLM 並解析 JSON 回應；遇到 429 等一下再試。"""
    for attempt in range(1, 7):
        try:
            response = await client.chat.completions.create(
                temperature=0, response_format={"type": "json_object"}, **kwargs
            )
            return json.loads(response.choices[0].message.content)
        except RateLimitError:
            if attempt == 6:
                raise
            print(f"[WARN] LLM 429，第 {attempt} 次重試，等待 {LLM_RETRY_WAIT} 秒...")
            await asyncio.sleep(LLM_RETRY_WAIT)


def _seen(shot: dict) -> tuple:
    """讀到的商品多的優先；一樣多就留「看到的東西」多的那次，讀不完整的件數才不會被另一次蓋掉而默默不見。"""
    return len(shot["items"]), len(shot["items"]) + shot["dropped"]


async def _read_with(client, model: str, url: str, timeout: float | None = None) -> dict:
    """用指定的模型讀一張截圖。回傳讀不完整或不是 JSON 時重讀一次，取比較完整的那次。"""
    best = None
    for _ in range(2):
        call = llm_json(
            client,
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": settle.EXTRACT_PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        )
        try:
            raw = await (asyncio.wait_for(call, timeout) if timeout else call)
        except json.JSONDecodeError as e:
            print("[WARN] 讀圖回傳的不是 JSON，重讀:", e)
            raw = {}
        shot = settle.clean_shot(raw)
        if best is None or _seen(shot) > _seen(best):
            best = shot
        if shot["items"] and not shot["dropped"]:
            break
    return best


async def read_screenshot(data: bytes, mime: str) -> dict:
    """讀一張截圖：先用本地模型，連不上、太慢或讀不完整才把截圖送到 Groq。"""
    global local_vision_down_until
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    local = None
    if local_vision_client is not None and time.monotonic() >= local_vision_down_until:
        started = time.monotonic()
        try:
            local = await _read_with(local_vision_client, LOCAL_VISION_MODEL, url, LOCAL_VISION_TIMEOUT)
        except Exception as e:
            local_vision_down_until = time.monotonic() + LOCAL_VISION_COOLDOWN
            print(f"[WARN] 本地讀圖失敗（{type(e).__name__}: {e}），接下來 {LOCAL_VISION_COOLDOWN} 秒改用 Groq")
        else:
            if local["items"] and not local["dropped"]:
                print(f"讀圖: 本地 {LOCAL_VISION_MODEL} {time.monotonic() - started:.1f}s")
                return local
            print("[WARN] 本地讀圖讀不完整，改用 Groq 再讀一次")
    started = time.monotonic()
    remote = await _read_with(groq_client, VISION_MODEL, url)
    print(f"讀圖: Groq {VISION_MODEL} {time.monotonic() - started:.1f}s")
    return remote if local is None or _seen(remote) > _seen(local) else local


async def match_rows(payload: list[dict]) -> list[dict]:
    """請 LLM 判斷每個商品對應表格的哪幾列。沒有候選列的商品不用問。"""
    todo = [p for p in payload if p["candidates"]]
    results = []
    for i in range(0, len(todo), MATCH_CHUNK):
        raw = await llm_json(
            groq_client,
            model=GROQ_MODEL,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": settle.MATCH_PROMPT},
                {"role": "user", "content": json.dumps(todo[i:i + MATCH_CHUNK], ensure_ascii=False)},
            ],
        )
        results += raw.get("results") or []
    return results


async def market_rate() -> float | None:
    """今天的 CNY→TWD 市場匯率，只用來檢查回算匯率合不合理；抓不到就不檢查。"""
    try:
        resp = await asyncio.to_thread(requests.get, "https://open.er-api.com/v6/latest/CNY", timeout=5)
        return float(resp.json()["rates"]["TWD"])
    except Exception as e:
        print("[WARN] 抓不到市場匯率:", e)
        return None


async def read_images(images: list, first: int = 1) -> list[dict]:
    """first：這批第一張在整張草稿裡是第幾張，錯誤訊息裡的「第 N 張」才對得上使用者傳的順序。"""
    shots = []
    for i, att in enumerate(images, first):
        if att.size > MAX_IMAGE_BYTES:
            raise SettleError(f"第 {i} 張圖超過 3MB，請改傳手機截圖（不要傳原始相片檔）")
        shots.append(await read_screenshot(await att.read(), att.content_type.split(";")[0]))
    return shots


def next_number(channel_id: int, session: dict) -> int:
    """同頻道裡還能認領的結算、和其他人進行中的草稿用到的最大編號 + 1，讓「認領 N」不會撞號。"""
    used = [it["no"] for s in SETTLEMENTS.values() if s["channel_id"] == channel_id and not s.get("closed")
            for u in s["units"] for it in u["items"]]
    used += [it["no"] for s in SETTLEMENTS.values() if s["channel_id"] == channel_id and not s.get("closed")
             for u in s.get("append_attempt", {}).get("added_units", []) for it in u["items"]]
    used += [it["no"] for p in PENDING.values() if p is not session and p["channel_id"] == channel_id and p["state"]
             for u in p["state"]["units"] for it in u["items"]]
    return max(used, default=0) + 1


async def build_settlement(session: dict, shots: list[dict]) -> tuple[dict, list[dict], dict]:
    units, errors, notes = settle.merge_shots(shots, start=next_number(session["channel_id"], session))
    if errors:
        raise SettleError("截圖有問題，沒辦法結算：\n" + "\n".join(f"- {e}" for e in errors))
    if not units:
        raise SettleError(
            "我在截圖裡找不到商品和價格 QQ\n"
            "請傳淘寶「确认订单」頁或訂單詳情頁的截圖，要拍到台幣金額。"
        )

    base = None
    if session.get("append_to"):
        base = SETTLEMENTS.get(session["append_to"])
        if base is None or base.get("closed"):
            raise SettleError("這筆結算已經結束認領，不能再追加截圖。")
        if base.get("append_attempt"):
            raise SettleError("這筆結算有已確認的追加正在寫入，請先在那張追加草稿按 ✅ 重試完成。")
        state, units, errors = settle.prepare_append(base, units)
        if errors:
            raise SettleError("沒辦法追加：\n" + "\n".join(f"- {e}" for e in errors))

    resp = await sheet_call({"action": "list"})
    if not resp.ok:
        raise SettleError("讀不到購物清單 QQ")
    rows = resp.json()["rows"]
    if base is not None:
        used = {row for u in base["units"] for it in u["items"] for row in it.get("row_ids", []) if row}
        rows = [r for r in rows if not r.get("id") or r["id"] not in used]

    payload = settle.match_payload(units, rows)
    writes, more = settle.assign(units, rows, payload, await match_rows(payload))
    previous = [s for s in SETTLEMENTS.values() if s["channel_id"] == session["channel_id"]
                and s["batch"] != session.get("append_to")]
    if "market_rate" not in session:
        session["market_rate"] = await market_rate()
    payer, poster = session["payer"], session["poster"]
    notes += more + settle.duplicate_notes(units, previous)
    if base is not None:
        notes += settle.append_overlap_notes(base, units)
        details = dict(added_units=units, append_notes=notes, before_state=deepcopy(base),
                       base_revision=base.get("append_revision", 0))
        state["notes"] = list(base.get("notes") or []) + notes
        return state, writes, details
    state = {
        "batch": session["batch"],
        "payer": payer.name,
        "payer_id": payer.id,
        "poster": "" if payer.id == poster.id else poster.name,
        "poster_id": poster.id,
        "channel_id": session["channel_id"],
        "units": units,
        "notes": notes,
        "market_rate": session["market_rate"],
        "mentions": {},
        "message_ids": [],
    }
    return state, writes, {}


async def send_chunks(target, text: str, ping=False, register: dict | None = None, view=None,
                      channel_only: bool = False) -> list:
    # 只 ping 人：商品名稱是從截圖讀來的，不能讓它觸發 @everyone 或身分組
    allowed = discord.AllowedMentions(everyone=False, roles=False, users=ping, replied_user=False)
    chunks = settle.split_message(text)
    sent = []
    for i, chunk in enumerate(chunks):
        kw = {"allowed_mentions": allowed}
        if view is not None and i == len(chunks) - 1:
            kw["view"] = view  # 按鈕放在最後一段
        message = await target.reply(chunk, **kw) if i == 0 and not channel_only else await target.channel.send(chunk, **kw)
        sent.append(message)
        if register is not None:
            register["message_ids"].append(message.id)
            save_settlements()
    return sent


async def member_mentions(guild, state: dict) -> dict:
    """表格裡記的是 Discord username，要查到 user id 才 @ 得到人；查不到就顯示純文字。"""
    mentions = {state["payer"]: f"<@{state['payer_id']}>"}
    for name in set(settle.totals(state)["twd"]) - {settle.UNCLASSIFIED, state["payer"]}:
        if guild is None:
            continue
        member = discord.utils.find(lambda m: m.name == name, guild.members)
        if member is None:
            try:
                found = await guild.query_members(query=name, limit=5)
                member = next((m for m in found if m.name == name), None)
            except Exception as e:
                print(f"[WARN] 查不到成員 {name}: {e}")
        if member is not None:
            mentions[name] = member.mention
    return mentions


async def handle_settle(message: discord.Message, append_to: dict | None = None):
    """「結算」＋截圖開一張草稿；還沒按 ✅ 之前，同一個人在同頻道再傳的截圖都併進同一張。"""
    images = [a for a in message.attachments if (a.content_type or "").startswith("image/")][:MAX_IMAGES]
    if not images:
        await message.reply("要結算的話，請把下單截圖和「結算」放在同一則訊息裡傳上來 🧾")
        return

    key = (message.channel.id, message.author.id)
    session = PENDING.get(key)
    if session is not None and session.get("write_started"):
        await message.reply("這批追加已經確認並送出寫入，請先在原追加草稿按 ✅ 重試完成，再傳其他截圖。")
        return
    if append_to is not None:
        if append_to.get("closed"):
            await message.reply("這筆結算已經結束認領，不能再追加截圖。")
            return
        if session is not None and session.get("append_to") != append_to["batch"]:
            await message.reply("你還有另一張草稿，請先按 ✅ 確認或 ❌ 取消，再追加這筆結算。")
            return
    if session is None:
        batch, taken, n = datetime.now().strftime("%Y%m%d-%H%M%S"), set(SETTLEMENTS) | {p["batch"] for p in PENDING.values()}, 1
        while (batch if n == 1 else f"{batch}-{n}") in taken:  # 同一秒有兩個人開始結算
            n += 1
        session = PENDING[key] = {
            "batch": batch if n == 1 else f"{batch}-{n}",
            "payer": named_user(message) or message.author,  # 「結算 @B」＝幫 B 結算，大家要轉帳給 B
            "poster": message.author,
            "channel_id": message.channel.id,
            "guild": message.guild,
            "lock": asyncio.Lock(),
            "shots": [],
            "state": None,
            "writes": [],
            "drafts": [],
            "timer": None,
        }
        if append_to is not None:
            session.update(
                batch=append_to["batch"], append_to=append_to["batch"],
                payer=SimpleNamespace(name=append_to["payer"], id=append_to["payer_id"]),
                poster=SimpleNamespace(name=append_to.get("poster") or append_to["payer"],
                                       id=append_to.get("poster_id", append_to["payer_id"])),
                market_rate=append_to.get("market_rate"),
            )

    await safe_react(message, "⏳")
    try:
        async with session["lock"]:
            if PENDING.get(key) is not session:
                await message.reply("剛剛那張草稿已經確認或取消了，這幾張沒有算進去。要另外結算的話，請重新傳並打「結算」。")
                return
            if session.get("write_started"):
                await message.reply("這張草稿已經確認，請先在原草稿重試完成寫入，再傳其他截圖。")
                return
            shots = session["shots"] + await read_images(images, first=len(session["shots"]) + 1)
            async with CHANNEL_LOCKS.setdefault(message.channel.id, asyncio.Lock()):
                if session.get("append_to"):
                    async with settlement_lock(session["append_to"]):
                        state, writes, details = await build_settlement(session, shots)
                else:
                    state, writes, details = await build_settlement(session, shots)
                if session.get("append_to"):
                    text = settle.render_append_draft(details["before_state"], state, details["added_units"],
                                                      details["append_notes"], session.get("market_rate"))
                else:
                    text = settle.render(state, final=False)
                sent = await send_chunks(message, text, view=draft_view())
                old = session["drafts"]
                session.update(shots=shots, state=state, writes=writes, drafts=sent, **details)
            for draft in old:
                try:
                    await draft.delete()
                except Exception as e:
                    print("[WARN] 刪不掉舊草稿:", e)
            restart_timer(key, session)
    except SettleError as e:
        await message.reply(str(e) + ("\n（之前的草稿還在，這幾張沒有併進去）" if session["state"] else ""))
    except Exception as e:
        print("Error building settlement:", repr(e))
        await message.reply("結算時我撞牆了 QQ 請幫我看一下 log")
    finally:
        if session["state"] is None and PENDING.get(key) is session:
            PENDING.pop(key)
        await safe_react(message, "⏳", remove=True)


def restart_timer(key: tuple, session: dict):
    if session["timer"]:
        session["timer"].cancel()
    session["timer"] = asyncio.create_task(expire_draft(key, session))


async def expire_draft(key: tuple, session: dict):
    await asyncio.sleep(SETTLE_CONFIRM_SECONDS)
    async with session["lock"]:
        if PENDING.get(key) is not session:
            return
        if session.get("write_started"):
            await session["drafts"][-1].reply("追加已確認並存檔，表格寫入結果仍待確認，請移除再加上 ✅ 重試完成。")
            return
        PENDING.pop(key)
    await session["drafts"][-1].reply("⌛ 太久沒有確認，這次結算已取消，表格沒有變動。")


def discard_draft(key: tuple, session: dict):
    if PENDING.get(key) is session:
        PENDING.pop(key)
    if session["timer"]:
        session["timer"].cancel()


async def write_settlement(prompt, state: dict, writes: list[dict], retrying: bool = False) -> str:
    """成功回空字串；表格衝突須重做草稿，暫時失敗可重試同一批寫入。"""
    if not writes:
        return ""
    try:
        resp = await sheet_call({"action": "settle", "batch": state["batch"], "rows": writes})
    except Exception as e:
        print("Error writing settlement to sheet:", repr(e))
        await prompt.reply(sheet_failure_reply("結算", "寫進表格", e))
        return "retry"
    if resp.status_code == 409:
        problems = resp.json().get("problems", [])
        explanation = ("先前的追加可能已寫進表格，但這次核對發現內容有變。已保留確認紀錄，請先核對這些列：\n"
                       if retrying else "表格在這幾分鐘內被改過，這次沒有寫入任何東西，請重新傳截圖結算一次：\n")
        await prompt.reply(
            explanation
            + "\n".join(f"- {p['tab']} 第 {p['row']} 列：{p['reason']}" for p in problems[:10])
        )
        return "conflict"
    if not resp.ok:
        await prompt.reply("結算結果寫進表格失敗 QQ，請稍後再試。")
        return "retry"
    return ""


async def confirm_append(key: tuple, session: dict):
    prompt = session["drafts"][-1]
    async with settlement_lock(session["append_to"]):
        state = SETTLEMENTS.get(session["append_to"])
        if state is None or state.get("closed"):
            discard_draft(key, session)
            await prompt.reply("這筆結算已經結束認領，不能再追加截圖。")
            return
        attempt = state.get("append_attempt")
        if attempt and attempt["prompt_id"] != prompt.id:
            await prompt.reply("這筆結算有另一批已確認的追加正在寫入，請先完成那張草稿。")
            return
        if state.get("append_revision", 0) != session["base_revision"]:
            discard_draft(key, session)
            await prompt.reply("這筆結算已有另一批追加確認，請回覆最新結算訊息重傳這次截圖，重新確認金額。")
            return
        before = deepcopy(state)
        combined, added, errors = settle.prepare_append(state, session["added_units"])
        if errors:
            discard_draft(key, session)
            await prompt.reply("沒辦法追加：\n" + "\n".join(errors))
            return
        if not attempt:
            proposed = deepcopy(state)
            proposed["append_attempt"] = {
                "added_units": deepcopy(session["added_units"]), "writes": deepcopy(session["writes"]),
                "notes": list(session["append_notes"]), "base_revision": session["base_revision"],
                "prompt_id": prompt.id, "submitter_id": key[1],
            }
        else:
            proposed = deepcopy(state)
        session["write_started"] = True
        try:
            await persist_settlement(proposed)
        except Exception as e:
            state.update(proposed)
            save_settlements()
            print("[WARN] 追加確認紀錄無法確認寫入結果:", repr(e))
            await prompt.reply("追加確認紀錄的寫入結果仍待確認，已保留這張草稿，請稍後重試。")
            return
        state.update(proposed)
        save_settlements()
        outcome = await write_settlement(prompt, combined, session["writes"], retrying=bool(attempt))
        if outcome:
            if outcome == "conflict" and not attempt:
                proposed = deepcopy(state)
                proposed.pop("append_attempt", None)
                await persist_settlement(proposed)
                state.clear()
                state.update(proposed)
                save_settlements()
                discard_draft(key, session)
            else:
                restart_timer(key, session)
                await prompt.reply("追加草稿仍保留，稍後請移除再加上 ✅ 重試同一批追加。")
            return

        combined["notes"] = list(state.get("notes") or []) + session["append_notes"]
        combined["append_revision"] = state.get("append_revision", 0) + 1
        combined.pop("append_attempt", None)
        try:
            await persist_settlement(combined)
        except Exception as e:
            print("[WARN] 追加結果無法寫進帳本:", repr(e))
            restart_timer(key, session)
            await prompt.reply("追加已送出，結算紀錄仍在等待寫入，請稍後再按確認完成。")
            return
        state.update(combined)
        state.pop("append_attempt", None)
        save_settlements()
        discard_draft(key, session)
        try:
            state["mentions"].update(await member_mentions(session["guild"], state))
            changed = settle.changed_owners(before, state)
            people = [discord.Object(id=int(mention[2:-1].lstrip("!")))
                      for name in changed if (mention := state["mentions"].get(name, "")).startswith("<@")]
            text = settle.render_append_result(before, state, added)
            await send_chunks(prompt, text, ping=people, register=state, view=settlement_view(state))
        except Exception as e:
            print(f"[ERROR] 結算 {state['batch']} 已追加，但通知送不出去:", repr(e))
            await prompt.reply("追加已完成並存檔，但更新金額的通知送出失敗，請查看 bot log。")


async def confirm_draft(key: tuple, session: dict, message_id: int, ok: bool):
    async with session["lock"]:
        # 按的是被新截圖取代掉的舊草稿 → 不算，要在新的草稿上按
        if PENDING.get(key) is not session or session["drafts"][-1].id != message_id:
            return
        prompt, state, writes = session["drafts"][-1], session["state"], session["writes"]
        if not ok:
            if session.get("write_started"):
                await prompt.reply("這批追加已經確認並送出，請按 ✅ 重試完成寫入。")
                return
            discard_draft(key, session)
            await prompt.reply("已取消，表格沒有變動。")
            return
        if session.get("append_to"):
            await confirm_append(key, session)
            return
        attempt = state.get("commit_attempt")
        if not attempt:
            if writes:
                try:
                    response = await sheet_call({"action": "validate_settle", "batch": state["batch"], "rows": writes})
                except Exception as e:
                    print("[WARN] 結算預檢失敗:", repr(e))
                    await prompt.reply("暫時讀不到表格，請稍後再按確認。")
                    return
                if response.status_code != 409 and not response.ok:
                    discard_draft(key, session)
                    await prompt.reply("結算結果寫進表格失敗 QQ，請稍後再試。")
                    return
                if not response.ok:
                    discard_draft(key, session)
                    await prompt.reply("表格在這幾分鐘內被改過，請重新傳截圖確認：\n" + "\n".join(
                        f"- {p['tab']} 第 {p['row']} 列：{p['reason']}" for p in response.json().get("problems", [])))
                    return
            proposed = deepcopy(state)
            proposed["commit_attempt"] = {"writes": writes, "prompt_id": prompt.id, "submitter_id": key[1]}
        else:
            proposed = deepcopy(state)
        session["write_started"] = True
        try:
            await persist_settlement(proposed)
        except Exception as e:
            state.update(proposed)
            SETTLEMENTS[state["batch"]] = state
            save_settlements()
            print("[WARN] 結算確認紀錄無法確認寫入結果:", repr(e))
            await prompt.reply("結算確認紀錄的寫入結果仍待確認，已保留這張草稿，請稍後重試。")
            return
        state.update(proposed)
        SETTLEMENTS[state["batch"]] = state
        save_settlements()
        if await write_settlement(prompt, state, writes, retrying=bool(attempt)):
            restart_timer(key, session)
            return

        proposed = deepcopy(state)
        proposed.pop("commit_attempt", None)
        try:
            await persist_settlement(proposed)
        except Exception as e:
            print("[WARN] 結算結果無法寫進帳本:", repr(e))
            restart_timer(key, session)
            await prompt.reply("結算已送出，結算紀錄仍在等待寫入，請稍後再按確認完成。")
            return
        state.clear()
        state.update(proposed)

        state["mentions"] = await member_mentions(session["guild"], state)
        SETTLEMENTS[state["batch"]] = state
        save_settlements()
        discard_draft(key, session)
        await send_chunks(prompt, settle.render(state, final=True, mentions=state["mentions"]),
                          ping=True, register=state, view=settlement_view(state))


def claim_numbers(text: str) -> list[int]:
    """「認領 3 5、7 9-11」→ [3, 5, 7, 9, 10, 11]。@ 人的 <@id> 裡的數字不算。"""
    rest = re.sub(r"<[@#][!&]?\d+>", " ", text)
    nos = []
    for m in re.finditer(r"#?(\d+)(?:\s*[-~～–至到]\s*#?(\d+))?", rest):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        nos += range(min(a, b), max(a, b) + 1) if abs(a - b) < 100 else [a, b]
    return list(dict.fromkeys(nos))


async def apply_claims(pool: list[dict], replied: dict | None, nos: list[int], target) -> tuple[dict, list[str]]:
    """把這些編號認領給 target。回傳 ({batch: [認領成功的編號]}, [沒領到的原因])。"""
    def find_state(no):
        if replied:
            return replied
        open_item = lambda s: (lambda it: it is not None and settle.is_unclassified(it))(settle.find_item(s, no)[1])
        # 有這個編號但不能領（已經有主）的，交給 claim() 說明原因
        return (next((s for s in reversed(pool) if open_item(s)), None)
                or next((s for s in reversed(pool) if not s.get("closed") and settle.find_item(s, no)[1]), None))

    claimed: dict[str, list[int]] = {}
    failures = {}
    grouped = {}
    for no in nos:
        state = find_state(no)
        if state is None:
            failures[no] = f"找不到還沒人認領的 #{no} 🙅"
            continue
        grouped.setdefault(state["batch"], (state, []))[1].append(no)
    for batch, (state, numbers) in grouped.items():
        async with settlement_lock(batch):
            proposed, done = deepcopy(state), []
            for no in numbers:
                error = settle.claim(proposed, no, target.name)
                if error:
                    failures[no] = error
                else:
                    done.append(no)
            if not done:
                continue
            proposed["mentions"][target.name] = target.mention
            try:
                await persist_settlement(proposed)
            except Exception as e:
                print("[WARN] 認領寫入失敗:", repr(e))
                failures.update({no: f"#{no} 的認領暫時無法寫進表格，請稍後重試。" for no in done})
                continue
            state.update(proposed)
            save_settlements()
            claimed[batch] = done
    return claimed, [failures[no] for no in nos if no in failures]


def claim_message(state: dict, done: list[int], target, failed: list[str]) -> str:
    items = [settle.find_item(state, no)[1] for no in done]
    if len(items) == 1:
        head = f"#{done[0]} {items[0]['name']} 算在 {target.mention} 名下 ✅"
    else:
        head = f"{len(items)} 件算在 {target.mention} 名下 ✅\n" + "\n".join(f"#{it['no']} {it['name']}" for it in items)
    if failed:
        head += "\n\n沒有認領到：\n" + "\n".join(f"- {e}" for e in failed)
    tail = (
        f"{state['payer']} 按 🏁 可以結束認領，剩下的算 {state['payer']} 自己的"
        if settle.has_unclassified(state)
        else f"全部都有人認領了，{state['payer']} 按 🏁 公布最終金額並通知大家"
    )
    return f"{head}\n\n**結算 {state['batch']} 更新後：**\n{settle.render_totals(state, state['mentions'])}\n\n{tail}"


async def handle_claim(message: discord.Message, nos: list[int]):
    if len(nos) > MAX_CLAIMS:
        await message.reply(f"一次最多認領 {MAX_CLAIMS} 件，請分幾次打 🙏")
        return

    pool = [s for s in SETTLEMENTS.values() if s["channel_id"] == message.channel.id and not s.get("commit_attempt")]
    replied_to = message.reference.message_id if message.reference else None
    replied = next((s for s in pool if replied_to in s["message_ids"]), None)
    target = named_user(message) or message.author
    claimed, failed = await apply_claims(pool, replied, nos, target)

    if not claimed:
        if len(failed) == 1 and failed[0].startswith("找不到"):
            failed[0] += "\n要認領的話，請直接回覆在那則結算訊息底下。"
        await message.reply("\n".join(failed) if len(failed) == 1 else "沒有認領到任何一件：\n" + "\n".join(f"- {e}" for e in failed))
        return

    for i, (batch, done) in enumerate(claimed.items()):
        state = SETTLEMENTS[batch]
        chunks = settle.split_message(claim_message(state, done, target, failed if i == len(claimed) - 1 else []))
        reply = await message.reply(chunks[0], allowed_mentions=discord.AllowedMentions(users=[target]),
                                    **({"view": settlement_view(state)} if len(chunks) == 1 else {}))
        for j, chunk in enumerate(chunks[1:], 2):
            reply = await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none(),
                                               **({"view": settlement_view(state)} if j == len(chunks) else {}))
        state["message_ids"].append(reply.id)  # 回覆這則也能找到同一次結算
    save_settlements()


# -------------------------------------------------
#  改歸屬：下單人把某件商品改給別人（對錯人，或幫沒上線的人指定）
# -------------------------------------------------
REASSIGN_RE = re.compile(r"^(改歸屬|改)\s*#?\s*\d")


async def apply_reassign(state: dict, nos: list[int], target) -> tuple[bool, str]:
    """回傳 (有沒有改到, 要給大家看的訊息)。"""
    done, failed, released = [], [], []
    async with settlement_lock(state["batch"]):
        proposed = deepcopy(state)
        for no in nos:
            item = settle.find_item(proposed, no)[1]
            row_ids = (item.get("row_ids") or []) if item else []
            if item and item.get("rows") and (len(row_ids) != len(item["rows"]) or not all(row_ids)):
                failed.append(f"#{no} 的舊版表格對應還無法唯一確認，請先核對原登記列。")
                continue
            error, row_ids = settle.reassign(proposed, no, target.name)
            if error:
                failed.append(error)
            else:
                done.append(no)
                released += row_ids
        if done:
            proposed["mentions"][target.name] = target.mention
            try:
                await persist_settlement(proposed, released)
            except Exception as e:
                print("[WARN] 改歸屬寫入失敗:", repr(e))
                return False, "改歸屬的表格更新暫時失敗，請稍後重試。"
            state.update(proposed)
            save_settlements()
    if not done:
        return False, failed[0] if len(failed) == 1 else "沒有改到任何一件：\n" + "\n".join(f"- {e}" for e in failed)
    head = f"✏️ {'、'.join(f'#{n}' for n in done)} 改算在 {target.mention} 名下"
    if failed:
        head += "\n\n注意：\n" + "\n".join(f"- {e}" for e in failed)
    return True, f"{head}\n\n**結算 {state['batch']} 更新後：**\n{settle.render_totals(state, state['mentions'])}"


async def handle_reassign(message: discord.Message, nos: list[int]):
    pool = [s for s in SETTLEMENTS.values() if s["channel_id"] == message.channel.id and not s.get("closed") and not s.get("commit_attempt")]
    state = replied_settlement(message) or next(
        (s for s in reversed(pool) if any(settle.find_item(s, n)[1] for n in nos)), None)
    if state is None:
        await message.reply("找不到有這個編號、而且還在認領中的結算 🙅")
        return
    if not can_manage(state, message.author.id):
        await message.reply(f"只有下單人（{state['payer']}）或代傳的人可以改歸屬。")
        return
    target = named_user(message)
    if target is None:
        await message.reply("要改給誰？請打 `改 3 @某人`（可以一次改好幾件：`改 3 5 @某人`）。")
        return
    ok, text = await apply_reassign(state, nos, target)
    reply = await message.reply(text, allowed_mentions=discord.AllowedMentions(users=[target]),
                                **({"view": settlement_view(state)} if ok else {}))
    if ok:
        state["message_ids"].append(reply.id)
        save_settlements()


async def close_settlement(state: dict, channel, ref_message_id: int) -> str:
    """結束認領並公布最終金額。回傳空字串代表完成，否則是不能結束的原因。"""
    async with settlement_lock(state["batch"]):
        if state.get("closed"):
            return f"結算 {state['batch']} 已經結束認領了。"
        if state.get("append_attempt") or state.get("commit_attempt"):
            return "這筆結算的追加已確認，表格寫入結果仍待確認，請先在追加草稿按 ✅ 重試完成，再按 🏁。"
        proposed = deepcopy(state)
        settle.close(proposed)
        try:
            await persist_settlement(proposed)
        except Exception as e:
            print("[WARN] 結束認領寫入失敗:", repr(e))
            return "結束認領的表格更新暫時失敗，請稍後重試。"
        state.update(proposed)
        save_settlements()
        try:
            ref = discord.MessageReference(message_id=ref_message_id, channel_id=channel.id, fail_if_not_exists=False)
            allowed = discord.AllowedMentions(everyone=False, roles=False, users=True)
            for i, chunk in enumerate(settle.split_message(settle.render_closed(state, state["mentions"]))):
                await channel.send(chunk, reference=ref if i == 0 else None, allowed_mentions=allowed)
        except Exception as e:
            print(f"[ERROR] 結算 {state['batch']} 已結束認領，但公布訊息送不出去:", repr(e))
    return ""


# -------------------------------------------------
#  按鈕和選單按下去之後
# -------------------------------------------------
def _option(item: dict, description: str) -> discord.SelectOption:
    return discord.SelectOption(label=f"#{item['no']} {item['name']}"[:100], value=str(item["no"]), description=description[:100])


async def on_component(interaction, custom_id: str, values: list):
    prefix, kind, *args = custom_id.split(":")
    user, msg = interaction.user, interaction.message

    if kind == "draft":
        hit = next(((k, v) for k, v in PENDING.items() if v["drafts"] and v["drafts"][-1].id == msg.id), None)
        if hit is None:
            await ephemeral(interaction, "這張草稿已經確認、取消、過期，或被新的草稿取代了。")
        elif user.id not in (hit[1]["payer"].id, hit[1]["poster"].id):
            await ephemeral(interaction, "只有下單人或代傳的人可以按。")
        else:
            await interaction.response.defer()
            await confirm_draft(hit[0], hit[1], msg.id, args[0] == "ok")
        return

    if kind == "del":
        pending = DELETE_CHOICES.get((interaction.channel.id, user.id))
        if not pending or str(pending["id"]) != args[0] or pending.get("prompt_id") != msg.id:
            await ephemeral(interaction, "這不是你的刪除選項，或選項已經過期了。")
            return
        await interaction.response.defer()
        asker = SimpleNamespace(author=user, channel=interaction.channel, content=f"刪除 {args[1]}", reply=msg.reply)
        await confirm_delete_choice(asker, int(args[1]), expected_choice_id=args[0])
        return

    if kind == "dup":
        pending = ADD_PENDING.get(args[0])
        if not pending or pending["expires"] <= time.monotonic():
            ADD_PENDING.pop(args[0], None)
            await ephemeral(interaction, "這個選項已經過期了，還要加的話請重新打一次。")
        elif pending["user_id"] != user.id:
            await ephemeral(interaction, "這是別人的登記，只有他自己可以決定。")
        elif pending.get("prompt_id") != msg.id or pending.get("channel_id") != interaction.channel.id:
            await ephemeral(interaction, "請使用這次登記訊息上的選項。")
        else:
            ADD_PENDING.pop(args[0])
            await interaction.response.defer()
            if args[1] == "y":
                await send_add(SimpleNamespace(author=user, reply=msg.reply), {**pending["payload"], "force": True})
            else:
                await msg.reply("好，沒有重複登記。")
        return

    # 以下都是某一筆已公布的結算上的操作
    state = SETTLEMENTS.get(args[0])
    if state is None or state["channel_id"] != interaction.channel.id:
        await ephemeral(interaction, "找不到這筆結算的紀錄（可能是太久以前的了）。")
        return
    if state.get("closed"):
        await ephemeral(interaction, f"結算 {state['batch']} 已經結束認領了，有問題請直接找下單的人。")
        return
    if state.get("commit_attempt"):
        await ephemeral(interaction, "這筆結算正在完成表格回寫，請稍後再操作。")
        return
    batch = state["batch"]
    rate_of = {it["no"]: u["rate"] for u in state["units"] for it in u["items"]}
    all_items = [it for u in state["units"] for it in u["items"]]

    if kind == "claim" and prefix == "sl":
        mine = [it for it in all_items if settle.is_unclassified(it)]
        if not mine:
            await ephemeral(interaction, "已經沒有待認領的商品了。")
            return
        options = [_option(it, f"×{it['qty']}　¥{it['line']:.2f} ≈ TWD {it['line'] * rate_of[it['no']]:,.0f}") for it in mine[:25]]
        more = "\n（只列出前 25 件，其他請打 `認領 編號`）" if len(mine) > 25 else ""
        await ephemeral(interaction, f"選出你的商品，可以一次選好幾件：{more}",
                        view_of(SLSelect(f"sls:claim:{batch}", options, "選你的商品", max_values=len(options))))
    elif kind == "claim":
        await interaction.response.defer()
        claimed, failed = await apply_claims([state], state, [int(v) for v in values], user)
        if not claimed:
            await interaction.edit_original_response(content="沒有認領到：\n" + "\n".join(f"- {e}" for e in failed), view=None)
            return
        await interaction.edit_original_response(content="認領好了 ✅", view=None)
        await send_chunks(interaction, claim_message(state, claimed[batch], user, failed), ping=[user],
                          register=state, view=settlement_view(state), channel_only=True)

    elif kind == "re":
        if not can_manage(state, user.id):
            await ephemeral(interaction, f"只有下單人（{state['payer']}）或代傳的人可以改歸屬。")
        elif prefix == "sl":
            who = lambda it: "、".join("沒人認領" if x["owner"] == settle.UNCLASSIFIED else x["owner"] for x in it["shares"])
            options = [_option(it, f"目前：{who(it)}") for it in all_items[:25]]
            more = "\n（只列出前 25 件，其他請打 `改 編號 @某人`）" if len(all_items) > 25 else ""
            await ephemeral(interaction, f"要改哪幾件？{more}",
                            view_of(SLSelect(f"sls:re:{batch}", options, "選商品", max_values=min(10, len(options)))))
        elif prefix == "sls":
            picked = "、".join(f"#{v}" for v in values)
            await interaction.response.edit_message(
                content=f"要把 {picked} 改給誰？", view=view_of(SLUserSelect(f"slu:re:{batch}:{'.'.join(values)}", "選一個人")))
        else:
            target = values[0]
            if getattr(target, "bot", False):
                await interaction.response.edit_message(content="不能改給機器人，請重新按「改歸屬」。", view=None)
                return
            await interaction.response.defer()
            ok, text = await apply_reassign(state, [int(n) for n in args[1].split(".")], target)
            await interaction.edit_original_response(content="改好了 ✅" if ok else text, view=None)
            if ok:
                await send_chunks(interaction, text, ping=[target], register=state,
                                  view=settlement_view(state), channel_only=True)

    elif kind == "close":
        if not can_manage(state, user.id):
            await ephemeral(interaction, f"只有下單人（{state['payer']}）或代傳的人可以結束認領。")
            return
        await interaction.response.defer()
        reason = await close_settlement(state, interaction.channel, msg.id)
        if reason:
            await interaction.followup.send(reason, ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """主要的操作方式是按鈕。手動加表情也還是有效：草稿上的 ✅／❌、結算訊息上的 🏁。"""
    emoji = str(payload.emoji)
    if bot.user and payload.user_id == bot.user.id:
        return
    if emoji in ("✅", "❌"):
        for key, session in list(PENDING.items()):
            if session["drafts"] and session["drafts"][-1].id == payload.message_id:
                if payload.user_id in (session["payer"].id, session["poster"].id):
                    await confirm_draft(key, session, payload.message_id, emoji == "✅")
                return
        return
    if emoji != CLOSE_EMOJI:
        return
    state = next((s for s in SETTLEMENTS.values() if payload.message_id in s["message_ids"]), None)
    if state is None or state.get("closed"):
        return
    if payload.user_id not in (state["payer_id"], state.get("poster_id", state["payer_id"])):
        return

    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    reason = await close_settlement(state, channel, payload.message_id)
    if reason and not state.get("closed"):
        await channel.send(reason)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ALLOWED_CHANNEL_IDS:
        print(f"Allowed channel IDs: {sorted(ALLOWED_CHANNEL_IDS)}")
    else:
        print("Allowed channel IDs: 不限制（所有頻道都會處理）")
    print(f"LLM: Groq {GROQ_MODEL} ✓")
    reader = f"本地 {LOCAL_VISION_MODEL} @ {LOCAL_VISION_URL}（失敗退回 Groq {VISION_MODEL}）" if LOCAL_VISION_URL else f"Groq {VISION_MODEL}"
    print(f"結算: 讀圖 {reader}，已載入 {len(SETTLEMENTS)} 筆結算紀錄")
    await prepare_sheet()
    await recover_commits()
    await recover_appends()


_sheet_ready = False


def migrate_row_ids(state: dict, rows: list[dict]):
    """舊紀錄只在批次、買家與實際價格能唯一對應時補固定 ID。"""
    used = {row_id for unit in state["units"] for item in unit["items"] for row_id in item.get("row_ids", []) if row_id}
    for unit in state["units"]:
        for item in unit["items"]:
            row_ids = item.get("row_ids") or []
            if not item.get("rows") or (len(row_ids) == len(item["rows"]) and all(row_ids)):
                continue
            resolved = []
            owners = {share["owner"] for share in item["shares"]}
            amounts = [share["cny"] for share in item["shares"]] + [item["line"]]
            for index, label in enumerate(item["rows"]):
                if index < len(row_ids) and row_ids[index]:
                    resolved.append(row_ids[index])
                    continue
                tab = next((tab for tab in sheets.TABS if str(label).startswith(tab)), None)
                candidates = [row for row in rows if row["tab"] == tab and row["batch"] == state["batch"]
                              and row["buyer"] in owners and row.get("id") and row["id"] not in used
                              and row["id"] not in resolved]
                matching = []
                for row in candidates:
                    try:
                        same_price = any(abs(float(row["price"]) - value) < 0.005 for value in amounts)
                    except (TypeError, ValueError):
                        same_price = False
                    if same_price:
                        matching.append(row)
                if len(matching) != 1:
                    break
                row_id = matching[0]["id"]
                resolved.append(row_id)
            if len(resolved) == len(item["rows"]):
                item["row_ids"] = resolved
                used.update(resolved)


async def prepare_sheet():
    """啟動時確認表格有需要的欄位和分頁；本機沒有的結算紀錄從表格還原。"""
    global _sheet_ready
    if _sheet_ready:
        return
    try:
        done = (await sheet_call({"action": "layout"})).json()["done"]
        print("表格: " + ("、".join(done) if done else "欄位和分頁都齊全"))
        saved = (await sheet_call({"action": "states"})).json()["states"]
        restored = []
        merged = dict(SETTLEMENTS)
        for batch, state in saved.items():
            local = merged.get(batch)
            if local is None or int(state.get("_version", 0)) >= int(local.get("_version", 0)):
                merged[batch] = state
                restored.append(batch)
        SETTLEMENTS.clear()
        SETTLEMENTS.update(dict(sorted(merged.items())))
        rows = (await sheet_call({"action": "all_rows"})).json()["rows"]
        for batch, state in SETTLEMENTS.items():
            migrate_row_ids(state, rows)
            if saved.get(batch) == json.loads(_snapshot(state)):
                _ledger_synced[batch] = _snapshot(state)
        if restored:
            print(f"結算: 從表格還原 {len(restored)} 筆結算紀錄")
        _sheet_ready = True
        save_settlements()
        await sync_ledger(delay=0)
    except Exception as e:
        print("[ERROR] 表格準備失敗:", repr(e))


async def recover_commits():
    """接續已確認、尚未完成表格回寫的新結算。"""
    for state in list(SETTLEMENTS.values()):
        attempt = state.get("commit_attempt")
        if not attempt:
            continue
        key = (state["channel_id"], attempt["submitter_id"])
        if key in PENDING:
            continue
        try:
            channel = bot.get_channel(state["channel_id"]) or await bot.fetch_channel(state["channel_id"])
            prompt = await channel.fetch_message(attempt["prompt_id"])
            session = PENDING[key] = {
                "batch": state["batch"], "channel_id": state["channel_id"],
                "payer": SimpleNamespace(name=state["payer"], id=state["payer_id"]),
                "poster": SimpleNamespace(name=state.get("poster") or state["payer"],
                                          id=state.get("poster_id", state["payer_id"])),
                "lock": asyncio.Lock(), "guild": channel.guild, "drafts": [prompt], "timer": None,
                "state": state, "writes": attempt["writes"], "shots": [], "write_started": True,
            }
            await confirm_draft(key, session, prompt.id, True)
        except Exception as e:
            print(f"[ERROR] 無法接續結算 {state['batch']} 的回寫:", repr(e))


async def recover_appends():
    """重啟後接續已按 ✅ 且已存檔的追加。同批次、相同金額的寫入可以安全地重送。"""
    for state in list(SETTLEMENTS.values()):
        attempt = state.get("append_attempt")
        if not attempt:
            continue
        key = (state["channel_id"], attempt["submitter_id"])
        if key in PENDING:
            continue
        try:
            channel = bot.get_channel(state["channel_id"]) or await bot.fetch_channel(state["channel_id"])
            prompt = await channel.fetch_message(attempt["prompt_id"])
            session = PENDING[key] = {
                "append_to": state["batch"], "batch": state["batch"], "channel_id": state["channel_id"],
                "payer": SimpleNamespace(name=state["payer"], id=state["payer_id"]),
                "poster": SimpleNamespace(name=state.get("poster") or state["payer"],
                                          id=state.get("poster_id", state["payer_id"])),
                "lock": asyncio.Lock(), "guild": channel.guild, "drafts": [prompt], "timer": None,
                "state": state, "writes": attempt["writes"], "shots": [], "write_started": True,
                "added_units": attempt["added_units"], "append_notes": attempt["notes"],
                "base_revision": attempt["base_revision"],
            }
            async with session["lock"]:
                await confirm_append(key, session)
        except Exception as e:
            print(f"[ERROR] 無法接續結算 {state['batch']} 的追加:", repr(e))


# -------------------------------------------------
#  主訊息處理：買 / ~刪除 / 結算 / 認領
# -------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    # 不處理任何 bot（包含自己），避免和其他 bot 互相觸發
    if message.author.bot:
        return

    # 如果有設定 ALLOWED_CHANNEL_IDS，就只處理這些頻道
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    text = message.content.strip()

    # ============================================
    # 0) 結算（附下單截圖）／認領 unclassified 的商品
    # ============================================
    has_image = any((a.content_type or "").startswith("image/") for a in message.attachments)
    append_to = replied_settlement(message) if has_image and not text.startswith("買") else None
    adding = has_image and (message.channel.id, message.author.id) in PENDING and not text.startswith("買")
    if SETTLE_RE.match(text) or adding or append_to is not None:
        await handle_settle(message, append_to=append_to)
        await bot.process_commands(message)
        return

    claim_match = CLAIM_RE.match(text)
    if claim_match:
        await handle_claim(message, claim_numbers(text))
        await bot.process_commands(message)
        return

    if REASSIGN_RE.match(text):
        await handle_reassign(message, claim_numbers(re.sub(r"^(改歸屬|改)", "", text)))
        await bot.process_commands(message)
        return

    # 多筆刪除的編號。要排在「刪除」送 LLM 之前，不然「刪除 2」會被當成商品名稱。
    pick = delete_pick_number(text, message)
    if pick is not None:
        await confirm_delete_choice(message, pick)
        await bot.process_commands(message)
        return

    # ============================================
    # 1) ~刪除：舊格式快速通道（用網址辨識，不經過 LLM）
    # ============================================
    if text.startswith("~刪除"):
        url_to_delete = extract_first_url(text)
        if not url_to_delete:
            await message.reply(
                "看不到要刪除的連結 QQ\n"
                "請用例如：`~刪除 https://e.tb.cn/h.xxx`，"
                "或直接用自然語言，例如：`刪除電動牙刷`。"
            )
        else:
            await send_delete(message, url_to_delete, "", text)
        await bot.process_commands(message)
        return

    # ============================================
    # 2) 自然語言：「買」開頭 → 新增；帶刪除意圖 → 刪除
    #    其他訊息不送 LLM，直接略過
    # ============================================
    if not (text.startswith("買") or DELETE_INTENT_RE.search(text)):
        await bot.process_commands(message)
        return

    # 先給使用者一個「處理中」的反應
    await safe_react(message, "⏳")

    parsed = await parse_with_llm(text)

    if parsed is None:
        await safe_react(message, "⏳", remove=True)
        await message.reply(
            "AI 解析服務暫時忙碌，請稍後再試一次 🔄\n"
            "（如果持續失敗，可能是 API 額度用完了）"
        )
        await bot.process_commands(message)
        return

    # ---- 刪除 ----
    if parsed["action"] == "delete":
        # regex 優先、LLM 只當備援：LLM 抽的網址會被截斷，新增和刪除兩次抽的
        # 結果不一定一樣，那樣就永遠對不上同一筆
        url = extract_first_url(text) or parsed["url"]
        item_name = parsed["itemName"]

        if not url and not item_name:
            await safe_react(message, "⏳", remove=True)
            await message.reply(
                "我看不出來你要刪掉哪個商品 QQ\n"
                "請試試：`刪除電動牙刷` 或 `~刪除 https://...`"
            )
        else:
            await send_delete(message, url, item_name, text)
            await safe_react(message, "⏳", remove=True)
        await bot.process_commands(message)
        return

    # ---- 非購物訊息 ----
    if parsed["action"] != "add" or not parsed["itemName"]:
        await safe_react(message, "⏳", remove=True)
        # 只有明確用「買」開頭的訊息才回覆格式提示，其他默默略過
        if text.startswith("買"):
            await message.reply(
                "我沒辦法從訊息中提取到商品名稱 QQ\n"
                "請試試類似這樣的格式：\n"
                "`買 兩台白色的小米風扇 https://...`\n"
                "`買 【淘宝】... https://e.tb.cn/h.xxx 「電動牙刷」`"
            )
        await bot.process_commands(message)
        return

    item_name = parsed["itemName"]
    quantity = parsed["quantity"]
    model_spec = parsed["modelSpec"]

    # regex 優先、LLM 只當備援（同上：LLM 會把長網址截斷成點不開的連結）
    url = extract_first_url(text) or parsed["url"]

    if not url:
        await safe_react(message, "⏳", remove=True)
        await message.reply(
            "我在訊息裡找不到購物網址 QQ\n"
            "請確認有貼上 https:// 開頭的購物連結。"
        )
        await bot.process_commands(message)
        return

    payload = {
        "action": "add",
        "shareText": text,
        "itemName": item_name,
        "quantity": quantity,
        "modelSpec": model_spec,
        "url": url,
        "senderId": str(message.author.id),
        "senderName": message.author.name,
    }
    await send_add(message, payload)
    await safe_react(message, "⏳", remove=True)

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    get_store()  # 缺設定就在這裡停下來，不要等到有人用才發現
    load_settlements()
    bot.run(DISCORD_TOKEN)
