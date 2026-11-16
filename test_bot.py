#!/usr/bin/env python3
"""bot.py 裡純函式的測試（不連 Discord、不連 n8n、不呼叫 LLM）。

    python3 test_bot.py
"""
import asyncio
import itertools
import json
import re
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
check("認領抓得到編號", [bot.claim_numbers(t) for t in ("認領 3", "認領#12", "认领 7 @人")], [[3], [12], [7]])
check("不是認領開頭不觸發", (bot.CLAIM_RE.match("我想認領 3"), bot.CLAIM_RE.match("認領 @某人")), (None, None))
check("一次多個：空白、逗號、頓號", [bot.claim_numbers(t) for t in ("認領 3 5 7", "認領 3,5，7", "認領 #3、#5")], [[3, 5, 7], [3, 5, 7], [3, 5]])
check("範圍（反過來寫也可以）", [bot.claim_numbers(t) for t in ("認領 3-5", "認領 5~3", "認領 #3 到 #5")], [[3, 4, 5]] * 3)
check("@ 人的 id 裡的數字不算、重複的只算一次", [bot.claim_numbers(t) for t in ("認領 3 <@300>", "認領 3 <@!300> 5", "認領 3 3")], [[3], [3, 5], [3]])
check("離譜的範圍不展開", len(bot.claim_numbers("認領 1-100000")), 2)

# ------------------------------------------------------------------
# 結算流程：Discord、n8n、LLM 都用假的，只驗證 bot 這一層怎麼串
bot.STATE_DIR = None  # 測試不可以寫到正式的結算紀錄（容器裡 STATE_DIR 有設）
real_post, real_parse, real_read, real_llm_json = bot.post_to_n8n, bot.parse_with_llm, bot.read_screenshot, bot.llm_json
_ids = itertools.count(1000)
SENT = []  # bot 送出的每則訊息
CALLS = []  # bot 送給 n8n 的每個請求


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
        self.raw_mentions = [int(i) for i in re.findall(r"<@!?(\d+)>", text)]
        self.reactions, self.deleted = [], False
        if record:
            SENT.append(self)

    async def reply(self, text, **kw):
        return FakeMsg(BOT_USER, self.channel, text=text, kw=kw, record=True)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, user):
        pass

    async def delete(self):
        self.deleted = True


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self.ok, self._body = status, status < 400, body

    def json(self):
        return self._body


def order(items, twd, order_no=""):
    return {"items": [{"name": n, "spec": "", "qty": q, "price": p} for n, q, p in items], "total_twd": twd, "order_no": order_no}


class FakeAtt:
    def __init__(self, size=1000, content_type="image/jpeg", shot=None):
        self.size, self.content_type, self.shot = size, content_type, shot or SHOT

    async def read(self):
        return json.dumps(self.shot).encode()


BOT_USER, AMY, BOB, CAROL = FakeUser(1, "ShoppingListBot", True), FakeUser(100, "amy"), FakeUser(200, "bob"), FakeUser(300, "carol")
CHAN = FakeChannel(1)
SHOT = order([("小米風扇", 1, 100.0), ("神秘商品", 2, 50.0)], 990)
EARBUDS = order([("藍牙耳機", 1, 30.0)], 150, "5127000000000000002")
SHEET = [{"tab": "淘寶", "row": 2, "name": "小米風扇落地扇", "spec": "白色", "url": "", "qty": 1, "buyer": "bob"}]


