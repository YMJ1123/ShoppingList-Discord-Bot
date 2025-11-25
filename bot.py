import os
import re
from pathlib import Path
from typing import Optional, Set

import requests
import discord
from discord.ext import commands


def _load_env_file(path: Path) -> None:
    """Simple .env loader (無需額外套件)."""
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file(Path(__file__).with_name(".env"))


def _require_env(
    name: str,
    *,
    allow_empty: bool = False,
    default: Optional[str] = None,
) -> str:
    value = os.getenv(name, default)
    if value is None or (not allow_empty and value.strip() == ""):
        raise RuntimeError(f"缺少必要的環境變數：{name}")
    return value.strip()


def _parse_channel_ids(raw: str) -> Set[int]:
    channel_ids: Set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            channel_ids.add(int(chunk))
        except ValueError:
            print(f"⚠️ 無法解析 Channel ID：{chunk}")

    if not channel_ids:
        raise RuntimeError("請在 ALLOWED_CHANNEL_IDS 中填入至少一個 Channel ID (逗號分隔).")

    return channel_ids


# ========= 用環境變數設定 =========

# 1. Discord Bot Token（從 Developer Portal → Bot 頁面拿）
DISCORD_TOKEN = _require_env("DISCORD_TOKEN")

# 2. n8n Webhook 的 Production URL，預設為本機
N8N_WEBHOOK_URL = _require_env(
    "N8N_WEBHOOK_URL",
    allow_empty=False,
    default="http://localhost:5678/webhook/discord-shopping",
)

# 3. 允許 bot 處理的頻道 ID（用逗號分隔）
ALLOWED_CHANNEL_IDS = _parse_channel_ids(_require_env("ALLOWED_CHANNEL_IDS"))

# =======================================

intents = discord.Intents.default()
# 一定要開，bot 才能讀訊息文字
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    # if bot.user not in message.mentions:
    #     return

    # 去掉 @bot
    text = message.content
    mention_str1 = f"<@{bot.user.id}>"
    mention_str2 = f"<@!{bot.user.id}>"
    text = text.replace(mention_str1, "").replace(mention_str2, "").strip()

    # 新格式：~要買~數量~[規格]~分享文字...
    # 例1：~要買~3~"型號規格 可以有空格"~【淘宝/京东】... https://... 「商品」
    # 例2：~要買~3~【淘宝/京东】... https://... 「商品」
    # 規格部分改為可選（用 (?:~\s*"([^"]+)")?）
    regex = r'^~要買\s*~\s*(\d+)(?:\s*~\s*"([^"]+)")?\s*~\s*([\s\S]+)'
    m = re.match(regex, text, re.IGNORECASE)

    if not m:
        if "~要買" in text:
            # 👉 格式不符時回提示
            example1 = '~要買~3~【淘宝/京东】... https://... 「商品」'
            example2 = '~要買~3~"型號規格"~【淘宝/京东】... https://... 「商品」'
            await message.reply(
                "格式有誤 QQ\n"
                "請用下面其中一種格式：\n"
                f"`{example1}`\n"
                f"`{example2}`"
            )
            await bot.process_commands(message)
        return


    quantity = int(m.group(1))
    model_spec = m.group(2) if m.group(2) else ""  # 👈 規格（顏色/型號）可能為空
    share_text = m.group(3).strip()  # 淘寶分享文字

    # 👉 檢查是否包含連結
    if not re.search(r'https?://', share_text):
        example1 = '~要買~3~【淘宝/京东】... https://... 「商品」'
        example2 = '~要買~3~"型號規格"~【淘宝/京东】... https://... 「商品」'
        await message.reply(
            "格式有誤 QQ\n"
            "訊息中缺少連結！請用下面其中一種格式：\n"
            f"`{example1}`\n"
            f"`{example2}`"
        )
        await bot.process_commands(message)
        return

    payload = {
        "fullText": text,
        "shareText": share_text,
        "quantity": quantity,
        "modelSpec": model_spec,      # 👈 丟給 n8n
        "senderId": str(message.author.id),
        "senderName": message.author.name,
        "channelId": str(message.channel.id),
        "createdAt": message.created_at.isoformat(),
    }

    try:
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=5)
        print("Sent to n8n:", resp.status_code, resp.text[:200])

        if resp.ok:
            if model_spec:
                await message.reply(f"已把「{quantity} 件 {model_spec}」商品加入購物清單 🧾")
            else:
                await message.reply(f"已把「{quantity} 件」商品加入購物清單 🧾")
        else:
            await message.reply("傳到 n8n 失敗 QQ")
    except Exception as e:
        print("Error sending to n8n:", e)
        await message.reply("我撞牆了，主人快來看 log QQ")

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)