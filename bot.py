import os
import re
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv

# -------------------------------------------------
#  載入環境變數 (.env)
# -------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
ALLOWED_CHANNEL_IDS_RAW = os.getenv("ALLOWED_CHANNEL_IDS", "")

if not DISCORD_TOKEN or not N8N_WEBHOOK_URL:
    raise RuntimeError("缺少必要的環境變數：DISCORD_TOKEN 或 N8N_WEBHOOK_URL")

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
    # 用法：
    #   ~刪除 https://e.tb.cn/h.xxx
    #   ~刪除 〖淘宝〗... https://e.tb.cn/h.xxx 「商品」
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
    # 2) ~要買：新增一筆到購物清單
    #    格式（以 ~ 分隔）：
    #      有規格：~要買~3~白色~【淘宝】... https://... 「商品」
    #      無規格：~要買~3~【淘宝】... https://... 「商品」
    # ============================================
    if not text.startswith("~要買"):
        # 其餘訊息交給 commands 系統
        await bot.process_commands(message)
        return

    # 以 ~ 分隔，並去掉空字串：
    #   "~要買~3~白色~分享文字" -> ["要買","3","白色","分享文字"]
    parts = text.split("~")
    parts = [p for p in parts if p != ""]

    if len(parts) < 3:
        usage1 = "~要買~3~白色~【淘宝】7天无理由退货 https://e.tb.cn/h.xxx 「電動牙刷」"
        usage2 = "~要買~3~【淘宝】7天无理由退货 https://e.tb.cn/h.xxx 「電動牙刷」"
        await message.reply(
            "格式怪怪的 QQ\n"
            "請用下面這種格式，例如：\n"
            f"`{usage1}`\n或（沒有規格）\n`{usage2}`"
        )
        await bot.process_commands(message)
        return

    # parts[0] 理論上會是 "要買"，但就算不是也不太影響後面解析
    command_name = parts[0].strip()

    quantity_str = parts[1].strip()
    try:
        quantity = int(quantity_str)
    except ValueError:
        await message.reply("數量要是整數喔，例如：`~要買~3~白色~...`")
        await bot.process_commands(message)
        return

    # 判斷有沒有「型號規格」欄位：
    #   有規格：["要買","3","白色","分享文字..."]
    #   無規格：["要買","3","分享文字..."]
    if len(parts) >= 4:
        model_spec = parts[2].strip()
        # 分享文字可能理論上還會有 ~，所以把剩下全部 join 回去
        share_text = "~".join(parts[3:]).strip()
    else:
        model_spec = ""
        share_text = "~".join(parts[2:]).strip()

    # 檢查分享文字裡至少要有一個 URL
    if not URL_REGEX.search(share_text):
        await message.reply("我在分享文字裡找不到網址 QQ\n請確認有貼 https:// 開頭的購物連結。")
        await bot.process_commands(message)
        return

    payload = {
        "action": "add",
        "fullText": text,
        "shareText": share_text,
        "quantity": quantity,
        "modelSpec": model_spec,
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": message.created_at.isoformat(),
    }

    try:
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=8)
        print("Sent ADD to n8n:", resp.status_code, resp.text[:200])

        if resp.ok:
            if model_spec:
                await message.reply(f"已把「{quantity} 件 {model_spec}」商品加入購物清單 🧾")
            else:
                await message.reply(f"已把「{quantity} 件」商品加入購物清單 🧾")
        else:
            await message.reply("新增請求傳到 n8n 失敗 QQ")
    except Exception as e:
        print("Error sending add to n8n:", e)
        await message.reply("新增時我撞牆了 QQ 請幫我看一下 log")

    await bot.process_commands(message)


# -------------------------------------------------
#  入口
# -------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
