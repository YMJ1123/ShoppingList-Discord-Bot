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


async def post_to_n8n(payload: dict):
    """在 thread 裡送 webhook，避免卡住 Discord 的 event loop。"""
    return await asyncio.to_thread(
        requests.post, N8N_WEBHOOK_URL, json=payload, timeout=8
    )

# -------------------------------------------------
#  初始化 Groq（OpenAI 相容 API）
# -------------------------------------------------
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_MAX_RETRIES = 3

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
                max_tokens=512,
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
    """加/移除反應失敗（例如缺權限）時不要讓整個訊息處理中斷。"""
    try:
        if remove:
            await message.remove_reaction(emoji, bot.user)
        else:
            await message.add_reaction(emoji)
    except discord.HTTPException as e:
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
        await message.reply("刪除時我撞牆了 QQ 請幫我看一下 log")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ALLOWED_CHANNEL_IDS:
        print(f"Allowed channel IDs: {sorted(ALLOWED_CHANNEL_IDS)}")
    else:
        print("Allowed channel IDs: 不限制（所有頻道都會處理）")
    print(f"LLM: Groq {GROQ_MODEL} ✓")


# -------------------------------------------------
#  主訊息處理：買 / ~刪除
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
        url = parsed["url"] or extract_first_url(text) or ""
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
    url = parsed["url"]

    # 如果 LLM 沒抓到 URL，從原始訊息中再嘗試 regex 撈一次
    if not url:
        url = extract_first_url(text) or ""

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
        await message.reply("新增時我撞牆了 QQ 請幫我看一下 log")

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