def stub(settle_status=200, read_delay=0.0):
    SENT.clear()
    CALLS.clear()
    bot.SETTLEMENTS.clear()
    bot.PENDING.clear()
    bot.ALLOWED_CHANNEL_IDS = set()
    bot.SETTLE_CONFIRM_SECONDS = 600

    async def fake_read(data, mime):
        await asyncio.sleep(read_delay)
        raw = json.loads(data)
        return bot.settle.clean_shot(dict(raw, items=[dict(i, paid_price=i["price"]) for i in raw["items"]]))

    async def fake_n8n(payload, url=None):
        CALLS.append(payload)
        if payload["action"] == "list":
            return FakeResp(200, {"status": "ok", "rows": SHEET})
        if settle_status == 409:
            return FakeResp(409, {"status": "conflict", "problems": [{"tab": "淘寶", "row": 2, "reason": "這一列已經不存在"}]})
        return FakeResp(settle_status, {"status": "settled"})

    async def fake_match(payload):
        return [{"no": p["no"], "rows": [c["id"] for c in p["candidates"]], "confidence": "high"} for p in payload]

    async def fake_rate():
        return None

    async def noop(message):
        pass

    async def no_llm(text):  # 測試不可以打真的 Groq
        return None

    bot.parse_with_llm = no_llm
    bot.read_screenshot, bot.post_to_n8n, bot.match_rows, bot.market_rate = fake_read, fake_n8n, fake_match, fake_rate
    bot.bot.process_commands = noop
    bot.bot.get_channel = lambda cid: CHAN


async def send(author, text="", shots=(), atts=None, mentions=(), reply_to=None):
    atts = list(atts) if atts is not None else [FakeAtt(shot=x) for x in shots]
    ref = SimpleNamespace(message_id=reply_to) if reply_to else None
    msg = FakeMsg(author, CHAN, text, attachments=atts, mentions=mentions, reference=ref)
    await bot.on_message(msg)
    return msg


async def react(emoji, user, msg):
    await bot.on_raw_reaction_add(SimpleNamespace(emoji=emoji, user_id=user.id, message_id=msg.id, channel_id=CHAN.id))


def drafts():
    return [m for m in SENT if m.content.startswith("🧾 **結算草稿") and not m.deleted]


def published():
    return [m for m in SENT if m.content.startswith("🧾 **結算 ")]


def settle_calls():
    return [c for c in CALLS if c["action"] == "settle"]


def nos(state):
    return [it["no"] for u in state["units"] for it in u["items"]]


async def settle_and_confirm(author=AMY, shots=(SHOT,), text="結算", mentions=(), confirmer=None):
    await send(author, text, shots, mentions=mentions)
    await react("✅", confirmer or author, drafts()[-1])
    return list(bot.SETTLEMENTS.values())[-1]


print("\n結算：確認後寫入表格並公布")
stub()


async def basic():
    await send(AMY, "結算", [SHOT])
    d = drafts()[-1]
    check("草稿不 ping 任何人，有 ✅ ❌", (d.content.split("\n")[0], d.kw["allowed_mentions"].to_dict()["parse"], d.reactions),
          ("🧾 **結算草稿**｜下單人 amy", [], ["✅", "❌"]))
    check("先向結算用的 webhook 要清單", CALLS[0], {"action": "list"})
    await react("✅", BOB, d)
    check("別人按 ✅ → 不算", (settle_calls(), len(bot.PENDING)), ([], 1))
    await react("👍", AMY, d)
    check("下單人按別的表情 → 不算", settle_calls(), [])
    await react("✅", AMY, d)
    batch = settle_calls()[0]["batch"]
    check("下單人按 ✅ → 把對到的列寫回表格", settle_calls()[0],
          {"action": "settle", "batch": batch, "rows": [{"tab": "淘寶", "row": 2, "name": "小米風扇落地扇", "buyer": "bob", "price": 100.0}]})
    f = published()[-1]
    check("正式結果 @ 下單人、有每人金額", (f.content.split("\n")[0], "**bob　TWD 495**" in f.content, "unclassified　TWD 495" in f.content),
          (f"🧾 **結算 {batch}**｜請轉帳給 <@100>", True, True))
    check("正式結果只 ping 人", f.kw["allowed_mentions"].to_dict()["parse"], ["users"])
    check("結算留下紀錄、草稿收掉", (list(bot.SETTLEMENTS), bot.SETTLEMENTS[batch]["message_ids"], bot.PENDING), ([batch], [f.id], {}))
    await react("✅", AMY, d)
    check("再按一次 ✅ 不會重寫", len(settle_calls()), 1)

