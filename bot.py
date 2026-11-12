import os
import re
import json
import base64
import asyncio
from datetime import datetime
from pathlib import Path

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


async def send_delete(message: discord.Message, url: str, item_name: str, full_text: str):
    """送刪除請求到 n8n 並回覆結果。可用網址或商品名稱其中之一辨識。"""
    payload = {
        "action": "delete",
        "url": url,
        "itemName": item_name,
        "fullText": full_text,
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": message.created_at.isoformat(),
    }

    try:
        resp = await post_to_n8n(payload)
        print("Sent DELETE to n8n:", resp.status_code, resp.text[:200])

        if resp.status_code == 200:
            deleted_name = ""
            try:
                deleted_name = str(resp.json().get("itemName", ""))
            except Exception:
                pass
            if deleted_name:
                await message.reply(f"已把「{deleted_name}」從購物清單刪掉 🗑️")
            else:
                await message.reply("已刪除你加進清單的那筆商品 🗑️")
        elif resp.status_code in (403, 404):
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
CLAIM_RE = re.compile(r"^(認領|认领)\s*#?\s*(\d+)")
SETTLE_CONFIRM_SECONDS = 600
CLOSE_EMOJI = "🏁"
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 3_000_000  # Groq 的 base64 圖片上限是 4MB，編碼後會膨脹三分之一
MATCH_CHUNK = 6  # 一次請 LLM 比對幾個商品；太多會超過每分鐘的 token 額度
LLM_RETRY_WAIT = 15
SETTLEMENTS: dict[str, dict] = {}  # batch → 結算狀態，照建立順序


class SettleError(Exception):
    """可以直接回給使用者看的錯誤。"""


def named_user(message: discord.Message):
    """內文裡 @ 的第一個真人。回覆別人時 Discord 會把對方也放進 mentions，所以要以內文為準。"""
    people = {u.id: u for u in message.mentions if not u.bot}
    return next((people[i] for i in message.raw_mentions if i in people), None)


def load_settlements():
    if STATE_DIR and (Path(STATE_DIR) / "settlements.json").exists():
        SETTLEMENTS.update(json.loads((Path(STATE_DIR) / "settlements.json").read_text(encoding="utf-8")))


def save_settlements():
    if not STATE_DIR:
        return
    for old in list(SETTLEMENTS)[:-30]:
        del SETTLEMENTS[old]
    try:
        tmp = Path(STATE_DIR) / "settlements.json.tmp"
        tmp.write_text(json.dumps(SETTLEMENTS, ensure_ascii=False), encoding="utf-8")
        tmp.replace(Path(STATE_DIR) / "settlements.json")
    except OSError as e:
        print("[WARN] 結算紀錄存檔失敗（重啟後會無法認領）:", e)


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
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    raw = await llm_json(
        vision_client,
        model=VISION_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": settle.EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
    )
    return settle.clean_shot(raw)


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


async def build_settlement(message: discord.Message, images: list, payer) -> tuple[dict, list[dict]]:
    shots = []
    for i, att in enumerate(images, 1):
        if att.size > MAX_IMAGE_BYTES:
            raise SettleError(f"第 {i} 張圖超過 3MB，請改傳手機截圖（不要傳原始相片檔）")
        shots.append(await read_screenshot(await att.read(), att.content_type.split(";")[0]))

    units, errors, notes = settle.merge_shots(shots)
    if errors:
        raise SettleError("截圖有問題，沒辦法結算：\n" + "\n".join(f"- {e}" for e in errors))
    if not units:
        raise SettleError(
            "我在截圖裡找不到商品和價格 QQ\n"
            "請傳淘寶「确认订单」頁或訂單詳情頁的截圖，要拍到台幣金額。"
        )

    resp = await post_to_n8n({"action": "list"}, N8N_SETTLE_WEBHOOK_URL)
    if not resp.ok:
        raise SettleError(f"讀不到購物清單 QQ（n8n 回 {resp.status_code}）")
    rows = resp.json()["rows"]

    payload = settle.match_payload(units, rows)
    writes, more = settle.assign(units, rows, payload, await match_rows(payload))
    state = {
        "batch": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "payer": payer.name,
        "payer_id": payer.id,
        "poster": "" if payer.id == message.author.id else message.author.name,
        "poster_id": message.author.id,
        "channel_id": message.channel.id,
        "units": units,
        "notes": notes + more,
        "market_rate": await market_rate(),
        "mentions": {},
        "message_ids": [],
    }
    return state, writes


async def send_chunks(target: discord.Message, text: str, ping: bool = False) -> list:
    # 只 ping 人：商品名稱是從截圖讀來的，不能讓它觸發 @everyone 或身分組
    allowed = discord.AllowedMentions(everyone=False, roles=False, users=ping)
    chunks = settle.split_message(text)
    sent = [await target.reply(chunks[0], allowed_mentions=allowed)]
    for chunk in chunks[1:]:
        sent.append(await target.channel.send(chunk, allowed_mentions=allowed))
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


