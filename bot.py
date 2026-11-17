import os
import re
import json
import time
import base64
import asyncio
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

import settle

# -------------------------------------------------
#  載入環境變數 (.env)
# -------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
ALLOWED_CHANNEL_IDS_RAW = os.getenv("ALLOWED_CHANNEL_IDS", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not DISCORD_TOKEN or not N8N_WEBHOOK_URL:
    raise RuntimeError("缺少必要的環境變數：DISCORD_TOKEN 或 N8N_WEBHOOK_URL")

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


# n8n 要等 Google Sheets 寫完才回應，實測單次 4.5~10.8 秒
N8N_READ_TIMEOUT = 30


async def post_to_n8n(payload: dict, url: str | None = None):
    """在 thread 裡送 webhook，避免卡住 Discord 的 event loop。"""
    return await asyncio.to_thread(
        requests.post, url or N8N_WEBHOOK_URL, json=payload, timeout=(5, N8N_READ_TIMEOUT)
    )


def n8n_failure_reply(verb: str, done: str, err: Exception) -> str:
    """讀取逾時不等於失敗：請求已送達 n8n，Sheets 通常還是會寫完。"""
    if isinstance(err, requests.exceptions.ReadTimeout):
        return (
            f"{verb}請求已送出，但 n8n 超過 {N8N_READ_TIMEOUT} 秒沒回應 ⏱️\n"
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

# 結算時讀截圖用的視覺模型。預設走 Groq；設了 VISION_BASE_URL 就改用那個
# OpenAI 相容的服務（例如本地的 Ollama），截圖就不會離開自己的機器。
VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
VISION_BASE_URL = os.getenv("VISION_BASE_URL")
vision_client = (
    AsyncOpenAI(base_url=VISION_BASE_URL, api_key=os.getenv("VISION_API_KEY", "none"))
    if VISION_BASE_URL
    else groq_client
)
N8N_SETTLE_WEBHOOK_URL = os.getenv("N8N_SETTLE_WEBHOOK_URL", N8N_WEBHOOK_URL + "-settle")
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


async def safe_react(message: discord.Message, emoji: str, remove: bool = False):
    """加/移除反應失敗（缺權限、網路暫時不通…）時不要讓整個訊息處理中斷。"""
    try:
        if remove:
            await message.remove_reaction(emoji, bot.user)
        else:
            await message.add_reaction(emoji)
    except Exception as e:
        print(f"[WARN] 無法{'移除' if remove else '加上'}反應 {emoji}: {e}")


# (頻道, 使用者) → 還沒選編號的刪除選項。只在記憶體，重啟就消失。
DELETE_CHOICES: dict[tuple[int, int], dict] = {}
DELETE_CHOICE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
DELETE_CHOICE_SECONDS = 600
DELETE_CHOICE_SEQ = 0
# 「刪除 2」「第2個」「2」；純數字只在回覆選項那則訊息時算數
DELETE_PICK_RE = re.compile(r"^(?:(?:刪除|刪掉|移除|選)\s*)?(?:第\s*)?#?\s*(\d{1,2})\s*(?:個)?$")


def delete_choice_lock(key: tuple[int, int]) -> asyncio.Lock:
    return DELETE_CHOICE_LOCKS.setdefault(key, asyncio.Lock())


def delete_pick_number(text: str, message: discord.Message) -> int | None:
    """有待選的刪除時，认出選編號。沒有待選就回 None，讓原本的刪除流程繼續。"""
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


def _drop_delete_choice(key: tuple[int, int], choice_id: int | None):
    pending = DELETE_CHOICES.get(key)
    if pending and choice_id is not None and pending["id"] == choice_id:
        DELETE_CHOICES.pop(key, None)


async def offer_delete_choices(message: discord.Message, choices: list[dict]):
    """多筆對得上時列出編號。新的一輪會取代舊的，編號用只增不減的序號。"""
    global DELETE_CHOICE_SEQ
    key = (message.channel.id, message.author.id)
    lines = []
    for choice in choices:
        extra = f"（{choice['spec']}）" if choice.get("spec") else ""
        qty = choice.get("qty")
        qty_text = f" ×{qty}" if qty not in ("", None) else ""
        lines.append(f"{choice['n']}. 「{choice['itemName']}」{extra}{qty_text}")
    prompt = await message.reply(
        "你有好幾筆對得上，我先不刪。回覆這則訊息的編號，或打「刪除 2」：\n" + "\n".join(lines)
    )
    async with delete_choice_lock(key):
        DELETE_CHOICE_SEQ += 1
        DELETE_CHOICES[key] = {
            "id": DELETE_CHOICE_SEQ,
            "expires": time.monotonic() + DELETE_CHOICE_SECONDS,
            "choices": choices,
            "prompt_id": prompt.id,
        }


async def confirm_delete_choice(message: discord.Message, number: int):
    key = (message.channel.id, message.author.id)
    async with delete_choice_lock(key):
        pending = DELETE_CHOICES.get(key)
        if not pending or pending["expires"] <= time.monotonic():
            DELETE_CHOICES.pop(key, None)
            await message.reply("這次的刪除選項已經過期，請重新說一次要刪什麼。")
            return
        choices = pending["choices"]
        if not 1 <= number <= len(choices):
            await message.reply(f"請回 1 到 {len(choices)} 的編號。")
            return
        choice = choices[number - 1]
        choice_id = pending["id"]
        row_number, sheet = choice["row_number"], choice["sheet"]
        # 列號可能在等他選的這段時間位移，所以連選項當時的內容一起送，讓 n8n 核對是不是同一筆
        expect = {"itemName": choice.get("itemName", ""), "spec": choice.get("spec", ""),
                  "qty": choice.get("qty", ""), "url": choice.get("url", "")}
    await send_delete(
        message, "", "", message.content,
        row_number=row_number, sheet=sheet, choice_id=choice_id, expect=expect,
    )


async def send_delete(
    message: discord.Message,
    url: str,
    item_name: str,
    full_text: str,
    row_number: str | int = "",
    sheet: str = "",
    choice_id: int | None = None,
    expect: dict | None = None,
):
    """送刪除請求到 n8n 並回覆結果。可用網址、商品名稱，或使用者選好的列號。"""
    key = (message.channel.id, message.author.id)
    if choice_id is None:
        pending = DELETE_CHOICES.get(key)
        choice_id = pending["id"] if pending else None
    stamp = getattr(message, "created_at", None)
    payload = {
        "action": "delete",
        "url": url,
        "itemName": item_name,
        "fullText": full_text,
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": stamp.isoformat() if stamp is not None else "",
    }
    if row_number != "" and row_number is not None:
        payload["rowNumber"] = row_number
        payload["sheet"] = sheet
        payload["expect"] = expect

    try:
        resp = await post_to_n8n(payload)
        body_text = getattr(resp, "text", "") or ""
        print("Sent DELETE to n8n:", resp.status_code, body_text[:200])
        body = {}
        try:
            body = resp.json() or {}
        except Exception:
            body = {}

        if resp.status_code == 404 and body.get("status") == "choose" and body.get("choices"):
            await offer_delete_choices(message, body["choices"])
            return
        if resp.status_code == 404 and body.get("status") == "changed":
            _drop_delete_choice(key, choice_id)
            await message.reply(
                "清單在你選的這段時間有變動，我不確定你選的是哪一筆，所以**沒有刪任何東西**。\n"
                "請重新說一次要刪什麼。"
            )
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
            await message.reply("刪除請求傳到 n8n 失敗 QQ")
    except Exception as e:
        print("Error sending delete to n8n:", e)
        await message.reply(n8n_failure_reply("刪除", "從表格刪掉", e))


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
        SETTLEMENTS.update(json.loads((Path(STATE_DIR) / "settlements.json").read_text(encoding="utf-8")))


def save_settlements():
    if not STATE_DIR:
        return True
    for old in list(SETTLEMENTS)[:-30]:
        if not SETTLEMENTS[old].get("append_attempt"):
            del SETTLEMENTS[old]
    try:
        tmp = Path(STATE_DIR) / "settlements.json.tmp"
        tmp.write_text(json.dumps(SETTLEMENTS, ensure_ascii=False), encoding="utf-8")
        tmp.replace(Path(STATE_DIR) / "settlements.json")
        return True
    except OSError as e:
        print("[WARN] 結算紀錄存檔失敗（重啟後會無法認領）:", e)
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


async def read_screenshot(data: bytes, mime: str) -> dict:
    """讀一張截圖。模型偶爾回傳讀不完整或不是 JSON 的結果，這時重讀一次，取比較完整的那次。"""
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    best = None
    for _ in range(2):
        try:
            raw = await llm_json(
                vision_client,
                model=VISION_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": settle.EXTRACT_PROMPT},
                    {"type": "image_url", "image_url": {"url": url}},
                ]}],
            )
        except json.JSONDecodeError as e:
            print("[WARN] 讀圖回傳的不是 JSON，重讀:", e)
            raw = {}
        shot = settle.clean_shot(raw)
        # 讀到的商品多的優先；一樣多就留「看到的東西」多的那次，讀不完整的件數才不會被另一次蓋掉而默默不見
        seen = lambda x: (len(x["items"]), len(x["items"]) + x["dropped"])
        if best is None or seen(shot) > seen(best):
            best = shot
        if shot["items"] and not shot["dropped"]:
            break
    return best


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

    resp = await post_to_n8n({"action": "list"}, N8N_SETTLE_WEBHOOK_URL)
    if not resp.ok:
        raise SettleError(f"讀不到購物清單 QQ（n8n 回 {resp.status_code}）")
    rows = resp.json()["rows"]
    if base is not None:
        used = {row for u in base["units"] for it in u["items"] for row in it.get("rows", [])}
        rows = [r for r in rows if settle.row_id(r) not in used]

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


