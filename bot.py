import os
import re
import json
import asyncio
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

# -------------------------------------------------
#  載入環境變數 (.env)
#  測試時可以用另一份設定：ENV_FILE=.env.test python3 bot.py
# -------------------------------------------------
ENV_FILE = os.getenv("ENV_FILE", ".env")
load_dotenv(ENV_FILE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
ALLOWED_CHANNEL_IDS_RAW = os.getenv("ALLOWED_CHANNEL_IDS", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# 選填：只有這些人能結單。留空＝任何人都能結單（靠 ✅ 二次確認防誤觸）
ORDER_MANAGER_IDS_RAW = os.getenv("ORDER_MANAGER_IDS", "")

if not DISCORD_TOKEN or not N8N_WEBHOOK_URL:
    raise RuntimeError("缺少必要的環境變數：DISCORD_TOKEN 或 N8N_WEBHOOK_URL")

if not GROQ_API_KEY:
    raise RuntimeError("缺少必要的環境變數：GROQ_API_KEY")


def parse_id_set(raw: str, label: str) -> set[int]:
    """把逗號分隔的 ID 字串解析成 int set（空字串代表不限制）。"""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            print(f"[WARN] 無法解析{label}：{part!r}（已略過）")
    return ids


ALLOWED_CHANNEL_IDS = parse_id_set(ALLOWED_CHANNEL_IDS_RAW, "頻道 ID")
ORDER_MANAGER_IDS = parse_id_set(ORDER_MANAGER_IDS_RAW, "結單負責人 ID")

# 抓第一個網址用的 regex（不吃空白、CJK 字元與全形標點——淘寶/京東分享文常把網址和中文擠在一起）
URL_REGEX = re.compile(r"https?://[^\s　-〿一-鿿＀-￯「-』]+")

# 網址結尾常見的標點（regex 可能把它們一起吃進來，要剝掉）
URL_TRAILING_PUNCT = ".,;:!?)]>\"'"

# Discord 單則訊息上限 2000 字，留一點餘裕
DISCORD_CHUNK_LIMIT = 1900

# 結單確認用
CONFIRM_YES = "✅"
CONFIRM_NO = "❌"
CLOSE_CONFIRM_TIMEOUT = 60


def extract_first_url(text: str) -> str | None:
    """從文字中抓出第一個網址，並剝掉結尾誤吃的標點符號。"""
    match = URL_REGEX.search(text)
    if not match:
        return None
    return match.group(0).rstrip(URL_TRAILING_PUNCT)


def safe_quantity(value) -> int:
    """把 LLM 回傳的數量安全轉成正整數，失敗一律回 1。"""
    try:
        qty = int(float(value))
    except (TypeError, ValueError):
        return 1
    return qty if qty > 0 else 1


def safe_price(value) -> float:
    """把 LLM 回傳的價格安全轉成正數，失敗或非正數一律回 0。"""
    try:
        price = float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0
    return price if price > 0 else 0.0


def split_message(content: str, limit: int = DISCORD_CHUNK_LIMIT) -> list[str]:
    """把長訊息按行切成多段，避免超過 Discord 的字數上限。"""
    chunks: list[str] = []
    current = ""

    for line in content.split("\n"):
        # 單一行就超長時只能硬切
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)
    return chunks or [""]


async def post_to_n8n(payload: dict):
    """在 thread 裡送 webhook，避免卡住 Discord 的 event loop。"""
    return await asyncio.to_thread(
        requests.post, N8N_WEBHOOK_URL, json=payload, timeout=15
    )

# -------------------------------------------------
#  初始化 Groq（OpenAI 相容 API）
# -------------------------------------------------
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)
# Llama 3.3 70B 已於 2026-08-16 對免費／Developer 方案下線。
# 官方替代：openai/gpt-oss-120b（正式版）或 qwen/qwen3.6-27b（預覽，中文較強）
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MAX_RETRIES = 3