async def handle_settle(message: discord.Message):
    images = [a for a in message.attachments if (a.content_type or "").startswith("image/")][:MAX_IMAGES]
    if not images:
        await message.reply("要結算的話，請把下單截圖和「結算」放在同一則訊息裡傳上來 🧾")
        return

    payer = named_user(message) or message.author  # 「結算 @B」＝幫 B 結算，大家要轉帳給 B
    await safe_react(message, "⏳")
    try:
        state, writes = await build_settlement(message, images, payer)
    except SettleError as e:
        await message.reply(str(e))
        return
    except Exception as e:
        print("Error building settlement:", repr(e))
        await message.reply("結算時我撞牆了 QQ 請幫我看一下 log")
        return
    finally:
        await safe_react(message, "⏳", remove=True)

    prompt = (await send_chunks(message, settle.render(state, final=False)))[-1]
    await safe_react(prompt, "✅")
    await safe_react(prompt, "❌")

    def can_confirm(reaction, user):
        return (
            reaction.message.id == prompt.id
            and user.id in (message.author.id, payer.id)
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=SETTLE_CONFIRM_SECONDS, check=can_confirm)
    except asyncio.TimeoutError:
        await prompt.reply("⌛ 太久沒有確認，這次結算已取消，表格沒有變動。")
        return
    if str(reaction.emoji) == "❌":
        await prompt.reply("已取消，表格沒有變動。")
        return

    if writes:
        try:
            resp = await post_to_n8n(
                {"action": "settle", "batch": state["batch"], "rows": writes}, N8N_SETTLE_WEBHOOK_URL
            )
        except Exception as e:
            print("Error sending settle to n8n:", e)
            await prompt.reply(n8n_failure_reply("結算", "寫進表格", e))
            return
        if resp.status_code == 409:
            problems = resp.json().get("problems", [])
            await prompt.reply(
                "表格在這幾分鐘內被改過，這次沒有寫入任何東西，請重新傳截圖結算一次：\n"
                + "\n".join(f"- {p['tab']} 第 {p['row']} 列：{p['reason']}" for p in problems[:10])
            )
            return
        if not resp.ok:
            await prompt.reply(f"結算結果寫進表格失敗 QQ（n8n 回 {resp.status_code}），表格沒有變動。")
            return

    state["mentions"] = await member_mentions(message.guild, state)
    sent = await send_chunks(prompt, settle.render(state, final=True, mentions=state["mentions"]), ping=True)
    state["message_ids"] = [m.id for m in sent]
    SETTLEMENTS[state["batch"]] = state
    save_settlements()
    if settle.has_unclassified(state):
        await safe_react(sent[-1], CLOSE_EMOJI)


async def handle_claim(message: discord.Message, no: int):
    def open_item(state):
        _, item = settle.find_item(state, no)
        return item is not None and settle.is_unclassified(item)

    pool = [s for s in SETTLEMENTS.values() if s["channel_id"] == message.channel.id]
    replied_to = message.reference.message_id if message.reference else None
    state = next((s for s in pool if replied_to in s["message_ids"]), None)
    if state is None:
        state = next((s for s in reversed(pool) if open_item(s)), None)
    if state is None:
        await message.reply(f"找不到還沒人認領的 #{no} 🙅\n要認領的話，請直接回覆在那則結算訊息底下。")
        return

    target = named_user(message) or message.author
    error = settle.claim(state, no, target.name)
    if error:
        await message.reply(error)
        return

    state["mentions"][target.name] = target.mention
    _, item = settle.find_item(state, no)
    tail = (
        f"{state['payer']} 按 🏁 可以結束認領，剩下的算 {state['payer']} 自己的"
        if settle.has_unclassified(state)
        else f"全部都有人認領了，{state['payer']} 按 🏁 公布最終金額並通知大家"
    )
    reply = await message.reply(
        f"#{no} {item['name']} 算在 {target.mention} 名下 ✅\n\n"
        f"**結算 {state['batch']} 更新後：**\n{settle.render_totals(state, state['mentions'])}\n\n{tail}",
        allowed_mentions=discord.AllowedMentions(users=[target]),
    )
    state["message_ids"].append(reply.id)  # 回覆這則也能找到同一次結算，在這則按 🏁 也有效
    save_settlements()
    await safe_react(reply, CLOSE_EMOJI)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """下單人在結算訊息上按 🏁 → 結束認領。用 raw 事件，bot 重啟後、訊息不在快取裡也收得到。"""
    if str(payload.emoji) != CLOSE_EMOJI or (bot.user and payload.user_id == bot.user.id):
        return
    state = next((s for s in SETTLEMENTS.values() if payload.message_id in s["message_ids"]), None)
    if state is None or state.get("closed"):
        return
    if payload.user_id not in (state["payer_id"], state.get("poster_id", state["payer_id"])):
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
    if SETTLE_RE.match(text):
        await handle_settle(message)
        await bot.process_commands(message)
        return

    claim_match = CLAIM_RE.match(text)
    if claim_match:
        await handle_claim(message, int(claim_match.group(2)))
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