async def send_chunks(target: discord.Message, text: str, ping=False, register: dict | None = None) -> list:
    # 只 ping 人：商品名稱是從截圖讀來的，不能讓它觸發 @everyone 或身分組
    allowed = discord.AllowedMentions(everyone=False, roles=False, users=ping, replied_user=False)
    chunks = settle.split_message(text)
    sent = []
    for i, chunk in enumerate(chunks):
        message = (await target.reply(chunk, allowed_mentions=allowed) if i == 0
                   else await target.channel.send(chunk, allowed_mentions=allowed))
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
                sent = await send_chunks(message, text)
                old = session["drafts"]
                session.update(shots=shots, state=state, writes=writes, drafts=sent, **details)
            for draft in old:
                try:
                    await draft.delete()
                except Exception as e:
                    print("[WARN] 刪不掉舊草稿:", e)
            await safe_react(sent[-1], "✅")
            await safe_react(sent[-1], "❌")
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
        resp = await post_to_n8n(
            {"action": "settle", "batch": state["batch"], "rows": writes}, N8N_SETTLE_WEBHOOK_URL
        )
    except Exception as e:
        print("Error sending settle to n8n:", e)
        await prompt.reply(n8n_failure_reply("結算", "寫進表格", e))
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
        await prompt.reply(f"結算結果寫進表格失敗 QQ（n8n 回 {resp.status_code}），請稍後再試。")
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
            state["append_attempt"] = {
                "added_units": deepcopy(session["added_units"]), "writes": deepcopy(session["writes"]),
                "notes": list(session["append_notes"]), "base_revision": session["base_revision"],
                "prompt_id": prompt.id, "submitter_id": key[1],
            }
            if not save_settlements():
                state.pop("append_attempt")
                await prompt.reply("追加確認紀錄存檔失敗，請稍後按 ✅ 重試。")
                return
        session["write_started"] = True
        outcome = await write_settlement(prompt, combined, session["writes"], retrying=bool(attempt))
        if outcome:
            if outcome == "conflict" and not attempt:
                state.pop("append_attempt", None)
                save_settlements()
                discard_draft(key, session)
            else:
                restart_timer(key, session)
                await prompt.reply("追加草稿仍保留，稍後請移除再加上 ✅ 重試同一批追加。")
            return

        combined["notes"] = list(state.get("notes") or []) + session["append_notes"]
        combined["append_revision"] = state.get("append_revision", 0) + 1
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
            sent = await send_chunks(prompt, text, ping=people, register=state)
            await safe_react(sent[-1], CLOSE_EMOJI)
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
        if await write_settlement(prompt, state, writes):
            discard_draft(key, session)
            return

        state["mentions"] = await member_mentions(session["guild"], state)
        SETTLEMENTS[state["batch"]] = state
        save_settlements()
        discard_draft(key, session)
        sent = await send_chunks(prompt, settle.render(state, final=True, mentions=state["mentions"]), ping=True, register=state)
        if settle.has_unclassified(state):
            await safe_react(sent[-1], CLOSE_EMOJI)