SYSTEM_PROMPT = (
    "你是一個團購購物清單助手。使用者會在 Discord 傳訊息，你要判斷意圖並提取資訊。\n"
    "請嚴格回傳以下 JSON 格式：\n"
    '{"action": "add", "itemName": "", "quantity": 1, "modelSpec": "", "url": "", '
    '"unitPrice": 0, "totalPrice": 0, "batch": ""}\n\n'
    "action 只能是這幾種：\n"
    '- "add"：想買、要加進清單（例如：買 兩台白色的小米風扇 https://...）\n'
    '- "delete"：想把還沒下單的商品從清單移除（例如：刪除電動牙刷、風扇不用買了）\n'
    '- "close"：結單，把目前整份清單拿去下單（例如：結單、可以下單了、我要去下單了）\n'
    '- "price"：回報某商品實際多少錢（例如：價格 電動牙刷 120、牙刷一個 120、牙刷總共 240）\n'
    '- "list"：想看目前清單（例如：清單、目前有什麼、還有什麼要買）\n'
    '- "bill"：想算錢、分帳（例如：分帳、帳單、大家要付多少、對帳）\n'
    '- "other"：跟購物清單無關，或無法判斷\n\n'
    "欄位規則：\n"
    "- itemName：商品名稱，add / delete / price 會用到。"
    "如果訊息包含「...」格式的商品名，優先使用。沒有就留空字串。\n"
    "- quantity：數量，必須是整數。中文數字要轉成阿拉伯數字（兩台 → 2）。預設 1。\n"
    "- modelSpec：型號、規格、顏色等描述（如 白色、256GB）。沒有就留空字串。\n"
    "- url：訊息中的網址（https:// 開頭）。沒有就留空字串，要注意不該有空格。\n"
    "- unitPrice：price 時的「單價」，也就是一個多少錢。沒有明說單價就填 0。\n"
    "- totalPrice：price 時的「總價」，也就是總共多少錢。沒有明說總價就填 0。\n"
    "  例：「牙刷一個 120」→ unitPrice=120、totalPrice=0；\n"
    "      「牙刷總共 240」→ unitPrice=0、totalPrice=240；\n"
    "      「價格 牙刷 120」→ 當成單價，unitPrice=120、totalPrice=0。\n"
    "- batch：bill 時如果使用者指定了批次（例如 20260910-1）就填，否則留空字串。\n"
    "- 資訊缺失請留空字串，數量預設 1、價格預設 0。\n"
    "- 只輸出 JSON，不要有其他解釋文字。"
)

VALID_ACTIONS = {"add", "delete", "close", "price", "list", "bill", "other"}