asyncio.run(basic())

print("\n追加截圖：還沒按 ✅ 前併成同一張草稿")
stub()


async def merge():
    await send(AMY, "結算", [SHOT])
    first = drafts()[-1]
    await send(AMY, "", [EARBUDS])  # 第二則只傳圖，沒打「結算」
    live = drafts()
    check("舊草稿被刪掉，只剩一張新的", (first.deleted, len(live)), (True, 1))
    state = bot.PENDING[(1, AMY.id)]["state"]
    check("兩則訊息的商品併在一起，編號接著排", nos(state), [1, 2, 3])
    check("新草稿列出兩筆訂單", live[0].content.split("\n")[1].startswith("2 筆訂單，實付合計 **TWD 1,140**"), True)
    await react("✅", AMY, first)
    check("在被取代的舊草稿上按 ✅ → 不算", settle_calls(), [])
    await send(BOB, "", [EARBUDS])
    check("別人只傳圖 → 不會被併進來", (len(bot.PENDING[(1, AMY.id)]["shots"]), len(drafts())), (2, 1))
    await react("✅", AMY, live[0])
    s = list(bot.SETTLEMENTS.values())[-1]
    check("確認後公布的是合併後的整張", (nos(s), sum(bot.settle.totals(s)["twd"].values())), ([1, 2, 3], 1140))
    n = len(SENT)
    await send(AMY, "", [EARBUDS])
    check("草稿確認後再只傳圖 → 不理會", (bot.PENDING, len(SENT)), ({}, n))

asyncio.run(merge())

stub()


async def overlap():
    page1 = order([("小米風扇", 1, 100.0), ("神秘商品", 2, 50.0)], 1200)
    page2 = order([("神秘商品", 2, 50.0), ("藍牙耳機", 1, 30.0)], 1200)
    await send(AMY, "結算", [page1])
    await send(AMY, "", [page2])
    state = bot.PENDING[(1, AMY.id)]["state"]
    check("確認頁分兩則傳、重疊拍到的商品 → 去重後是同一筆付款", ([len(u["items"]) for u in state["units"]], state["units"][0]["cny"]), ([3], 230.0))

asyncio.run(overlap())

stub(read_delay=0.05)


async def burst():
    await asyncio.gather(send(AMY, "結算", [SHOT]), send(AMY, "", [EARBUDS]))
    check("第一張還在讀圖時第二則就到 → 排隊併成一張", (len(drafts()), nos(bot.PENDING[(1, AMY.id)]["state"])), (1, [1, 2, 3]))
    check("沒有出現「草稿已經確認」的誤報", any("已經確認或取消" in m.content for m in SENT), False)

asyncio.run(burst())

stub(read_delay=0.05)


async def race():
    await send(AMY, "結算", [SHOT])
    old = drafts()[-1]
    await asyncio.gather(send(AMY, "", [EARBUDS]), react("✅", AMY, old))
    check("第二批還在處理時在舊草稿按 ✅ → 不算（不能確認還沒看過的商品）", (settle_calls(), len(bot.PENDING)), ([], 1))
    await react("✅", AMY, drafts()[-1])
    check("在新草稿上按 ✅ 才確認", nos(list(bot.SETTLEMENTS.values())[-1]), [1, 2, 3])

asyncio.run(race())

print("\n讀圖：讀壞了要重讀，還是不行就提醒")
PENCIL = {"items": [{"name": "适用applepencil笔尖苹果pencil...", "spec": "白色【2 枚装】", "qty": 1, "paid_price": 9.8}],
          "total_twd": 46, "order_no": "3317017045092056761"}


