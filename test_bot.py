#!/usr/bin/env python3
"""bot.py 裡純函式的測試（不連 Discord、不連 n8n、不呼叫 LLM）。

    python3 test_bot.py
"""
import asyncio
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import requests

import bot

PASSED = 0


def check(name: str, got, expect):
    global PASSED
    if got != expect:
        print(f"  ✗ {name}\n      實際: {got!r}\n      預期: {expect!r}")
        sys.exit(1)
    PASSED += 1
    print(f"  ✓ {name}")


# ============================================================
# 2026-09-10 的實際案例：淘寶長網址貼進 Discord 之後的樣子。
# 顯示文字是解碼過的（含 " [ ]），括號裡才是真正能點的網址。
TB_REAL = (
    "https://item.taobao.com/item.htm?abbucket=10&id=652762230399"
    "&mi_id=0000PANjxDe-5C--f_HTLhxrxb8PkWsoFzvgbVQXXgx3Op0&ns=1&skuId=4709862357636"
    "&spm=a21n57.1.hoverItem.37"
    "&utparam=%7B%22aplus_abtest%22%3A%22bcb827415a592b935282236e5ea4572f%22%7D"
    "&xxc=taobaoSearch"
)
TB_DISPLAY = (
    "https://item.taobao.com/item.htm?abbucket=10&id=652762230399"
    "&mi_id=0000PANjxDe-5C--f_HTLhxrxb8PkWsoFzvgbVQXXgx3Op0&ns=1&skuId=4709862357636"
    "&spm=a21n57.1.hoverItem.37"
    '&utparam=%7B["aplus_abtest"%3A"bcb827415a592b935282236e5ea4572f"%7D'
    "&xxc=taobaoSearch"
)
TB_PASTED = f"{TB_DISPLAY}]({TB_REAL})"

MSG_ADD = (
    "買 一組 磨砂卡册+20页黑色双面360个卡位 【送100个卡膜】 "
    f"{TB_PASTED} "
    "「卡片收集册卡牌活页收藏册小马宝莉叶罗丽火影九格活页卡册卡页」"
)

print("\nextract_first_url：貼上的淘寶長網址（2026-09-10 迴歸）")

added = bot.extract_first_url(MSG_ADD)
check("markdown 包裝 → 取括號裡真正能點的網址", added, TB_REAL)
check("抽到的網址不含 markdown 分隔", "](" in added, False)
check("自然語言刪除抽到同一個網址", bot.extract_first_url(f"刪除 {TB_PASTED}"), added)
check("~刪除 抽到同一個網址", bot.extract_first_url(f"~刪除 {TB_PASTED}"), added)
check(
    "沒有完整 markdown 包裝時也不會吃到分隔符",
    bot.extract_first_url(f"買 東西 {TB_DISPLAY}](something 「名稱」"),
    TB_DISPLAY,
)

print("\nextract_first_url：一般情況")

CASES = [
    ("純網址", "買 風扇 https://e.tb.cn/h.abc", "https://e.tb.cn/h.abc"),
    (
        "網址緊貼中文（沒有空格）",
        "買一組 竖版7寸https://detail.tmall.com/item.htm?id=790842407992",
        "https://detail.tmall.com/item.htm?id=790842407992",
    ),
    ("結尾全形引號", "買 牙刷 https://e.tb.cn/h.abc」", "https://e.tb.cn/h.abc"),
    ("結尾半形括號與逗號", "看這個 (https://example.com/x)，好嗎", "https://example.com/x"),
    ("帶 query string", "https://example.com/p?a=1&b=2", "https://example.com/p?a=1&b=2"),
    ("京東短連結", "買 三個【京东】https://3.cn/2v-s8bsu 「耳機」", "https://3.cn/2v-s8bsu"),
    ("沒有網址回 None", "今天天氣真好", None),
]
for label, text, expect in CASES:
    check(label, bot.extract_first_url(text), expect)

print("\nsafe_quantity")

for label, got, expect in [
    ("整數", bot.safe_quantity(3), 3),
    ("數字字串", bot.safe_quantity("2"), 2),
    ("中文數字轉不了 → 1", bot.safe_quantity("兩"), 1),
    ("None → 1", bot.safe_quantity(None), 1),
    ("負數 → 1", bot.safe_quantity(-5), 1),
    ("浮點數", bot.safe_quantity(2.0), 2),
]:
    check(label, got, expect)