# 訊息不是「買」開頭時，要命中這些關鍵字才會送 LLM 判斷（省 API 額度、也避免誤觸）
INTENT_RE = re.compile(
    r"刪除|刪掉|移除|取消|不用買|不想買|不買了|別買|退掉"  # delete
    r"|結單|關單|下單了|可以下單|要下單|開始下單"  # close
    r"|價格|價錢|單價|總價|多少錢|花了|付了"  # price
    r"|清單|列表|還有什麼|目前有什麼"  # list
    r"|分帳|帳單|對帳|算錢|付多少|收錢"  # bill
)


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
                max_tokens=1024,
                extra_body={"reasoning_effort": "low"},
            )
            raw_text = response.choices[0].message.content.strip()
            parsed = json.loads(raw_text)

            action = str(parsed.get("action", "")).lower()
            if action not in VALID_ACTIONS:
                action = "other"

            return {
                "action": action,
                "itemName": str(parsed.get("itemName", "")).strip(),
                "quantity": safe_quantity(parsed.get("quantity")),
                "modelSpec": str(parsed.get("modelSpec", "")).strip(),
                "url": str(parsed.get("url", "")).strip(),
                "unitPrice": safe_price(parsed.get("unitPrice")),
                "totalPrice": safe_price(parsed.get("totalPrice")),
                "batch": str(parsed.get("batch", "")).strip(),
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
    """加/移除反應失敗（例如缺權限）時不要讓整個訊息處理中斷。"""
    try:
        if remove:
            await message.remove_reaction(emoji, bot.user)
        else:
            await message.add_reaction(emoji)
    except discord.HTTPException as e:
        print(f"[WARN] 無法{'移除' if remove else '加上'}反應 {emoji}: {e}")


async def reply_long(message: discord.Message, content: str) -> discord.Message:
    """回覆可能很長的內容（清單／結單摘要會超過 2000 字），回傳最後送出的訊息。"""
    content = (content or "").strip() or "（n8n 沒有回內容，請看一下 log）"
    sent = message
    for i, chunk in enumerate(split_message(content)):
        if i == 0:
            sent = await message.reply(chunk)
        else:
            sent = await message.channel.send(chunk)
    return sent


def base_payload(message: discord.Message) -> dict:
    """所有 action 共用的欄位。日期時間用本地時區，批次號才不會跨日錯亂。"""
    local = message.created_at.astimezone()
    return {
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": message.created_at.isoformat(),
        "localDate": local.strftime("%Y%m%d"),
        "localTime": local.strftime("%Y-%m-%d %H:%M"),
    }


async def call_n8n(message: discord.Message, extra: dict) -> dict | None:
    """送一個 action 給 n8n，回傳 {status, message}；失敗回 None。"""
    payload = {**base_payload(message), **extra}
    try:
        resp = await post_to_n8n(payload)
        print(f"Sent {payload.get('action')} to n8n:", resp.status_code, resp.text[:200])
        if not resp.ok:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        print(f"Error sending {payload.get('action')} to n8n:", e)
        return None


async def run_action(message: discord.Message, extra: dict, fallback: str = "完成 ✓"):
    """送 action 到 n8n 並把回覆貼回頻道。"""
    data = await call_n8n(message, extra)
    if data is None:
        await message.reply("跟 n8n 溝通時我撞牆了 QQ 請幫我看一下 log")
        return None
    await reply_long(message, data.get("message") or fallback)
    return data


async def handle_close(message: discord.Message):
    """結單：先給預覽，等發話者按 ✅ 才真的凍結整批。"""
    if ORDER_MANAGER_IDS and message.author.id not in ORDER_MANAGER_IDS:
        await message.reply("只有下單負責人可以結單喔 🙅")
        return

    # 先撈一次待下單清單當預覽，讓大家看清楚這批要結什麼
    preview = await call_n8n(message, {"action": "list"})
    if preview is None:
        await message.reply("跟 n8n 溝通時我撞牆了 QQ 請幫我看一下 log")
        return

    if preview.get("status") == "empty":
        await reply_long(message, preview.get("message") or "目前沒有待下單的商品 👌")
        return

    prompt = await reply_long(
        message,
        f"{preview.get('message', '')}\n\n"
        f"以上是這批要下單的內容。\n"
        f"確認結單請按 {CONFIRM_YES}，取消按 {CONFIRM_NO}（{CLOSE_CONFIRM_TIMEOUT} 秒內有效）。\n"
        f"⚠️ 結單後這批就不能再自己刪除了。",
    )

    await safe_react(prompt, CONFIRM_YES)
    await safe_react(prompt, CONFIRM_NO)

    def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
        return (
            user.id == message.author.id
            and reaction.message.id == prompt.id
            and str(reaction.emoji) in (CONFIRM_YES, CONFIRM_NO)
        )

    try:
        reaction, _ = await bot.wait_for(
            "reaction_add", timeout=CLOSE_CONFIRM_TIMEOUT, check=check
        )
    except asyncio.TimeoutError:
        await prompt.reply("等太久了，這次沒有結單 ⏱️（要結單請再打一次「結單」）")
        return

    if str(reaction.emoji) == CONFIRM_NO:
        await prompt.reply("好，這次不結單 👌")
        return

    await run_action(message, {"action": "close"}, fallback="結單完成 ✅")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"設定檔: {ENV_FILE}　webhook: {N8N_WEBHOOK_URL}")
    if ALLOWED_CHANNEL_IDS:
        print(f"Allowed channel IDs: {sorted(ALLOWED_CHANNEL_IDS)}")
    else:
        print("Allowed channel IDs: 不限制（所有頻道都會處理）")
    if ORDER_MANAGER_IDS:
        print(f"結單負責人: {sorted(ORDER_MANAGER_IDS)}")
    else:
        print("結單負責人: 不限制（任何人都能結單，靠 ✅ 二次確認）")
    print(f"LLM: Groq {GROQ_MODEL} ✓")