def vision(*replies):
    """讓讀圖的模型依序回傳 replies（Exception 就丟出），回傳 (讀到的結果, 呼叫次數)。"""
    calls = []

    async def fake_llm_json(client, **kw):
        calls.append(kw["model"])
        r = replies[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    bot.llm_json = fake_llm_json
    try:
        shot = asyncio.run(real_read(b"img", "image/jpeg"))
    finally:
        bot.llm_json = real_llm_json
    return shot, len(calls)


shot, n = vision(PENCIL)
check("一次就讀好 → 不重讀", ([it["name"][:13] for it in shot["items"]], n), (["适用applepencil"], 1))
shot, n = vision({"items": [], "total_twd": 46, "order_no": "3317017045092056761"}, PENCIL)
check("第一次沒讀到商品 → 重讀一次就有了", ([it["price"] for it in shot["items"]], shot["total_twd"], n), ([9.8], 46, 2))
shot, n = vision(json.JSONDecodeError("bad", "{", 0), PENCIL)
check("第一次回傳的不是 JSON → 重讀", ([it["price"] for it in shot["items"]], n), ([9.8], 2))
shot, n = vision({"items": [{"name": "甲", "qty": 1, "paid_price": 10}, {"name": "乙", "qty": 1, "paid_price": "?"}], "total_twd": 60},
                 {"items": [{"name": "甲", "qty": 1, "paid_price": 10}], "total_twd": 60})
check("兩次都不完整 → 取讀到比較多的那次", ([it["name"] for it in shot["items"]], shot["dropped"], n), (["甲"], 1, 2))
shot, n = vision({"items": []}, {"items": []})
check("兩次都讀不到 → 最多讀兩次，回傳空的讓草稿提醒", (shot["items"], n), ([], 2))

stub()


async def skipped_shot():
    await send(AMY, "結算", [SHOT])
    await send(AMY, "", [order([], 46, "3317017045092056761"), EARBUDS])
    text = drafts()[-1].content
    check("追加的第 1 張讀不到 → 草稿提醒「第 2 張截圖」（整張草稿的順序）",
          "第 2 張截圖沒讀到任何商品，可能讀錯了，請檢查那張或重傳" in text, True)
    check("同一則的其他圖照常併進來", nos(bot.PENDING[(1, AMY.id)]["state"]), [1, 2, 3])

asyncio.run(skipped_shot())

print("\n編號：同頻道不撞號")
stub()


async def numbering():
    s1 = await settle_and_confirm(AMY, [SHOT])
    s2 = await settle_and_confirm(CAROL, [EARBUDS])
    check("第二筆接著第一筆的編號", (nos(s1), nos(s2)), ([1, 2], [3]))
    check("同一秒開始的兩筆，批次編號不會撞", s1["batch"] != s2["batch"], True)
    await bot.handle_claim(FakeMsg(BOB, CHAN, "認領 3", mentions=()), [3])
    check("「認領 3」不用回覆，落在第二筆", bot.settle.find_item(s2, 3)[1]["shares"][0]["owner"], "bob")
    SENT.clear()
    await bot.handle_claim(FakeMsg(BOB, CHAN, "認領 2", mentions=()), [2])
    check("「認領 2」落在第一筆", bot.settle.find_item(s1, 2)[1]["shares"][0]["owner"], "bob")
    SENT.clear()
    await bot.handle_claim(FakeMsg(CAROL, CHAN, "認領 1", mentions=()), [1])
    check("已經對到人的編號 → 說明算在誰名下，而不是找不到", SENT[-1].content, "#1 已經算在 bob 名下了")
    await react("🏁", AMY, SimpleNamespace(id=s1["message_ids"][-1]))
    s3 = await settle_and_confirm(AMY, [EARBUDS])
    check("只看還在認領中的結算接續編號（第一筆已結束）", nos(s3), [4])
    await react("🏁", CAROL, SimpleNamespace(id=s2["message_ids"][-1]))
    await react("🏁", AMY, SimpleNamespace(id=s3["message_ids"][-1]))
    s4 = await settle_and_confirm(AMY, [EARBUDS])
    check("全部都結束認領後，編號從 #1 重新開始", nos(s4), [1])

asyncio.run(numbering())

print("\n重複結算的警告")
stub()


async def duplicate():
    s1 = await settle_and_confirm(AMY, [SHOT])
    await send(BOB, "結算", [SHOT])
    text = drafts()[-1].content
    check("同樣的商品又結算一次 → 草稿提醒", (f"#3 小米風扇 ×1 ¥100.00 和結算 {s1['batch']} 的 #1 看起來是同一件，可能重複結算" in text,
                                         f"#4 神秘商品 ×2 ¥50.00 和結算 {s1['batch']} 的 #2 看起來是同一件，可能重複結算" in text), (True, True))
    await send(CAROL, "結算", [EARBUDS])
    check("不同商品 → 不提醒", "重複結算" in drafts()[-1].content, False)

asyncio.run(duplicate())

print("\n結算：不寫入的情況")
stub()


async def cancelled():
    await send(AMY, "結算", [SHOT])
    await react("❌", AMY, drafts()[-1])
    check("按 ❌ → 不寫表格、不留紀錄", (settle_calls(), SENT[-1].content, bot.SETTLEMENTS, bot.PENDING), ([], "已取消，表格沒有變動。", {}, {}))

asyncio.run(cancelled())

stub()


async def expired():
    bot.SETTLE_CONFIRM_SECONDS = 0.1
    await send(AMY, "結算", [SHOT])
    await asyncio.sleep(0.06)
    await send(AMY, "", [EARBUDS])  # 追加會重新計時
    await asyncio.sleep(0.06)
    check("追加後重新計時，還沒過期", len(bot.PENDING), 1)
    await asyncio.sleep(0.1)
    check("沒人確認 → 逾時取消", (settle_calls(), SENT[-1].content[:8], bot.PENDING), ([], "⌛ 太久沒有確認", {}))
    d = [m for m in SENT if m.content.startswith("🧾 **結算草稿") and not m.deleted][-1]
    await react("✅", AMY, d)
    check("過期後按 ✅ 沒用", (settle_calls(), bot.SETTLEMENTS), ([], {}))

asyncio.run(expired())

for status, label, expect in [(409, "表格被改過（409）→ 說明原因、不公布、不留紀錄", "淘寶 第 2 列：這一列已經不存在"),
                              (500, "n8n 寫入失敗 → 不公布", "寫進表格失敗")]:
    stub(settle_status=status)

    async def fail():
        await send(AMY, "結算", [SHOT])
        await react("✅", AMY, drafts()[-1])
        check(label, (expect in SENT[-1].content, published(), bot.SETTLEMENTS), (True, [], {}))
    asyncio.run(fail())

stub()


async def bad_input():
    await send(AMY, "結算")
    check("沒附圖 → 提示用法，不呼叫任何服務", (CALLS, "請把下單截圖和「結算」放在同一則訊息" in SENT[-1].content), ([], True))
    await send(AMY, "結算", atts=[FakeAtt(size=5_000_000)])
    check("圖太大 → 說明原因，不留草稿", (CALLS, SENT[-1].content[:12], bot.PENDING), ([], "第 1 張圖超過 3MB", {}))
    await send(AMY, "結算", atts=[FakeAtt(content_type="application/pdf")])
    check("不是圖片的附件不算", CALLS, [])
    await send(AMY, "結算", [SHOT])
    await send(AMY, "", atts=[FakeAtt(size=5_000_000)])
    check("追加的圖有問題 → 舊草稿保留", (SENT[-1].content.endswith("（之前的草稿還在，這幾張沒有併進去）"), len(drafts()), len(bot.PENDING)), (True, 1, 1))
    n = len(SENT)
    await send(BOB, "", [SHOT])
    check("沒有草稿時只傳圖 → 不理會", (len(bot.PENDING), len(SENT)), (1, n))
    n = len(SENT)
    await send(AMY, "買 小米風扇 https://e.tb.cn/x", [EARBUDS])
    check("有草稿時傳「買…」附圖 → 不會被當成追加截圖", (len(bot.PENDING[(1, AMY.id)]["shots"]), len([m for m in SENT[n:] if "結算草稿" in m.content])), (1, 0))

asyncio.run(bad_input())

print("\n認領")


def run_claim(author, text, mentions=(), reply_to=None):
    SENT.clear()
    ref = SimpleNamespace(message_id=reply_to) if reply_to else None
    asyncio.run(bot.handle_claim(FakeMsg(author, CHAN, text, mentions=mentions, reference=ref), bot.claim_numbers(text)))
    return SENT[-1].content


stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
batch, final_id = state["batch"], state["message_ids"][-1]
check("別的頻道認領不到", (SENT.clear(), asyncio.run(bot.handle_claim(FakeMsg(CAROL, FakeChannel(2), "認領 2"), [2])), SENT[-1].content[:14])[2], "找不到還沒人認領的 #2 🙅")
out = run_claim(CAROL, "認領 2")
check("認領後回覆更新的金額", out, f"#2 神秘商品 算在 <@300> 名下 ✅\n\n**結算 {batch} 更新後：**\nbob　TWD 495\n<@300>　TWD 495\n\n全部都有人認領了，amy 按 🏁 公布最終金額並通知大家")
check("認領後算在認領的人名下", bot.settle.totals(state)["twd"], {"bob": 495, "carol": 495})
check("被認領走的不能再認領", run_claim(BOB, "認領 2", reply_to=final_id), "#2 已經算在 carol 名下了")
check("已經對到人的商品不能認領", run_claim(CAROL, "認領 1", reply_to=final_id), "#1 已經算在 bob 名下了")

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
run_claim(AMY, "認領 2 <@200>", mentions=[BOT_USER, BOB], reply_to=state["message_ids"][-1])
check("可以幫別人認領（略過被回覆的 bot 本身）", bot.settle.totals(state)["twd"], {"bob": 990})

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
run_claim(CAROL, "認領 2", mentions=[BOB], reply_to=state["message_ids"][-1])
check("回覆 bob 的訊息打「認領 2」→ 算給自己，不是被回覆的 bob", bot.settle.totals(state)["twd"], {"bob": 495, "carol": 495})

print("\n一次認領多件")
THREE = order([("甲甲甲", 1, 10.0), ("乙乙乙", 1, 20.0), ("丙丙丙", 1, 30.0)], 300)
stub()
state = asyncio.run(settle_and_confirm(AMY, [THREE]))
out = run_claim(CAROL, "認領 1 3")
check("一次認領兩件 → 一則回覆列出全部", out.split("\n\n")[0], "2 件算在 <@300> 名下 ✅\n#1 甲甲甲\n#3 丙丙丙")
check("金額一起更新（匯率 5：¥40 → 200，剩 ¥20 → 100）", bot.settle.totals(state)["twd"], {"carol": 200, "unclassified": 100})
check("兩件都算在 carol", [bot.settle.find_item(state, n)[1]["shares"][0]["owner"] for n in (1, 2, 3)], ["carol", "unclassified", "carol"])
reply = SENT[-1]
check("回覆上有 🏁、記進這次結算", (reply.reactions, reply.id in state["message_ids"]), (["🏁"], True))
out = run_claim(BOB, "認領 1-2")
check("部分成功：成功的照領，失敗的說明原因", out.split("\n\n")[:2], ["#2 乙乙乙 算在 <@200> 名下 ✅", "沒有認領到：\n- #1 已經算在 carol 名下了"])
check("部分成功後金額正確", bot.settle.totals(state)["twd"], {"carol": 200, "bob": 100})
check("全部都失敗 → 列出每一件的原因，不改任何東西", run_claim(BOB, "認領 1 3 9"),
      "沒有認領到任何一件：\n- #1 已經算在 carol 名下了\n- #3 已經算在 carol 名下了\n- 找不到還沒人認領的 #9 🙅")
check("全部失敗後金額沒變", bot.settle.totals(state)["twd"], {"carol": 200, "bob": 100})
check("單一編號失敗的訊息和以前一樣", run_claim(BOB, "認領 9"), "找不到還沒人認領的 #9 🙅\n要認領的話，請直接回覆在那則結算訊息底下。")
check("一次超過上限 → 拒絕", run_claim(BOB, "認領 1-40"), "一次最多認領 30 件，請分幾次打 🙏")

stub()
s1 = asyncio.run(settle_and_confirm(AMY, [THREE]))
s2 = asyncio.run(settle_and_confirm(CAROL, [order([("丁丁丁", 1, 40.0), ("戊戊戊", 1, 50.0)], 450)]))
run_claim(BOB, "認領 3 4 <@300>", mentions=[CAROL])
replies = [m.content for m in SENT]
check("跨兩筆結算 + 幫別人認領 → 每筆各回一則", [r.split("\n")[0] for r in replies], ["#3 丙丙丙 算在 <@300> 名下 ✅", "#4 丁丁丁 算在 <@300> 名下 ✅"])
check("@ 的 id 不會被當成編號 300", any("#300" in r for r in replies), False)
check("各自歸到 carol", (bot.settle.find_item(s1, 3)[1]["shares"][0]["owner"], bot.settle.find_item(s2, 4)[1]["shares"][0]["owner"]), ("carol", "carol"))

print("\n幫別人結算：「結算 @B」")
stub()


async def proxy():
    await send(AMY, "結算 <@300>", [SHOT], mentions=[CAROL])
    d = drafts()[-1]
    check("草稿標出代傳", d.content.split("\n")[0], "🧾 **結算草稿**｜下單人 carol（amy 代傳）")
    await react("✅", BOB, d)
    check("其他人按 ✅ 不算", settle_calls(), [])
    await send(AMY, "", [EARBUDS])
    check("代傳的人追加截圖 → 併進同一張", nos(bot.PENDING[(1, AMY.id)]["state"]), [1, 2, 3])
    await react("✅", CAROL, drafts()[-1])
    s = list(bot.SETTLEMENTS.values())[-1]
    check("被幫忙的人也能按 ✅", (s["payer"], s["payer_id"], s["poster"]), ("carol", 300, "amy"))
    check("正式結果請大家轉給 carol", published()[-1].content.split("\n")[0], f"🧾 **結算 {s['batch']}**｜請轉帳給 <@300>（amy 代傳）")
    check("代傳的人不會被當成下單人", "不用轉" in published()[-1].content, False)

asyncio.run(proxy())

stub()
asyncio.run(send(AMY, "結算 <@200>", [SHOT], mentions=[BOB]))
asyncio.run(react("✅", AMY, drafts()[-1]))
check("被幫忙的人自己有買 → 他那份標不用轉", "**<@200>　TWD 495**（¥100.00）（下單人自己的，不用轉）" in published()[-1].content, True)
stub()
asyncio.run(send(AMY, "結算", [SHOT], mentions=[CAROL]))
check("沒在內文 @ 人（例如回覆別人時被帶進來的）→ 維持傳圖的人", drafts()[-1].content.split("\n")[0], "🧾 **結算草稿**｜下單人 amy")
stub()
asyncio.run(send(AMY, "結算 <@1>", [SHOT], mentions=[BOT_USER]))
check("@ 的是 bot → 不算", drafts()[-1].content.split("\n")[0], "🧾 **結算草稿**｜下單人 amy")

print("\n🏁 結束認領")


def press(emoji, user, message_id):
    """模擬有人在某則訊息上按反應，回傳 bot 因此送出的訊息。"""
    SENT.clear()
    asyncio.run(bot.on_raw_reaction_add(SimpleNamespace(emoji=emoji, user_id=user.id, message_id=message_id, channel_id=1)))
    return list(SENT)


stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
batch, final = state["batch"], published()[-1]
final_id = final.id
check("有沒人認領的商品 → 正式結果上有 🏁", final.reactions, ["🏁"])
check("別人按 🏁 → 沒反應", (press("🏁", BOB, final_id), state.get("closed")), ([], None))
check("下單人按別的表情 → 沒反應", (press("👍", AMY, final_id), state.get("closed")), ([], None))
check("在不相干的訊息按 🏁 → 沒反應", (press("🏁", AMY, 424242), state.get("closed")), ([], None))
out = press("🏁", AMY, final_id)
check("下單人按 🏁 → 公布最終金額", [m.content for m in out],
      [f"🏁 **結算 {batch} 結束認領**｜請轉帳給 <@100>\n\n**bob　TWD 495**\n\n下單人自己的 TWD 495 不用轉，含沒人認領的 #2"])
check("回覆在被按的那則訊息底下，而且只 ping 人", (out[0].kw["reference"].message_id, out[0].kw["allowed_mentions"].to_dict()["parse"]), (final_id, ["users"]))
check("沒人領的算給下單人，總額不變", bot.settle.totals(state)["twd"], {"bob": 495, "amy": 495})
check("再按一次不會重發", press("🏁", AMY, final_id), [])
check("結束後回覆那則訊息認領 → 拒絕", run_claim(CAROL, "認領 2", reply_to=final_id), f"結算 {batch} 已經結束認領了，有問題請直接找下單的人")
check("結束後不回覆直接認領 → 找不到", run_claim(CAROL, "認領 2")[:14], "找不到還沒人認領的 #2 🙅")

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
run_claim(CAROL, "認領 2")
claim_reply = SENT[-1]
check("認領的回覆上也有 🏁，而且記進這次結算", (claim_reply.reactions, claim_reply.id in state["message_ids"]), (["🏁"], True))
out = press("🏁", AMY, claim_reply.id)
check("全部認領完，在認領回覆上按 🏁 → 公布並 ping 每個要付錢的人", out[0].content,
      f"🏁 **結算 {state['batch']} 結束認領**｜請轉帳給 <@100>\n\n**bob　TWD 495**\n**<@300>　TWD 495**")

stub()
asyncio.run(settle_and_confirm(AMY, [order([("小米風扇", 1, 100.0)], 495)]))
check("全部都對到人 → 不放 🏁", published()[-1].reactions, [])

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT], text="結算 <@300>", mentions=[CAROL], confirmer=CAROL))
out = press("🏁", AMY, published()[-1].id)
lines = out[0].content.split("\n") if out else ["（沒有送出任何訊息）"]
check("代傳：代傳的人也能按 🏁，剩下的算給被幫忙的人", (lines[0], lines[-1]),
      (f"🏁 **結算 {state['batch']} 結束認領**｜請轉帳給 <@300>", "下單人自己的 TWD 495 不用轉，含沒人認領的 #2"))

print("\n結算紀錄存檔")
with tempfile.TemporaryDirectory() as d:
    bot.STATE_DIR = d
    stub()
    state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
    final_id, batch = state["message_ids"][-1], state["batch"]
    saved = json.loads(json.dumps(bot.SETTLEMENTS))
    bot.SETTLEMENTS.clear()
    bot.load_settlements()
    check("重啟後讀得回來", bot.SETTLEMENTS == saved and len(saved) == 1, True)
    bot.SETTLEMENTS[batch].pop("poster_id")           # 舊版存的紀錄沒有這個欄位
    check("重啟後、舊紀錄：別人按還是沒反應", press("🏁", CAROL, final_id), [])
    check("重啟後、舊紀錄：下單人按 🏁 照樣有效", len(press("🏁", AMY, final_id)), 1)
    bot.SETTLEMENTS.clear()
    bot.load_settlements()
    check("結束狀態有存檔，重啟後不會被重開", (bot.SETTLEMENTS[batch]["closed"], press("🏁", AMY, final_id)), (True, []))
    bot.STATE_DIR = None
bot.post_to_n8n, bot.parse_with_llm = real_post, real_parse
bot.SETTLE_CONFIRM_SECONDS = 600

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
