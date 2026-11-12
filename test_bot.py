#!/usr/bin/env python3
"""bot.py 裡純函式的測試（不連 Discord、不連 n8n、不呼叫 LLM）。

    python3 test_bot.py
"""
import asyncio
import itertools
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import httpx
import openai
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

# ============================================================
# 結算流程：Discord、n8n、LLM 都用假的，只驗證 bot 這一層怎麼串
print("\n結算：觸發關鍵字")

check("「結算」開頭才觸發", [bool(bot.SETTLE_RE.match(t)) for t in ("結算", "结算 這批", "幫我結算", "買 結算")], [True, True, False, False])
check("認領抓得到編號", [bot.CLAIM_RE.match(t).group(2) for t in ("認領 3", "認領#12", "认领 7 @人")], ["3", "12", "7"])
check("不是認領開頭不觸發", bot.CLAIM_RE.match("我想認領 3"), None)

bot.STATE_DIR = None  # 測試不可以寫到正式的結算紀錄（容器裡 STATE_DIR 有設）
_ids = itertools.count(1000)
SENT = []  # bot 送出的每則訊息


class FakeUser:
    def __init__(self, uid, name, is_bot=False):
        self.id, self.name, self.bot, self.mention = uid, name, is_bot, f"<@{uid}>"


class FakeChannel:
    def __init__(self, cid=1):
        self.id = cid

    async def send(self, text, **kw):
        return FakeMsg(BOT_USER, self, text=text, kw=kw, record=True)


class FakeMsg:
    def __init__(self, author, channel, text="", attachments=(), mentions=(), reference=None, kw=None, record=False):
        self.id, self.author, self.channel, self.content = next(_ids), author, channel, text
        self.attachments, self.mentions, self.reference, self.guild, self.kw = list(attachments), list(mentions), reference, None, kw or {}
        if record:
            SENT.append(self)

    async def reply(self, text, **kw):
        return FakeMsg(BOT_USER, self.channel, text=text, kw=kw, record=True)

    async def add_reaction(self, emoji):
        pass

    async def remove_reaction(self, emoji, user):
        pass


class FakeAtt:
    def __init__(self, size=1000, content_type="image/jpeg"):
        self.size, self.content_type = size, content_type

    async def read(self):
        return b"img"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self.ok, self._body = status, status < 400, body

    def json(self):
        return self._body


BOT_USER, AMY, BOB, CAROL = FakeUser(1, "ShoppingListBot", True), FakeUser(100, "amy"), FakeUser(200, "bob"), FakeUser(300, "carol")
SHOT = {"items": [{"name": "小米風扇", "spec": "白色", "qty": 1, "price": 100.0}, {"name": "神秘商品", "spec": "", "qty": 2, "price": 50.0}],
        "total_twd": 990, "order_no": ""}
SHEET = [{"tab": "淘寶", "row": 2, "name": "小米風扇落地扇", "spec": "白色", "url": "", "qty": 1, "buyer": "bob"}]


def run_settle(emoji="✅", settle_status=200, attachments=None, timeout=False):
    """跑一次 handle_settle，回傳 (n8n 收到的請求, bot 送出的訊息)。"""
    SENT.clear()
    bot.SETTLEMENTS.clear()
    calls, seen = [], {}

    async def fake_read(data, mime):
        return bot.settle.clean_shot({"items": [dict(i, paid_price=i["price"]) for i in SHOT["items"]], "total_twd": SHOT["total_twd"]})

    async def fake_n8n(payload, url=None):
        calls.append((payload, url))
        if payload["action"] == "list":
            return FakeResp(200, {"status": "ok", "rows": SHEET})
        body = {"status": "settled"} if settle_status == 200 else {"status": "conflict", "problems": [{"tab": "淘寶", "row": 2, "reason": "這一列已經不存在"}]}
        return FakeResp(settle_status, body)

    async def fake_match(payload):
        return [{"no": 1, "rows": ["淘寶2"], "confidence": "high"}]

    async def fake_rate():
        return None

    async def fake_wait(event, timeout=None, check=None):
        if timeout_flag:
            raise asyncio.TimeoutError()
        reaction = SimpleNamespace(message=SENT[-1], emoji=emoji)
        seen["other_user_ok"] = check(reaction, BOB)
        seen["payer_ok"] = check(reaction, AMY)
        return reaction, AMY

    timeout_flag = timeout
    bot.read_screenshot, bot.post_to_n8n, bot.match_rows, bot.market_rate, bot.bot.wait_for = fake_read, fake_n8n, fake_match, fake_rate, fake_wait
    msg = FakeMsg(AMY, FakeChannel(), "結算", attachments=[FakeAtt()] if attachments is None else attachments)
    asyncio.run(bot.handle_settle(msg))
    return calls, list(SENT), seen


print("\n結算：確認後寫入表格並公布")
real_post = bot.post_to_n8n
calls, sent, seen = run_settle("✅")
check("先向結算用的 webhook 要清單", (calls[0][0], calls[0][1]), ({"action": "list"}, bot.N8N_SETTLE_WEBHOOK_URL))
check("草稿不 ping 任何人", (sent[0].content.startswith("🧾 **結算草稿**｜下單人 amy"), sent[0].kw["allowed_mentions"].to_dict()["parse"]), (True, []))
check("只有下單人按的才算數", (seen["payer_ok"], seen["other_user_ok"]), (True, False))
batch = calls[1][0]["batch"]
check("確認後把對到的列寫回表格", calls[1][0], {"action": "settle", "batch": batch,
      "rows": [{"tab": "淘寶", "row": 2, "name": "小米風扇落地扇", "buyer": "bob", "price": 100.0}]})