# -------------------------------------------------
#  主訊息處理
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
            await run_action(
                message,
                {"action": "delete", "url": url_to_delete, "itemName": "", "fullText": text},
            )
        await bot.process_commands(message)
        return

    # ============================================
    # 2) 自然語言：「買」開頭，或命中意圖關鍵字才送 LLM
    # ============================================
    if not (text.startswith("買") or INTENT_RE.search(text)):
        await bot.process_commands(message)
        return

    await safe_react(message, "⏳")
    parsed = await parse_with_llm(text)
    await safe_react(message, "⏳", remove=True)

    if parsed is None:
        await message.reply(
            "AI 解析服務暫時忙碌，請稍後再試一次 🔄\n"
            "（如果持續失敗，可能是 API 額度用完了）"
        )
        await bot.process_commands(message)
        return

    action = parsed["action"]
    url = parsed["url"] or extract_first_url(text) or ""

    # ---- 結單 ----
    if action == "close":
        await handle_close(message)

    # ---- 刪除 ----
    elif action == "delete":
        if not url and not parsed["itemName"]:
            await message.reply(
                "我看不出來你要刪掉哪個商品 QQ\n"
                "請試試：`刪除電動牙刷` 或 `~刪除 https://...`"
            )
        else:
            await run_action(
                message,
                {
                    "action": "delete",
                    "url": url,
                    "itemName": parsed["itemName"],
                    "fullText": text,
                },
            )

    # ---- 回填價格 ----
    elif action == "price":
        if not parsed["itemName"] and not url:
            await message.reply(
                "我看不出來這是哪個商品的價格 QQ\n"
                "請試試：`價格 電動牙刷 120`（單價）或 `電動牙刷總共 240`。"
            )
        else:
            await run_action(
                message,
                {
                    "action": "price",
                    "itemName": parsed["itemName"],
                    "url": url,
                    "unitPrice": parsed["unitPrice"],
                    "totalPrice": parsed["totalPrice"],
                    "fullText": text,
                },
            )

    # ---- 看清單 ----
    elif action == "list":
        await run_action(message, {"action": "list"})

    # ---- 分帳 ----
    elif action == "bill":
        await run_action(message, {"action": "bill", "batch": parsed["batch"]})

    # ---- 新增 ----
    elif action == "add":
        if not parsed["itemName"]:
            # 只有明確用「買」開頭的訊息才提示格式，其他默默略過
            if text.startswith("買"):
                await message.reply(
                    "我沒辦法從訊息中提取到商品名稱 QQ\n"
                    "請試試類似這樣的格式：\n"
                    "`買 兩台白色的小米風扇 https://...`\n"
                    "`買 【淘宝】... https://e.tb.cn/h.xxx 「電動牙刷」`"
                )
        elif not url:
            await message.reply(
                "我在訊息裡找不到購物網址 QQ\n"
                "請確認有貼上 https:// 開頭的購物連結。"
            )
        else:
            await run_action(
                message,
                {
                    "action": "add",
                    "fullText": text,
                    "shareText": text,
                    "itemName": parsed["itemName"],
                    "quantity": parsed["quantity"],
                    "modelSpec": parsed["modelSpec"],
                    "url": url,
                },
            )

    # ---- 其他：默默略過，只有「買」開頭才提示 ----
    elif text.startswith("買"):
        await message.reply(
            "我沒辦法從訊息中提取到商品名稱 QQ\n"
            "請試試類似這樣的格式：\n"
            "`買 兩台白色的小米風扇 https://...`\n"
            "`買 【淘宝】... https://e.tb.cn/h.xxx 「電動牙刷」`"
        )

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
