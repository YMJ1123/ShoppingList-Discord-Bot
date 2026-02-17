import os
import re
import json
import asyncio
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI

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

# 抓第一個網址用的 regex
URL_REGEX = re.compile(r"https?://\S+")

# -------------------------------------------------
#  初始化 Groq（OpenAI 相容 API）
# -------------------------------------------------
groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_MAX_RETRIES = 3

SYSTEM_PROMPT = (
    "你是一個購物助手。使用者會傳來 Discord 購物訊息，你要從中提取購物資訊。\n"
    "請嚴格回傳以下 JSON 格式：\n"
    '{"itemName": "商品名稱", "quantity": 1, "modelSpec": "型號規格", "url": "網址"}\n\n'
    "規則：\n"
    "- itemName：商品名稱，從訊息中提取。如果訊息包含「...」格式的商品名，優先使用。\n"
    "- quantity：數量，必須是整數。如果使用者用中文數字（如 兩台、三個），請轉換為阿拉伯數字。預設為 1。\n"
    "- modelSpec：型號、規格、顏色等描述（如 白色、256GB）。如果沒有就留空字串。\n"
    "- url：訊息中的網址（https:// 開頭）。如果沒有就留空字串，要注意不該有空格。\n"
    "- 如果資訊缺失請留空字串，數量預設為 1。\n"
    "- 只輸出 JSON，不要有其他解釋文字。"
)


async def parse_with_llm(message_content: str) -> dict | None:
    """呼叫 Groq API 解析購物訊息，含 429 自動重試，回傳 dict 或 None。"""
    raw_text = ""

    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        try:
            response = groq_client.chat.completions.create(
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

            return {
                "itemName": str(parsed.get("itemName", "")),
                "quantity": int(parsed.get("quantity", 1)) if parsed.get("quantity") else 1,
                "modelSpec": str(parsed.get("modelSpec", "")),
                "url": str(parsed.get("url", "")),
            }

        except json.JSONDecodeError as e:
            print(f"[ERROR] LLM 回傳的不是有效 JSON: {e}")
            print(f"[ERROR] 原始回傳: {raw_text!r}")
            return None

        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "rate_limit" in error_str.lower()

            if is_rate_limit and attempt < GROQ_MAX_RETRIES:
                wait_sec = min(10 * attempt, 30)
                print(f"[WARN] Groq 429 rate limit，第 {attempt} 次重試，等待 {wait_sec} 秒...")
                await asyncio.sleep(wait_sec)
                continue

            print(f"[ERROR] 呼叫 Groq 時發生錯誤 (attempt {attempt}/{GROQ_MAX_RETRIES}): {e}")
            return None

    return None


# -------------------------------------------------
#  Discord Bot 初始化
# -------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # 一定要開，不然讀不到訊息內容

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ALLOWED_CHANNEL_IDS:
        print(f"Allowed channel IDs: {sorted(ALLOWED_CHANNEL_IDS)}")
    else:
        print("Allowed channel IDs: 不限制（所有頻道都會處理）")
    print(f"LLM: Groq {GROQ_MODEL} ✓")


# -------------------------------------------------
#  主訊息處理：~要買 / ~刪除
# -------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    # 不處理自己
    if message.author == bot.user:
        return

    # 如果有設定 ALLOWED_CHANNEL_IDS，就只處理這些頻道
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    text = message.content.strip()

    # ============================================
    # 1) ~刪除：刪除自己加的那筆（用網址辨識）
    # ============================================
    if text.startswith("~刪除"):
        url_match = URL_REGEX.search(text)
        if not url_match:
            usage = "~刪除 https://e.tb.cn/h.xxx"
            await message.reply(
                "看不到要刪除的連結 QQ\n"
                f"請用例如：`{usage}`，或是把原本分享文貼上前面加 `~刪除`。"
            )
            await bot.process_commands(message)
            return

        url_to_delete = url_match.group(0)

        payload = {
            "action": "delete",
            "url": url_to_delete,
            "senderId": str(message.author.id),
            "senderName": message.author.name,
            "channelId": str(message.channel.id),
            "createdAt": message.created_at.isoformat(),
        }

        try:
            resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=8)
            print("Sent DELETE to n8n:", resp.status_code, resp.text[:200])

            if resp.status_code == 200:
                await message.reply("已刪除你加進清單的那筆商品 🗑️")
            elif resp.status_code == 403:
                await message.reply("這不是你加進清單的，你不能刪 🙅")
            else:
                await message.reply("刪除請求傳到 n8n 失敗 QQ")
        except Exception as e:
            print("Error sending delete to n8n:", e)
            await message.reply("刪除時我撞牆了 QQ 請幫我看一下 log")

        await bot.process_commands(message)
        return

    # ============================================
    # 2) ~要買：使用 LLM 解析自然語言，新增到購物清單
    # ============================================
    if not text.startswith("~要買"):
        await bot.process_commands(message)
        return

    # 先給使用者一個「處理中」的反應
    await message.add_reaction("⏳")

    parsed = await parse_with_llm(text)

    if parsed is None:
        await message.remove_reaction("⏳", bot.user)
        await message.reply(
            "AI 解析服務暫時忙碌，請稍後再試一次 🔄\n"
            "（如果持續失敗，可能是 API 額度用完了）"
        )
        await bot.process_commands(message)
        return

    if not parsed.get("itemName"):
        await message.remove_reaction("⏳", bot.user)
        await message.reply(
            "我沒辦法從訊息中提取到商品名稱 QQ\n"
            "請試試類似這樣的格式：\n"
            "`~要買 兩台白色的小米風扇 https://...`\n"
            "`~要買 【淘宝】... https://e.tb.cn/h.xxx 「電動牙刷」`"
        )
        await bot.process_commands(message)
        return

    item_name = parsed["itemName"]
    quantity = parsed["quantity"]
    model_spec = parsed["modelSpec"]
    url = parsed["url"]

    # 如果 LLM 沒抓到 URL，從原始訊息中再嘗試 regex 撈一次
    if not url:
        url_match = URL_REGEX.search(text)
        if url_match:
            url = url_match.group(0)

    if not url:
        await message.remove_reaction("⏳", bot.user)
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
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=8)
        print("Sent ADD to n8n:", resp.status_code, resp.text[:200])

        await message.remove_reaction("⏳", bot.user)

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
        await message.remove_reaction("⏳", bot.user)
        await message.reply("新增時我撞牆了 QQ 請幫我看一下 log")

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