check("正式結果 @ 下單人", f"🧾 **結算 {batch}**｜請轉帳給 <@100>" in sent[-1].content, True)
check("正式結果有每人金額，加總等於實付", ("**bob　TWD 495**" in sent[-1].content, "unclassified　TWD 495" in sent[-1].content), (True, True))
check("正式結果會 ping 人", sent[-1].kw["allowed_mentions"].to_dict()["parse"], ["users"])
check("結算狀態有留下來給認領用", (list(bot.SETTLEMENTS), bot.SETTLEMENTS[batch]["message_ids"]), ([batch], [sent[-1].id]))

print("\n認領")
state = bot.SETTLEMENTS[batch]
final_id = sent[-1].id


def run_claim(author, text, channel=None, mentions=(), reply_to=None):
    SENT.clear()
    ref = SimpleNamespace(message_id=reply_to) if reply_to else None
    msg = FakeMsg(author, channel or FakeChannel(), text, mentions=mentions, reference=ref)
    asyncio.run(bot.handle_claim(msg, int(bot.CLAIM_RE.match(text).group(2))))
    return SENT[-1].content


check("別的頻道認領不到", run_claim(CAROL, "認領 2", channel=FakeChannel(2))[:14], "找不到還沒人認領的 #2 🙅")
out = run_claim(CAROL, "認領 2")
check("認領後回覆更新的金額", out, f"#2 神秘商品 算在 <@300> 名下 ✅\n\n**結算 {batch} 更新後：**\nbob　TWD 495\n<@300>　TWD 495")
check("認領後算在認領的人名下", (bot.settle.totals(state)["twd"], "算在 <@300> 名下" in out), ({"bob": 495, "carol": 495}, True))
check("被認領走的不能再認領", run_claim(BOB, "認領 2", reply_to=final_id), "#2 已經算在 carol 名下了")
check("已經對到人的商品不能認領", run_claim(CAROL, "認領 1", reply_to=final_id), "#1 已經算在 bob 名下了")

calls, sent, _ = run_settle("✅")
out = run_claim(AMY, "認領 2 @bob", mentions=[BOT_USER, BOB], reply_to=sent[-1].id)
check("可以幫別人認領（略過被回覆的 bot 本身）", bot.settle.totals(next(iter(bot.SETTLEMENTS.values())))["twd"], {"bob": 990})

print("\n結算：不寫入的情況")
calls, sent, _ = run_settle("❌")
check("按 ❌ → 不寫表格、不留紀錄", ([c[0]["action"] for c in calls], sent[-1].content, bot.SETTLEMENTS), (["list"], "已取消，表格沒有變動。", {}))
calls, sent, _ = run_settle(timeout=True)
check("沒人確認 → 逾時取消", ([c[0]["action"] for c in calls], sent[-1].content[:8]), (["list"], "⌛ 太久沒有確認"))
calls, sent, _ = run_settle("✅", settle_status=409)
check("表格被改過（409）→ 說明原因、不公布、不留紀錄", ("淘寶 第 2 列：這一列已經不存在" in sent[-1].content, "請轉帳給" in sent[-1].content, bot.SETTLEMENTS), (True, False, {}))
calls, sent, _ = run_settle("✅", settle_status=500)
check("n8n 寫入失敗 → 不公布", ("寫進表格失敗" in sent[-1].content, bot.SETTLEMENTS), (True, {}))
calls, sent, _ = run_settle(attachments=[])
check("沒附圖 → 提示用法，不呼叫任何服務", (calls, "請把下單截圖和「結算」放在同一則訊息" in sent[-1].content), ([], True))
calls, sent, _ = run_settle(attachments=[FakeAtt(size=5_000_000)])
check("圖太大 → 說明原因", (calls, sent[-1].content[:12]), ([], "第 1 張圖超過 3MB"))
calls, sent, _ = run_settle(attachments=[FakeAtt(content_type="application/pdf")])
check("不是圖片的附件不算", calls, [])

print("\n結算紀錄存檔")
with tempfile.TemporaryDirectory() as d:
    bot.STATE_DIR = d
    run_settle("✅")
    saved = dict(bot.SETTLEMENTS)
    bot.SETTLEMENTS.clear()
    bot.load_settlements()
    check("重啟後讀得回來", bot.SETTLEMENTS == saved and len(saved) == 1, True)
    bot.STATE_DIR = None
bot.post_to_n8n = real_post

print("\nllm_json：429 重試")


class FlakyClient:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls += 1
        if self.calls <= self.fails:
            raise openai.RateLimitError("429", response=httpx.Response(429, request=httpx.Request("POST", "http://x")), body=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))])


bot.LLM_RETRY_WAIT = 0
flaky = FlakyClient(2)
check("429 兩次後成功", (asyncio.run(bot.llm_json(flaky, model="m", messages=[])), flaky.calls), ({"ok": 1}, 3))
always = FlakyClient(99)
try:
    asyncio.run(bot.llm_json(always, model="m", messages=[]))
    gave_up = False
except openai.RateLimitError:
    gave_up = True
check("一直 429 → 試 6 次後放棄", (gave_up, always.calls), (True, 6))

print(f"\n全部 {PASSED} 項測試通過 ✓\n")