def claim_numbers(text: str) -> list[int]:
    """「認領 3 5、7 9-11」→ [3, 5, 7, 9, 10, 11]。@ 人的 <@id> 裡的數字不算。"""
    rest = re.sub(r"<[@#][!&]?\d+>", " ", text)
    nos = []
    for m in re.finditer(r"#?(\d+)(?:\s*[-~～–至到]\s*#?(\d+))?", rest):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        nos += range(min(a, b), max(a, b) + 1) if abs(a - b) < 100 else [a, b]
    return list(dict.fromkeys(nos))


async def handle_claim(message: discord.Message, nos: list[int]):
    if len(nos) > MAX_CLAIMS:
        await message.reply(f"一次最多認領 {MAX_CLAIMS} 件，請分幾次打 🙏")
        return

    pool = [s for s in SETTLEMENTS.values() if s["channel_id"] == message.channel.id]
    replied_to = message.reference.message_id if message.reference else None
    replied = next((s for s in pool if replied_to in s["message_ids"]), None)

    def find_state(no):
        if replied:
            return replied
        open_item = lambda s: (lambda it: it is not None and settle.is_unclassified(it))(settle.find_item(s, no)[1])
        # 有這個編號但不能領（已經有主）的，交給 claim() 說明原因
        return (next((s for s in reversed(pool) if open_item(s)), None)
                or next((s for s in reversed(pool) if not s.get("closed") and settle.find_item(s, no)[1]), None))

    target = named_user(message) or message.author
    claimed: dict[str, list[int]] = {}  # batch → 認領成功的編號
    failed = []
    for no in nos:
        state = find_state(no)
        if state is None:
            error = f"找不到還沒人認領的 #{no} 🙅"
        else:
            async with settlement_lock(state["batch"]):
                error = settle.claim(state, no, target.name)
                if not error:
                    state["mentions"][target.name] = target.mention
                    save_settlements()
        if error:
            failed.append(error)
        else:
            claimed.setdefault(state["batch"], []).append(no)

    if not claimed:
        if len(failed) == 1 and failed[0].startswith("找不到"):
            failed[0] += "\n要認領的話，請直接回覆在那則結算訊息底下。"
        await message.reply("\n".join(failed) if len(failed) == 1 else "沒有認領到任何一件：\n" + "\n".join(f"- {e}" for e in failed))
        return

    for i, (batch, done) in enumerate(claimed.items()):
        state = SETTLEMENTS[batch]
        state["mentions"][target.name] = target.mention
        items = [settle.find_item(state, no)[1] for no in done]
        if len(items) == 1:
            head = f"#{done[0]} {items[0]['name']} 算在 {target.mention} 名下 ✅"
        else:
            head = f"{len(items)} 件算在 {target.mention} 名下 ✅\n" + "\n".join(f"#{it['no']} {it['name']}" for it in items)
        if failed and i == len(claimed) - 1:
            head += "\n\n沒有認領到：\n" + "\n".join(f"- {e}" for e in failed)
        tail = (
            f"{state['payer']} 按 🏁 可以結束認領，剩下的算 {state['payer']} 自己的"
            if settle.has_unclassified(state)
            else f"全部都有人認領了，{state['payer']} 按 🏁 公布最終金額並通知大家"
        )
        text = f"{head}\n\n**結算 {batch} 更新後：**\n{settle.render_totals(state, state['mentions'])}\n\n{tail}"
        chunks = settle.split_message(text)
        reply = await message.reply(chunks[0], allowed_mentions=discord.AllowedMentions(users=[target]))
        for chunk in chunks[1:]:
            reply = await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        state["message_ids"].append(reply.id)  # 回覆這則也能找到同一次結算，在這則按 🏁 也有效
        await safe_react(reply, CLOSE_EMOJI)
    save_settlements()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """草稿上的 ✅／❌ 確認或取消；結算訊息上的 🏁 結束認領。

    用 raw 事件：bot 重啟後、訊息不在快取裡也收得到。
    """
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

    async with settlement_lock(state["batch"]):
        if state.get("closed"):
            return
        if state.get("append_attempt"):
            channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
            await channel.send("這筆結算的追加已確認，表格寫入結果仍待確認，請先在追加草稿按 ✅ 重試完成，再按 🏁。")
            return
        settle.close(state)
        save_settlements()
        try:
            channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
            ref = discord.MessageReference(message_id=payload.message_id, channel_id=payload.channel_id, fail_if_not_exists=False)
            allowed = discord.AllowedMentions(everyone=False, roles=False, users=True)
            for i, chunk in enumerate(settle.split_message(settle.render_closed(state, state["mentions"]))):
                await channel.send(chunk, reference=ref if i == 0 else None, allowed_mentions=allowed)
        except Exception as e:
            print(f"[ERROR] 結算 {state['batch']} 已結束認領，但公布訊息送不出去:", repr(e))


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ALLOWED_CHANNEL_IDS:
        print(f"Allowed channel IDs: {sorted(ALLOWED_CHANNEL_IDS)}")
    else:
        print("Allowed channel IDs: 不限制（所有頻道都會處理）")
    print(f"LLM: Groq {GROQ_MODEL} ✓")
    print(f"結算: 讀圖 {VISION_MODEL} @ {VISION_BASE_URL or 'Groq'}，已載入 {len(SETTLEMENTS)} 筆結算紀錄")
    await recover_appends()


async def recover_appends():
    """重啟後接續已按 ✅ 且已存檔的追加，n8n 會接受同批次相同金額的重試。"""
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
        "fullText": text,
        "shareText": text,
        "itemName": item_name,
        "quantity": quantity,
        "modelSpec": model_spec,
        "url": url,
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": message.created_at.isoformat(),
    }

    try:
        resp = await post_to_n8n(payload)
        print("Sent ADD to n8n:", resp.status_code, resp.text[:200])

        await safe_react(message, "⏳", remove=True)

        if resp.ok:
            summary_parts = [f"{quantity} 件"]
            if model_spec:
                summary_parts.append(model_spec)
            summary_parts.append(f"「{item_name}」")
            summary = " ".join(summary_parts)
            await message.reply(f"已把 {summary} 加入購物清單 🧾")
        else:
            await message.reply("新增請求傳到 n8n 失敗 QQ")
    except Exception as e:
        print("Error sending add to n8n:", e)
        await safe_react(message, "⏳", remove=True)
        await message.reply(n8n_failure_reply("新增", "寫進表格", e))

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    load_settlements()
    bot.run(DISCORD_TOKEN)