print("\n刪除意圖關鍵字")

for label, text, should_match in [
    ("刪除電動牙刷", "刪除電動牙刷", True),
    ("小米風扇不用買了", "小米風扇不用買了", True),
    ("幫我把風扇刪掉", "幫我把風扇刪掉", True),
    ("閒聊不該命中", "今天天氣真好", False),
]:
    check(label, bool(bot.DELETE_INTENT_RE.search(text)), should_match)

# ============================================================
# 2026-10-01 15:34：n8n 的 Sheets 節點寫了 10.8 秒，舊的 8 秒逾時就誤報「新增失敗」，
# 但那筆其實已經寫進表格。15:35 重送時 DNS 剛好解析失敗，safe_react 沒接住就整個中斷。
print("\nsafe_react：網路錯誤不能讓整個訊息處理中斷")


class FakeMessage:
    def __init__(self, exc):
        self.exc = exc

    async def add_reaction(self, emoji):
        raise self.exc

    async def remove_reaction(self, emoji, user):
        raise self.exc


def run_safe_react(exc, remove=False):
    try:
        asyncio.run(bot.safe_react(FakeMessage(exc), "⏳", remove=remove))
    except Exception as e:
        return e
    return None


check(
    "加反應遇到 DNS 解析失敗 → 不丟例外",
    run_safe_react(aiohttp.ClientConnectionError("Temporary failure in name resolution")),
    None,
)
check("加反應遇到等待逾時 → 不丟例外", run_safe_react(asyncio.TimeoutError()), None)
check("移除反應遇到網路錯誤 → 不丟例外", run_safe_react(aiohttp.ClientConnectionError("x"), remove=True), None)

print("\nn8n 逾時：慢不等於失敗")


class SlowN8n(BaseHTTPRequestHandler):
    delay = 0.0

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        time.sleep(self.delay)
        body = b'{"status":"added"}'
        try:
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def post_to_slow_n8n(delay: float):
    SlowN8n.delay = delay
    try:
        return asyncio.run(bot.post_to_n8n({"action": "add"}))
    except requests.exceptions.RequestException as e:
        return e


server = ThreadingHTTPServer(("127.0.0.1", 0), SlowN8n)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
prod_url, prod_timeout = bot.N8N_WEBHOOK_URL, bot.N8N_READ_TIMEOUT
bot.N8N_WEBHOOK_URL = f"http://127.0.0.1:{server.server_port}/"
bot.N8N_READ_TIMEOUT = 1
try:
    slow_ok = post_to_slow_n8n(0.4)
    check("慢但在時限內 → 照常拿到 200", getattr(slow_ok, "status_code", slow_ok), 200)

    timed_out = post_to_slow_n8n(2.0)
    check("超過時限 → ReadTimeout", type(timed_out).__name__, "ReadTimeout")
    msg = bot.n8n_failure_reply("新增", "寫進表格", timed_out)
    check("新增逾時 → 告知請求已送出、可能已寫入", "可能已經寫進表格" in msg, True)
    check("逾時訊息帶的秒數 = 實際設定值", "超過 1 秒" in msg, True)
finally:
    bot.N8N_WEBHOOK_URL, bot.N8N_READ_TIMEOUT = prod_url, prod_timeout
    server.shutdown()

check("正式的讀取逾時比實測最慢的 10.8 秒寬鬆", bot.N8N_READ_TIMEOUT > 10.8, True)
check(
    "刪除逾時 → 講的是「可能已經刪掉」",
    "可能已經從表格刪掉" in bot.n8n_failure_reply("刪除", "從表格刪掉", requests.exceptions.ReadTimeout()),
    True,
)
check(
    "連不上 n8n → 維持原本的訊息",
    bot.n8n_failure_reply("新增", "寫進表格", requests.exceptions.ConnectionError()),
    "新增時我撞牆了 QQ 請幫我看一下 log",
)
check(
    "連線逾時（請求根本沒送出）→ 不能說「可能已經」",
    "可能已經" in bot.n8n_failure_reply("新增", "寫進表格", requests.exceptions.ConnectTimeout()),
    False,
)

print(f"\n全部 {PASSED} 項測試通過 ✓\n")
