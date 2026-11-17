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
import time
from pathlib import Path
from types import SimpleNamespace

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

print("\n表格回應逾時：慢不等於失敗")
check("逾時 → 告知請求已送出、可能已經寫入",
      bot.sheet_failure_reply("新增", "寫進表格", requests.exceptions.ReadTimeout()),
      "新增請求已送出，但 Google 表格太久沒回應 ⏱️\n這筆**可能已經寫進表格**，請先看一下清單，沒有再重送一次。")
check("刪除逾時 → 講的是「可能已經刪掉」",
      "可能已經從表格刪掉" in bot.sheet_failure_reply("刪除", "從表格刪掉", requests.exceptions.ReadTimeout()), True)
check("其他錯誤 → 請他看 log，不說「可能已經」",
      bot.sheet_failure_reply("新增", "寫進表格", requests.exceptions.ConnectionError()), "新增時我撞牆了 QQ 請幫我看一下 log")

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
real_post, real_parse, real_read, real_llm_json = bot.sheet_call, bot.parse_with_llm, bot.read_screenshot, bot.llm_json
_ids = itertools.count(1000)
SENT = []  # bot 送出的每則訊息
CALLS = []  # bot 送給 n8n 的每個請求


class FakeUser:
    def __init__(self, uid, name, is_bot=False):
        self.id, self.name, self.bot, self.mention = uid, name, is_bot, f"<@{uid}>"


class FakeChannel:
    def __init__(self, cid=1):
        self.id, self.guild = cid, None

    async def send(self, text, **kw):
        return FakeMsg(BOT_USER, self, text=text, kw=kw, record=True)

    async def fetch_message(self, message_id):
        return next(message for message in SENT if message.id == message_id)


class FakeMsg:
    def __init__(self, author, channel, text="", attachments=(), mentions=(), reference=None, kw=None, record=False):
        self.id, self.author, self.channel, self.content = next(_ids), author, channel, text
        self.attachments, self.mentions, self.reference, self.guild, self.kw = list(attachments), list(mentions), reference, None, kw or {}
        self.raw_mentions = [int(i) for i in re.findall(r"<@!?(\d+)>", text)]
        self.reactions, self.deleted = [], False
        self.buttons = [getattr(c, "custom_id", "") for c in (self.kw.get("view").children if self.kw.get("view") else [])]
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
        self.text = json.dumps(body, ensure_ascii=False)

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


def stub(settle_status=200, read_delay=0.0, sheet=None):
    SENT.clear()
    CALLS.clear()
    bot.SETTLEMENTS.clear()
    bot.PENDING.clear()
    bot.DELETE_CHOICES.clear()
    bot.ADD_PENDING.clear()
    bot._ledger_synced.clear()
    bot._ledger_uncertain.clear()
    bot.LEDGER_SYNC = False  # 寫「結算紀錄」分頁另外測，免得每個測試的表格呼叫紀錄裡都多一筆
    for lock_name in ("SETTLEMENT_LOCKS", "CHANNEL_LOCKS", "DELETE_CHOICE_LOCKS"):
        getattr(bot, lock_name, {}).clear()
    bot.ALLOWED_CHANNEL_IDS = set()
    bot.SETTLE_CONFIRM_SECONDS = 600

    async def fake_read(data, mime):
        await asyncio.sleep(read_delay)
        raw = json.loads(data)
        return bot.settle.clean_shot(dict(raw, items=[dict(i, paid_price=i["price"]) for i in raw["items"]]))

    async def fake_n8n(payload, url=None):
        CALLS.append(payload)
        if payload["action"] == "list":
            return FakeResp(200, {"status": "ok", "rows": SHEET if sheet is None else sheet})
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
    bot.read_screenshot, bot.sheet_call, bot.match_rows, bot.market_rate = fake_read, fake_n8n, fake_match, fake_rate
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
    check("草稿不 ping 任何人，有確認／取消按鈕", (d.content.split("\n")[0], d.kw["allowed_mentions"].to_dict()["parse"], d.buttons),
          ("🧾 **結算草稿**｜下單人 amy", [], ["sl:draft:ok", "sl:draft:no"]))
    check("先向結算用的 webhook 要清單", CALLS[0], {"action": "list"})
    await react("✅", BOB, d)
    check("別人按 ✅ → 不算", (settle_calls(), len(bot.PENDING)), ([], 1))
    await react("👍", AMY, d)
    check("下單人按別的表情 → 不算", settle_calls(), [])
    await react("✅", AMY, d)
    batch = settle_calls()[0]["batch"]
    check("下單人按 ✅ → 把對到的列寫回表格", settle_calls()[0],
          {"action": "settle", "batch": batch, "rows": [{"id": "", "tab": "淘寶", "row": 2, "name": "小米風扇落地扇", "buyer": "bob", "price": 100.0}]})
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
bot.local_vision_client = None
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

print("\n讀圖：本地模型優先，太慢或失敗退回 Groq")
LOCAL = object()  # 假的本地 client，只用來分辨是哪一邊被呼叫
EMPTY = {"items": [], "total_twd": 46, "order_no": "3317017045092056761"}


def vision2(local=(), groq=(), slow=0.0):
    """本地和 Groq 各自依序回傳的內容（Exception 就丟出）。回傳 (讀到的結果, 依序被呼叫的是哪一邊)。"""
    calls, queues = [], {"local": list(local), "groq": list(groq)}

    async def fake_llm_json(client, **kw):
        side = "local" if client is LOCAL else "groq"
        calls.append(side)
        if side == "local":
            await asyncio.sleep(slow)
        r = queues[side].pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    bot.llm_json = fake_llm_json
    try:
        shot = asyncio.run(real_read(b"img", "image/jpeg"))
    finally:
        bot.llm_json = real_llm_json
    return shot, calls


bot.local_vision_client, bot.local_vision_down_until, bot.LOCAL_VISION_TIMEOUT = LOCAL, 0.0, 5
shot, calls = vision2(local=[PENCIL])
check("本地讀得好 → 截圖不會送到 Groq", ([it["price"] for it in shot["items"]], calls), ([9.8], ["local"]))

shot, calls = vision2(local=[ConnectionError("refused")], groq=[PENCIL])
check("本地連不上 → 退回 Groq，照樣讀到", ([it["price"] for it in shot["items"]], calls), ([9.8], ["local", "groq"]))
shot, calls = vision2(groq=[PENCIL])
check("本地剛失敗過 → 接下來幾分鐘直接用 Groq，不讓每張圖都等", calls, ["groq"])
bot.local_vision_down_until = 0.0
shot, calls = vision2(local=[PENCIL])
check("冷卻時間過了 → 又先試本地", calls, ["local"])

bot.LOCAL_VISION_TIMEOUT = 0.05
shot, calls = vision2(local=[PENCIL], groq=[PENCIL], slow=0.5)
check("本地太慢（逾時）→ 退回 Groq", ([it["price"] for it in shot["items"]], calls, bot.local_vision_down_until > 0), ([9.8], ["local", "groq"], True))
bot.LOCAL_VISION_TIMEOUT, bot.local_vision_down_until = 5, 0.0

shot, calls = vision2(local=[EMPTY, EMPTY], groq=[PENCIL])
check("本地兩次都沒讀到商品 → 交給 Groq 再讀", ([it["price"] for it in shot["items"]], calls), ([9.8], ["local", "local", "groq"]))
check("本地只是讀不好、不是壞掉 → 下一張還是先試本地", bot.local_vision_down_until, 0.0)
PARTIAL = {"items": [{"name": "甲", "qty": 1, "paid_price": 10}, {"name": "乙", "qty": 1, "paid_price": "?"}], "total_twd": 60}
shot, calls = vision2(local=[PARTIAL, PARTIAL], groq=[{"items": []}, {"items": []}])
check("Groq 讀得更差 → 留本地讀到的", ([it["name"] for it in shot["items"]], shot["dropped"]), (["甲"], 1))
bot.local_vision_client, bot.local_vision_down_until = None, 0.0
shot, calls = vision2(groq=[PENCIL])
check("沒設定本地模型 → 只用 Groq", calls, ["groq"])

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
check("回覆上有結束認領按鈕、記進這次結算", (f"sl:close:{state['batch']}" in reply.buttons, reply.id in state["message_ids"]), (True, True))
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
check("有沒人認領的商品 → 正式結果上有認領、改歸屬、結束認領按鈕", final.buttons, [f"sl:claim:{batch}", f"sl:re:{batch}", f"sl:close:{batch}"])
check("別人按 🏁 → 沒反應", (press("🏁", BOB, final_id), state.get("closed")), ([], None))
check("下單人按別的表情 → 沒反應", (press("👍", AMY, final_id), state.get("closed")), ([], None))
check("在不相干的訊息按 🏁 → 沒反應", (press("🏁", AMY, 424242), state.get("closed")), ([], None))
out = press("🏁", AMY, final_id)
check("下單人按 🏁 → 公布最終金額", [m.content for m in out],
      [f"🏁 **結算 {batch} 結束認領**｜請轉帳給 <@100>\n\n"
       "**bob　TWD 495**\n　#1 小米風扇 ×1　¥100.00\n\n"
       "下單人自己的 TWD 495 不用轉，含沒人認領的 #2\n　#2 神秘商品 ×2　¥100.00"])
check("回覆在被按的那則訊息底下，而且只 ping 人", (out[0].kw["reference"].message_id, out[0].kw["allowed_mentions"].to_dict()["parse"]), (final_id, ["users"]))
check("沒人領的算給下單人，總額不變", bot.settle.totals(state)["twd"], {"bob": 495, "amy": 495})
check("再按一次不會重發", press("🏁", AMY, final_id), [])
check("結束後回覆那則訊息認領 → 拒絕", run_claim(CAROL, "認領 2", reply_to=final_id), f"結算 {batch} 已經結束認領了，有問題請直接找下單的人")
check("結束後不回覆直接認領 → 找不到", run_claim(CAROL, "認領 2")[:14], "找不到還沒人認領的 #2 🙅")

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT]))
run_claim(CAROL, "認領 2")
claim_reply = SENT[-1]
check("認領的回覆上也有按鈕，而且記進這次結算", (f"sl:close:{state['batch']}" in claim_reply.buttons, claim_reply.id in state["message_ids"]), (True, True))
out = press("🏁", AMY, claim_reply.id)
check("全部認領完，在認領回覆上按 🏁 → 公布並 ping 每個要付錢的人", out[0].content,
      f"🏁 **結算 {state['batch']} 結束認領**｜請轉帳給 <@100>\n\n"
      "**bob　TWD 495**\n　#1 小米風扇 ×1　¥100.00\n"
      "**<@300>　TWD 495**\n　#2 神秘商品 ×2　¥100.00")

stub()
asyncio.run(settle_and_confirm(AMY, [order([("小米風扇", 1, 100.0)], 495)]))
check("全部都對到人 → 沒有認領按鈕，但仍可改歸屬（AI 可能配錯）和結束", [b.split(":")[1] for b in published()[-1].buttons], ["re", "close"])

stub()
state = asyncio.run(settle_and_confirm(AMY, [SHOT], text="結算 <@300>", mentions=[CAROL], confirmer=CAROL))
out = press("🏁", AMY, published()[-1].id)
text = out[0].content if out else ""
check("代傳：代傳的人也能按 🏁，剩下的算給被幫忙的人",
      (text.split("\n")[0], "下單人自己的 TWD 495 不用轉，含沒人認領的 #2" in text, "　#2 神秘商品 ×2　¥100.00" in text),
      (f"🏁 **結算 {state['batch']} 結束認領**｜請轉帳給 <@300>", True, True))

print("\n已公布結算：回覆附圖追加")
stub()


async def published_append_trigger():
    state = await settle_and_confirm(AMY, [SHOT])
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    check("回覆已公布結算並附圖 → 建立追加草稿", len(bot.PENDING), 1)


asyncio.run(published_append_trigger())


def append_draft(author=AMY):
    return bot.PENDING[(CHAN.id, author.id)]["drafts"][-1]


def copy_state(state):
    return json.loads(json.dumps(state))


stub()


async def append_unmatched():
    state = await settle_and_confirm(AMY, [SHOT])
    batch, original_id = state["batch"], state["message_ids"][0]
    old = copy_state(state)
    writes_before = len(settle_calls())
    await send(AMY, "", [EARBUDS], reply_to=original_id)
    draft = append_draft()
    check("追加草稿只列新商品", ("藍牙耳機" in draft.content, "小米風扇" in draft.content, "神秘商品" in draft.content), (True, False, False))
    check("追加草稿沿用原批次", bot.PENDING[(1, AMY.id)]["state"]["batch"], batch)
    check("確認前原結算與表格維持原狀", (bot.SETTLEMENTS[batch], len(settle_calls())), (old, writes_before))
    await react("✅", BOB, draft)
    check("第三人按追加草稿 ✅ 無效", (len(bot.PENDING), len(settle_calls())), (1, writes_before))
    await react("✅", AMY, draft)
    current = bot.SETTLEMENTS[batch]
    check("追加後同一批次、商品編號接續", (list(bot.SETTLEMENTS), nos(current)), ([batch], [1, 2, 3]))
    check("新訂單實付只加一次", bot.settle.totals(current)["total_twd"], 1140)
    check("沒對到表格的追加不呼叫寫入", len(settle_calls()), writes_before)
    check("追加保留原結算訊息、登記更新訊息", (original_id in current["message_ids"], SENT[-1].id in current["message_ids"]), (True, True))
    check("追加公布可繼續結束認領", any(b.startswith("sl:close:") for b in SENT[-1].buttons), True)
    writes_after = len(settle_calls())
    await react("✅", AMY, draft)
    check("同一張追加草稿重按不重複加入", (nos(bot.SETTLEMENTS[batch]), len(settle_calls())), ([1, 2, 3], writes_after))
    await send(BOB, "認領 3", reply_to=original_id)
    check("舊正式訊息仍能認領新商品", bot.settle.find_item(bot.SETTLEMENTS[batch], 3)[1]["shares"][0]["owner"], "bob")


asyncio.run(append_unmatched())

stub(sheet=[])


async def pencil_fifteen():
    fourteen = order([(f"原商品{i}", 1, 10.0) for i in range(1, 15)], 6404, "5127000000000000014")
    state = await settle_and_confirm(AMY, [fourteen])
    batch = state["batch"]
    pencil = order([("适用applepencil笔尖", 1, 9.8)], 46, "3317017045092056761")
    await send(AMY, "", [pencil], reply_to=state["message_ids"][0])
    await react("✅", AMY, append_draft())
    state = bot.SETTLEMENTS[batch]
    check("筆尖補進原本十四件成 #15", (nos(state), bot.settle.find_item(state, 15)[1]["name"]), (list(range(1, 16)), "适用applepencil笔尖"))
    check("筆尖追加後同單合計 TWD 6450", (len(bot.SETTLEMENTS), bot.settle.totals(state)["total_twd"], settle_calls()), (1, 6450, []))


asyncio.run(pencil_fifteen())

print("\n追加確認：原下單人與代傳人的權限")
for approver in (AMY, CAROL):
    stub()

    async def append_proxy(approver=approver):
        state = await settle_and_confirm(AMY, [SHOT], text="結算 <@300>", mentions=[CAROL], confirmer=CAROL)
        batch = state["batch"]
        await send(BOB, "補圖 <@100>", [EARBUDS], reply_to=state["message_ids"][0], mentions=[AMY])
        draft = append_draft(BOB)
        await react("✅", BOB, draft)
        check("補圖的人不會取得原結算確認權", (len(bot.PENDING), nos(bot.SETTLEMENTS[batch])), (1, [1, 2]))
        await react("✅", approver, draft)
        state = bot.SETTLEMENTS[batch]
        check(f"原 {'代傳人' if approver is AMY else '下單人'}可以確認追加", (nos(state), state["payer"], state["payer_id"], state["poster_id"]), ([1, 2, 3], "carol", CAROL.id, AMY.id))

    asyncio.run(append_proxy())

stub()


async def append_legacy():
    state = await settle_and_confirm(AMY, [SHOT])
    state.pop("poster_id")
    await send(BOB, "", [EARBUDS], reply_to=state["message_ids"][0])
    await react("✅", BOB, append_draft(BOB))
    check("舊資料沒有 poster_id 時補圖人不能確認", len(bot.PENDING), 1)
    await react("✅", AMY, append_draft(BOB))
    check("舊資料沒有 poster_id 時下單人可確認", nos(bot.SETTLEMENTS[state["batch"]]), [1, 2, 3])


asyncio.run(append_legacy())

print("\n追加表格回寫與同筆付款重新分攤")
stub()


async def append_matched():
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    original_post = bot.sheet_call
    new_row = {"tab": "淘寶", "row": 3, "name": "藍牙耳機", "spec": "", "url": "", "qty": 1, "buyer": "carol"}

    async def changed_sheet(payload, url=None):
        if payload["action"] == "list":
            CALLS.append(payload)
            return FakeResp(200, {"rows": [new_row]})
        return await original_post(payload, url)

    bot.sheet_call = changed_sheet
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    await react("✅", AMY, append_draft())
    check("追加配對的新列用原 batch 寫回", settle_calls()[-1], {"action": "settle", "batch": batch, "rows": [{"id": "", "tab": "淘寶", "row": 3, "name": "藍牙耳機", "buyer": "carol", "price": 30.0}]})
    check("追加只回寫新列，舊列維持原價", len(settle_calls()), 2)
    check("新增配對人取得新增金額", bot.settle.totals(bot.SETTLEMENTS[batch])["twd"], {"bob": 495, "unclassified": 495, "carol": 150})


asyncio.run(append_matched())

stub()


async def append_shared_payment():
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    await send(CAROL, "認領 2", reply_to=state["message_ids"][0])
    state["mentions"].update(bob=BOB.mention, carol=CAROL.mention)
    overlapping = order([("神秘商品", 2, 50.0), ("藍牙耳機", 1, 30.0)], 990)
    await send(AMY, "", [overlapping], reply_to=state["message_ids"][0])
    draft = append_draft()
    check("同筆付款草稿只列真正新增的商品", ("藍牙耳機" in draft.content, "神秘商品" in draft.content), (True, False))
    await react("✅", AMY, draft)
    state = bot.SETTLEMENTS[batch]
    check("同付款補漏圖保持 TWD990 並去重", (len(state["units"]), nos(state)[:2], len(nos(state)), bot.settle.totals(state)["total_twd"]), (1, [1, 2], 3, 990))
    check("重新分攤保留舊認領", bot.settle.totals(state)["twd"], {"bob": 431, "carol": 430, "unclassified": 129})
    check("更新通知只允許金額有變動的人被 ping", sorted(int(uid) for uid in SENT[-1].kw["allowed_mentions"].to_dict().get("users", [])), [BOB.id, CAROL.id])
    check("追加通知不額外 ping 被回覆的草稿作者", SENT[-1].kw["allowed_mentions"].replied_user, False)


asyncio.run(append_shared_payment())

stub()


async def claim_during_append_draft():
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    draft = append_draft()
    await send(CAROL, "認領 2", reply_to=state["message_ids"][0])
    claim_reply_id = SENT[-1].id
    await react("✅", AMY, draft)
    state = bot.SETTLEMENTS[batch]
    check("追加草稿期間的認領不會被舊快照覆蓋", bot.settle.find_item(state, 2)[1]["shares"][0]["owner"], "carol")
    check("追加保留草稿期間的認領回覆參照", claim_reply_id in state["message_ids"], True)


asyncio.run(claim_during_append_draft())

print("\n追加取消、逾時、關閉與重傳")
for emoji in ("❌", "timeout"):
    stub()

    async def append_cancelled(emoji=emoji):
        state = await settle_and_confirm(AMY, [SHOT])
        batch, before, calls_before = state["batch"], copy_state(state), len(settle_calls())
        if emoji == "timeout":
            bot.SETTLE_CONFIRM_SECONDS = 0.02
        await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
        if emoji == "timeout":
            await asyncio.sleep(0.05)
        else:
            await react(emoji, AMY, append_draft())
        check(f"追加 {emoji} 保留原結算、不回寫", (bot.SETTLEMENTS[batch], len(settle_calls()), bot.PENDING), (before, calls_before, {}))

    asyncio.run(append_cancelled())

stub()


async def closed_append():
    state = await settle_and_confirm(AMY, [SHOT])
    original_id = state["message_ids"][0]
    await react("🏁", AMY, SimpleNamespace(id=original_id))
    before, calls_before = copy_state(state), len(CALLS)
    await send(AMY, "", [EARBUDS], reply_to=original_id)
    check("結束認領的單拒絕追加，連讀表都不做", (bot.SETTLEMENTS[state["batch"]], bot.PENDING, len(CALLS)), (before, {}, calls_before))
    check("已關閉追加會告知原因", "結束認領" in SENT[-1].content, True)


asyncio.run(closed_append())

stub()


async def closed_after_draft():
    state = await settle_and_confirm(AMY, [SHOT])
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    draft = append_draft()
    await react("🏁", AMY, SimpleNamespace(id=state["message_ids"][0]))
    before, calls_before = copy_state(state), len(settle_calls())
    await react("✅", AMY, draft)
    check("草稿建立後先關閉，再確認追加仍被拒絕", (bot.SETTLEMENTS[state["batch"]], len(settle_calls())), (before, calls_before))
    check("關閉後不能留下可確認的追加草稿", bot.PENDING, {})


asyncio.run(closed_after_draft())

stub()


async def duplicate_append():
    state = await settle_and_confirm(AMY, [SHOT])
    before, calls_before = copy_state(state), len(settle_calls())
    await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
    check("重傳完全相同的圖不會加出可確認的草稿", bot.PENDING, {})
    check("重傳完全相同的圖不變更金額與表格", (bot.SETTLEMENTS[state["batch"]], len(settle_calls())), (before, calls_before))


asyncio.run(duplicate_append())

for failure in (409, 500, "timeout"):
    stub()

    async def append_write_failure(failure=failure):
        state = await settle_and_confirm(AMY, [EARBUDS])
        before = copy_state(state)
        original_post = bot.sheet_call

        async def failing_write(payload, url=None):
            if payload["action"] != "settle":
                return await original_post(payload, url)
            CALLS.append(payload)
            if failure == "timeout":
                raise requests.exceptions.ReadTimeout()
            return FakeResp(failure, {"problems": [{"tab": "淘寶", "row": 2, "reason": "這一列已經不存在"}]})

        bot.sheet_call = failing_write
        await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
        await react("✅", AMY, append_draft())
        current = {key: value for key, value in bot.SETTLEMENTS[state["batch"]].items() if key != "append_attempt"}
        check(f"追加表格寫入 {failure} 保留原結算", current, before)
        if failure == "timeout":
            check("追加逾時告知可能已回寫", any("可能已經寫進表格" in message.content for message in SENT[-2:]), True)

    asyncio.run(append_write_failure())

stub()


async def append_write_retry():
    state = await settle_and_confirm(AMY, [EARBUDS])
    batch = state["batch"]
    original_post = bot.sheet_call
    attempts = []

    async def uncertain_first_write(payload, url=None):
        if payload["action"] == "settle":
            attempts.append(copy_state(payload))
            if len(attempts) == 1:
                CALLS.append(payload)
                raise requests.exceptions.ReadTimeout()
        return await original_post(payload, url)

    bot.sheet_call = uncertain_first_write
    bot.SETTLE_CONFIRM_SECONDS = 0.02
    await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
    draft = append_draft()
    await react("✅", AMY, draft)
    check("追加寫表逾時保留核准的操作紀錄", "append_attempt" in bot.SETTLEMENTS[batch], True)
    check("追加寫表逾時保留同一張可重試草稿", append_draft().id, draft.id)
    approved_shots = len(bot.PENDING[(1, AMY.id)]["shots"])
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    check("已送出寫表的追加不會再併進新截圖", len(bot.PENDING[(1, AMY.id)]["shots"]), approved_shots)
    await react("❌", AMY, draft)
    check("可能已寫入的追加不能取消丟棄", len(bot.PENDING), 1)
    await asyncio.sleep(0.05)
    check("可能已寫入的追加不因草稿逾時消失", len(bot.PENDING), 1)
    await react("✅", AMY, draft)
    check("追加重試使用完全相同的 batch 與列", (len(attempts), attempts[0] == attempts[1]), (2, True))
    check("追加重試成功只併入一次並清除操作紀錄", (nos(bot.SETTLEMENTS[batch]), "append_attempt" in bot.SETTLEMENTS[batch], bot.PENDING), ([1, 2, 3], False, {}))


asyncio.run(append_write_retry())

stub()


async def recover_approved_append():
    with tempfile.TemporaryDirectory() as state_dir:
        bot.STATE_DIR = state_dir
        try:
            state = await settle_and_confirm(AMY, [EARBUDS])
            batch, original_id = state["batch"], state["message_ids"][0]
            original_post = bot.sheet_call
            attempts = []

            async def uncertain_first_write(payload, url=None):
                if payload["action"] == "settle":
                    attempts.append(copy_state(payload))
                    if len(attempts) == 1:
                        CALLS.append(payload)
                        raise requests.exceptions.ReadTimeout()
                return await original_post(payload, url)

            bot.sheet_call = uncertain_first_write
            await send(AMY, "", [SHOT], reply_to=original_id)
            await react("✅", AMY, append_draft())
            saved = json.loads((Path(state_dir) / "settlements.json").read_text())
            check("核准後的追加寫表前先存檔", ("append_attempt" in saved[batch], nos(saved[batch])), (True, [1]))
            for pending in bot.PENDING.values():
                if pending["timer"]:
                    pending["timer"].cancel()
            bot.PENDING.clear()
            bot.SETTLEMENTS.clear()
            bot.load_settlements()
            await bot.recover_appends()
            current = bot.SETTLEMENTS[batch]
            check("重啟接續已核准追加時重送相同 batch 與列", (len(attempts), attempts[0] == attempts[1]), (2, True))
            check("重啟接續追加保留原訊息與新商品", (original_id in current["message_ids"], nos(current), "append_attempt" in current, bot.PENDING), (True, [1, 2, 3], False, {}))
            await bot.recover_appends()
            check("再次 ready 不重做已完成的追加", len(attempts), 2)
        finally:
            bot.STATE_DIR = None


asyncio.run(recover_approved_append())

stub()


async def restarting_append_reserves_numbers():
    state = await settle_and_confirm(AMY, [EARBUDS])
    original_post = bot.sheet_call

    async def uncertain_write(payload, url=None):
        if payload["action"] == "settle":
            CALLS.append(payload)
            raise requests.exceptions.ReadTimeout()
        return await original_post(payload, url)

    bot.sheet_call = uncertain_write
    await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
    await react("✅", AMY, append_draft())
    reserved = [item["no"] for unit in bot.SETTLEMENTS[state["batch"]]["append_attempt"]["added_units"] for item in unit["items"]]
    for pending in bot.PENDING.values():
        if pending["timer"]:
            pending["timer"].cancel()
    bot.PENDING.clear()
    fresh = order([("独立新商品", 1, 40.0)], 200, "5127000000000000003")
    await send(BOB, "結算", [fresh])
    fresh_nos = nos(bot.PENDING[(1, BOB.id)]["state"])
    check("重啟後尚未接續的已核准追加仍預留商品編號", min(fresh_nos) > max(reserved), True)


asyncio.run(restarting_append_reserves_numbers())

stub(read_delay=0.02)


async def append_burst_and_conflict():
    state = await settle_and_confirm(AMY, [SHOT])
    first_id = state["message_ids"][0]
    extra = order([("独立新商品", 1, 40.0)], 200, "5127000000000000003")
    await asyncio.gather(send(AMY, "", [EARBUDS], reply_to=first_id), send(AMY, "", [extra]))
    draft = append_draft()
    check("追加草稿多則附圖排隊合併", ("藍牙耳機" in draft.content, "独立新商品" in draft.content), (True, True))
    await react("✅", AMY, draft)
    check("追加多則附圖只增加各筆一次", (nos(bot.SETTLEMENTS[state["batch"]]), bot.settle.totals(bot.SETTLEMENTS[state["batch"]])["total_twd"]), ([1, 2, 3, 4], 1340))


asyncio.run(append_burst_and_conflict())

stub()


async def append_replacement_notification_failure():
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    await send(AMY, "", [EARBUDS], reply_to=state["message_ids"][0])
    visible_draft = append_draft()
    original_send_chunks = bot.send_chunks
    extra = order([("独立新商品", 1, 40.0)], 200, "5127000000000000003")

    async def failing_replacement(target, text, **kwargs):
        if text.startswith("🧾 **追加結算草稿"):
            raise RuntimeError("新草稿無法送出")
        return await original_send_chunks(target, text, **kwargs)

    bot.send_chunks = failing_replacement
    try:
        await send(AMY, "", [extra])
    finally:
        bot.send_chunks = original_send_chunks
    check("替代草稿送出失敗後仍保留原本可見草稿", (append_draft().id, visible_draft.deleted), (visible_draft.id, False))
    await react("✅", AMY, visible_draft)
    current = bot.SETTLEMENTS[batch]
    names = [item["name"] for unit in current["units"] for item in unit["items"]]
    check("舊草稿確認只併入使用者看過的追加商品", names, ["小米風扇", "神秘商品", "藍牙耳機"])
    check("替代草稿失敗不把看不到的金額併入", bot.settle.totals(current)["total_twd"], 1140)


asyncio.run(append_replacement_notification_failure())

stub()


async def concurrent_append_confirmations():
    state = await settle_and_confirm(AMY, [SHOT])
    original_id = state["message_ids"][0]
    extra = order([("独立新商品", 1, 40.0)], 200, "5127000000000000003")
    await asyncio.gather(send(AMY, "", [EARBUDS], reply_to=original_id), send(BOB, "", [extra], reply_to=original_id))
    amy_draft, bob_draft = append_draft(), append_draft(BOB)
    proposed = [bot.PENDING[(1, user.id)]["state"] for user in (AMY, BOB)]
    proposed_nos = [[n for n in nos(s) if n not in [1, 2]] for s in proposed]
    check("同時追加的兩張草稿預留不同編號", bool(set(proposed_nos[0]) & set(proposed_nos[1])), False)
    await asyncio.gather(react("✅", AMY, amy_draft), react("✅", AMY, bob_draft))
    current = bot.SETTLEMENTS[state["batch"]]
    names = [it["name"] for unit in current["units"] for it in unit["items"]]
    accepted = [name for name in ("藍牙耳機", "独立新商品") if name in names]
    check("同時確認至少保留第一筆追加且編號唯一", (len(accepted) >= 1, len(nos(current)) == len(set(nos(current)))), (True, True))
    if len(accepted) == 1:
        check("第二筆被拒時清楚說明結算已更新", any(("更新" in message.content or "追加確認" in message.content) and ("重新" in message.content or "重傳" in message.content) for message in SENT[-3:]), True)
    check("並行確認保留原本兩件", names[:2], ["小米風扇", "神秘商品"])


asyncio.run(concurrent_append_confirmations())

print("\n追加確認：表格等待期間的認領與結束認領")
stub()


async def claim_while_append_writes():
    state = await settle_and_confirm(AMY, [EARBUDS])
    batch = state["batch"]
    original_post = bot.sheet_call
    writing, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(payload, url=None):
        if payload["action"] == "settle":
            writing.set()
            await release.wait()
        return await original_post(payload, url)

    bot.sheet_call = delayed_write
    await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
    confirming = asyncio.create_task(react("✅", AMY, append_draft()))
    await writing.wait()
    claiming = asyncio.create_task(send(CAROL, "認領 1", reply_to=state["message_ids"][0]))
    await asyncio.sleep(0.01)
    check("追加寫表等待期間認領會排隊", claiming.done(), False)
    release.set()
    await asyncio.gather(confirming, claiming)
    current = bot.SETTLEMENTS[batch]
    check("寫表後認領與追加都保留", (nos(current), bot.settle.find_item(current, 1)[1]["shares"][0]["owner"]), ([1, 2, 3], "carol"))


asyncio.run(claim_while_append_writes())

stub()


async def close_while_append_writes():
    state = await settle_and_confirm(AMY, [EARBUDS])
    batch, original_id = state["batch"], state["message_ids"][0]
    original_post = bot.sheet_call
    writing, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(payload, url=None):
        if payload["action"] == "settle":
            writing.set()
            await release.wait()
        return await original_post(payload, url)

    bot.sheet_call = delayed_write
    await send(AMY, "", [SHOT], reply_to=original_id)
    confirming = asyncio.create_task(react("✅", AMY, append_draft()))
    await writing.wait()
    closing = asyncio.create_task(react("🏁", AMY, SimpleNamespace(id=original_id)))
    await asyncio.sleep(0.01)
    check("追加寫表等待期間結束認領會排隊", closing.done(), False)
    release.set()
    await asyncio.gather(confirming, closing)
    current = bot.SETTLEMENTS[batch]
    check("追加後結束認領會處理全部新舊商品", (nos(current), current["closed"], bot.settle.has_unclassified(current)), ([1, 2, 3], True, False))


asyncio.run(close_while_append_writes())

stub()


async def append_notification_failure():
    with tempfile.TemporaryDirectory() as state_dir:
        bot.STATE_DIR = state_dir
        original_send_chunks = bot.send_chunks
        try:
            state = await settle_and_confirm(AMY, [EARBUDS])
            batch = state["batch"]

            async def failing_result(target, text, **kwargs):
                if text.startswith(f"🧾 **結算 {batch} 已追加"):
                    raise RuntimeError("Discord 暫時無法送出訊息")
                return await original_send_chunks(target, text, **kwargs)

            bot.send_chunks = failing_result
            await send(AMY, "", [SHOT], reply_to=state["message_ids"][0])
            await react("✅", AMY, append_draft())
            check("寫表成功但通知失敗仍保存追加", (nos(bot.SETTLEMENTS[batch]), len(settle_calls())), ([1, 2, 3], 1))
            saved = json.loads((Path(state_dir) / "settlements.json").read_text())
            check("通知失敗後重啟資料仍含追加商品", nos(saved[batch]), [1, 2, 3])
        finally:
            bot.send_chunks = original_send_chunks
            bot.STATE_DIR = None


asyncio.run(append_notification_failure())

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
bot.sheet_call, bot.parse_with_llm = real_post, real_parse
bot.SETTLE_CONFIRM_SECONDS = 600

print("\n刪除：多筆先選編號")


async def delete_choices():
    stub()
    fans = [
        {"n": 1, "id": "fan-white", "sheet": "淘寶", "itemName": "小米風扇", "spec": "白色", "qty": 1, "url": "https://e.tb.cn/a"},
        {"n": 2, "id": "fan-usb", "sheet": "淘寶", "itemName": "USB風扇", "spec": "", "qty": 2, "url": "https://e.tb.cn/b"},
    ]
    seen = []
    moved = []  # 非空時，模擬表格在等待選擇期間被改過

    async def fake_parse(text):
        return {"action": "delete", "itemName": "風扇", "quantity": 1, "modelSpec": "", "url": ""}

    async def fake_n8n(payload, url=None):
        seen.append(dict(payload))
        if payload.get("rowId") and moved:
            return FakeResp(404, {"status": "changed", "found": False})
        if payload.get("rowId"):
            name = next(c["itemName"] for c in fans if c["id"] == payload["rowId"])
            return FakeResp(200, {"status": "deleted", "itemName": name})
        return FakeResp(404, {"status": "choose", "found": False, "choices": fans})

    bot.parse_with_llm = fake_parse
    bot.sheet_call = fake_n8n
    await send(AMY, "刪除風扇")
    prompt = SENT[-1]
    check("多筆先列出編號，而且還沒指定列", ("1." in prompt.content and "USB風扇" in prompt.content, seen[-1].get("rowId")), (True, None))
    await send(AMY, "刪除 2")
    check("「刪除 2」使用第二筆固定 ID", seen[-1].get("rowId"), "fan-usb")
    check("選擇不再依賴當時的列號", "rowNumber" in seen[-1], False)
    check("選完回報刪掉的名稱", "USB風扇" in SENT[-1].content, True)
    check("選完清掉選項", bot.DELETE_CHOICES, {})

    before = len(SENT)
    await send(AMY, "2")
    check("沒有選項時，純數字不刪", len(SENT), before)

    await send(AMY, "刪除風扇")
    old_prompt = SENT[-1]
    await send(AMY, "刪除風扇")
    new_prompt = SENT[-1]
    calls = len(seen)
    await send(AMY, "1", reply_to=old_prompt.id)
    check("回覆舊選項的數字不算", len(seen), calls)
    await send(AMY, "2", reply_to=new_prompt.id)
    check("回覆目前選項的 2 才刪", seen[-1].get("rowId"), "fan-usb")

    await send(AMY, "刪除風扇")
    bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["expires"] = time.monotonic() - 1
    calls = len(seen)
    await send(AMY, "刪除 1")
    check("過期不再刪，並請他重說", (len(seen), "過期" in SENT[-1].content), (calls, True))

    bot.DELETE_CHOICES.clear()
    await send(AMY, "刪除風扇")
    moved.append(True)
    await send(AMY, "刪除 2")
    check("選擇的固定 ID 已不存在 → 說明沒有刪、請他重說", SENT[-1].content,
          "你選的那一筆已經不在清單裡了（可能剛被刪掉），所以**沒有刪任何東西**。\n還要刪的話請重新說一次。")
    check("選項作廢，不能拿舊編號再試", bot.DELETE_CHOICES, {})
    moved.clear()

    await send(AMY, "刪除風扇")
    first_id = bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["id"]
    await send(AMY, "刪除風扇")
    check("新的一輪識別碼不會重用", bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["id"] != first_id, True)


asyncio.run(delete_choices())
bot.sheet_call, bot.parse_with_llm = real_post, real_parse

print("\n按鈕與選單：真實 dispatcher 路徑")


class FakeInteractionResponse:
    def __init__(self):
        self.done, self.events = False, []

    def is_done(self):
        return self.done

    async def _ack(self, kind, text=None, **kwargs):
        if self.done:
            raise RuntimeError("同一個 Discord interaction 被回覆兩次")
        self.done = True
        self.events.append({"kind": kind, "content": text, **kwargs})

    async def send_message(self, text, **kwargs):
        await self._ack("send", text, **kwargs)

    async def edit_message(self, content=None, **kwargs):
        await self._ack("edit", content, **kwargs)

    async def defer(self, **kwargs):
        await self._ack("defer", **kwargs)


class FakeInteraction:
    def __init__(self, user, message, channel=None):
        self.user, self.message = user, message
        self.channel = channel or message.channel
        self.guild = self.channel.guild
        self.response = FakeInteractionResponse()
        self.edits, self.followups = [], []
        self.followup = SimpleNamespace(send=self.followup_send)

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)

    async def followup_send(self, content=None, **kwargs):
        self.followups.append({"content": content, **kwargs})


async def component(user, message, custom_id, values=()):
    interaction = FakeInteraction(user, message)
    await bot.on_component(interaction, custom_id, list(values))
    return interaction


def ui_events(interaction):
    return interaction.response.events + interaction.edits + interaction.followups


async def component_settlement_after_reload():
    stub()
    await send(AMY, "結算", [SHOT])
    draft = drafts()[-1]
    denied = await component(BOB, draft, "sl:draft:ok")
    check("別人按確認按鈕只收到私人權限說明", (len(bot.PENDING), denied.response.events[0]["ephemeral"]), (1, True))
    accepted = await component(AMY, draft, "sl:draft:ok")
    state = next(iter(bot.SETTLEMENTS.values()))
    batch, published_message = state["batch"], published()[-1]
    check("確認按鈕會先 acknowledgement 再寫表", (accepted.response.events[0]["kind"], len(settle_calls())), ("defer", 1))
    stale = await component(AMY, draft, "sl:draft:ok")
    check("重按已確認草稿不會重寫表格", (len(settle_calls()), "已經確認" in stale.response.events[0]["content"]), (1, True))

    saved = copy_state(bot.SETTLEMENTS)
    bot.PENDING.clear()
    bot.SETTLEMENTS.clear()
    bot.SETTLEMENTS.update(saved)
    state = bot.SETTLEMENTS[batch]
    original = published_message.kw["view"].children[0]
    rebuilt = await bot.SLButton.from_custom_id(None, original.item, None)
    chooser = FakeInteraction(CAROL, published_message)
    await rebuilt.callback(chooser)
    event = chooser.response.events[0]
    menu = event["view"].children[0]
    check("重啟後舊結算按鈕仍可重建認領選單", (event["ephemeral"], menu.custom_id, [o.value for o in menu.item.options]),
          (True, f"sls:claim:{batch}", ["2"]))
    rebuilt_menu = await bot.SLSelect.from_custom_id(None, menu.item, None)
    check("認領選單重建保留可選商品與上限", ([o.value for o in rebuilt_menu.item.options], rebuilt_menu.item.max_values), (["2"], 1))
    claimed = await component(CAROL, published_message, f"sls:claim:{batch}", ["2"])
    check("認領選單實際更新歸屬與每人金額", bot.settle.totals(state)["twd"], {"bob": 495, "carol": 495})
    check("認領選單完成後公布可繼續操作的訊息", ("認領好了" in "".join(e.get("content") or "" for e in ui_events(claimed)),
                                               SENT[-1].id in state["message_ids"]), (True, True))
    denied = await component(BOB, published_message, f"sl:close:{batch}")
    check("第三人不能按結束認領", (state.get("closed", False), denied.response.events[0]["ephemeral"]), (False, True))
    closed = await component(AMY, published_message, f"sl:close:{batch}")
    check("重啟後原下單人仍可從舊按鈕結束認領", (state["closed"], closed.response.events[0]["kind"]), (True, "defer"))
    again = await component(CAROL, published_message, f"sl:claim:{batch}")
    check("已結束結算的舊認領按鈕會告知結束", "已經結束認領" in again.response.events[0]["content"], True)


asyncio.run(component_settlement_after_reload())


async def component_claim_long_names_splits_notifications():
    stub(sheet=[])
    shot = order([("長" * 78 + f"{i:02d}", 1, 1.0) for i in range(25)], 2500)
    await send(AMY, "結算", [shot])
    draft = bot.PENDING[(CHAN.id, AMY.id)]["drafts"][-1]
    await component(AMY, draft, "sl:draft:ok")
    state = next(iter(bot.SETTLEMENTS.values()))
    message = await CHAN.fetch_message(state["message_ids"][-1])
    original_send = CHAN.send

    async def limited_send(text, **kwargs):
        if len(text) > 2000:
            raise ValueError("Discord message exceeds 2000 characters")
        return await original_send(text, **kwargs)

    CHAN.send = limited_send
    sent_before = len(SENT)
    try:
        await component(CAROL, message, f"sls:claim:{state['batch']}", [str(i) for i in range(1, 26)])
        notifications = SENT[sent_before:]
        check("按鈕一次認領 25 件長名稱商品時通知自動分段",
              (len(notifications) > 1, all(len(m.content) <= 2000 for m in notifications)), (True, True))
        check("長認領通知每一段都可回覆且管理按鈕放在末段",
              (all(m.id in state["message_ids"] for m in notifications),
               ["view" in m.kw for m in notifications]),
              (True, [False] * (len(notifications) - 1) + [True]))
        check("長認領通知保留正確認領金額並只允許認領者通知",
              (bot.settle.totals(state)["twd"], [m.kw["allowed_mentions"].users for m in notifications]),
              ({"carol": 2500}, [[CAROL]] * len(notifications)))
    finally:
        CHAN.send = original_send


asyncio.run(component_claim_long_names_splits_notifications())


async def component_reassign():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch, message = state["batch"], published()[-1]
    bot.settle.find_item(state, 1)[1]["row_ids"] = ["stable-fan-id"]
    denied = await component(CAROL, message, f"sl:re:{batch}")
    check("改歸屬按鈕限制原下單人或代傳人", denied.response.events[0]["ephemeral"], True)
    asked = await component(AMY, message, f"sl:re:{batch}")
    menu = asked.response.events[0]["view"].children[0]
    check("改歸屬先讓下單人選商品", [o.value for o in menu.item.options], ["1", "2"])
    picked = await component(AMY, message, f"sls:re:{batch}", ["1"])
    user_menu = picked.response.events[0]["view"].children[0]
    check("選完商品接著選新歸屬人", user_menu.custom_id, f"slu:re:{batch}:1")
    rebuilt = await bot.SLUserSelect.from_custom_id(None, user_menu.item, None)
    check("選人元件識別碼可從舊訊息還原", rebuilt.item.custom_id, f"slu:re:{batch}:1")
    await component(AMY, message, f"slu:re:{batch}:1", [CAROL])
    check("選人後更改歸屬並以帳本交易釋放舊配對列", (bot.settle.find_item(state, 1)[1]["shares"],
                                               {key: CALLS[-1][key] for key in ("action", "batch", "released_ids")}),
          ([{"owner": "carol", "cny": 100.0}], {"action": "ledger", "batch": batch, "released_ids": ["stable-fan-id"]}))
    check("改歸屬結果訊息可繼續追加與管理", SENT[-1].id in state["message_ids"], True)
    before = copy_state(state)
    refused = await component(AMY, message, f"slu:re:{batch}:1", [BOT_USER])
    check("選人元件拒絕將商品改給 bot", (state == before, "不能改給機器人" in refused.response.events[0]["content"]), (True, True))


asyncio.run(component_reassign())


async def component_duplicate_add():
    stub()
    requests_seen = []

    async def duplicate_store(payload):
        requests_seen.append(copy_state(payload))
        if payload.get("force"):
            return FakeResp(200, {"status": "added"})
        return FakeResp(409, {"status": "duplicate", "rows": [
            {"itemName": "小米風扇", "spec": "白色", "qty": 1, "id": "existing-fan"}]})

    bot.sheet_call = duplicate_store
    request = {"action": "add", "senderName": AMY.name, "senderId": str(AMY.id), "itemName": "小米風扇",
               "modelSpec": "白色", "quantity": 1, "url": "https://e.tb.cn/fan", "shareText": "買風扇"}
    await bot.send_add(FakeMsg(AMY, CHAN), request)
    prompt = SENT[-1]
    token = next(iter(bot.ADD_PENDING))
    check("重複登記會先提供再加一筆與取消按鈕", prompt.buttons, [f"sl:dup:{token}:y", f"sl:dup:{token}:n"])
    denied = await component(BOB, prompt, f"sl:dup:{token}:y")
    check("其他人不能替登記者確認重複新增", (len(requests_seen), denied.response.events[0]["ephemeral"]), (1, True))
    await component(AMY, prompt, f"sl:dup:{token}:y")
    check("原登記者按再加一筆才送 force 新增", (len(requests_seen), requests_seen[-1]["force"], bot.ADD_PENDING), (2, True, {}))
    await component(AMY, prompt, f"sl:dup:{token}:y")
    check("再按舊重複新增按鈕不會再寫一次", len(requests_seen), 2)
    await bot.send_add(FakeMsg(AMY, CHAN), request)
    token = next(iter(bot.ADD_PENDING))
    await component(AMY, SENT[-1], f"sl:dup:{token}:n")
    check("不用了會清除待確認新增", (bot.ADD_PENDING, len(requests_seen)), ({}, 3))
    await bot.send_add(FakeMsg(AMY, CHAN), request)
    token = next(iter(bot.ADD_PENDING))
    bot.ADD_PENDING[token]["expires"] = time.monotonic() - 1
    expired = await component(AMY, SENT[-1], f"sl:dup:{token}:y")
    check("過期重複新增按鈕不寫表格", (len(requests_seen), "過期" in expired.response.events[0]["content"]), (4, True))


asyncio.run(component_duplicate_add())


async def component_delete_buttons():
    stub()
    choices = [{"n": 1, "id": "fan-white", "itemName": "小米風扇", "spec": "白色", "qty": 1},
               {"n": 2, "id": "fan-black", "itemName": "小米風扇", "spec": "黑色", "qty": 1}]
    await bot.offer_delete_choices(FakeMsg(AMY, CHAN), choices)
    prompt = SENT[-1]
    token = bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["id"]
    calls = []

    async def delete_store(payload):
        calls.append(payload)
        return FakeResp(200, {"status": "deleted", "itemName": "小米風扇"})

    bot.sheet_call = delete_store
    denied = await component(BOB, prompt, f"sl:del:{token}:2")
    check("他人不能選刪除按鈕", (calls, denied.response.events[0]["ephemeral"]), ([], True))
    await component(AMY, prompt, f"sl:del:{token}:2")
    check("刪除按鈕送出所選固定 ID", (calls[0]["rowId"], bot.DELETE_CHOICES), ("fan-black", {}))
    await component(AMY, prompt, f"sl:del:{token}:2")
    check("重按已完成刪除按鈕不重刪", len(calls), 1)
    await bot.offer_delete_choices(FakeMsg(AMY, CHAN), choices)
    token = bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["id"]
    await component(AMY, SENT[-1], f"sl:del:{token}:0")
    check("都不要刪會清除待選且不寫表格", (bot.DELETE_CHOICES, len(calls)), ({}, 1))


asyncio.run(component_delete_buttons())


async def old_delete_button_after_restart():
    stub()
    choices = [{"n": 1, "id": "old-fan", "itemName": "舊風扇", "spec": "", "qty": 1}]
    await bot.offer_delete_choices(FakeMsg(AMY, CHAN), choices)
    old_prompt = SENT[-1]
    old_button = old_prompt.buttons[0]
    bot.DELETE_CHOICES.clear()  # 重啟後待選狀態消失
    replacement = [{"n": 1, "id": "new-toothbrush", "itemName": "新牙刷", "spec": "", "qty": 1}]
    await bot.offer_delete_choices(FakeMsg(AMY, CHAN), replacement)
    writes = []

    async def delete_store(payload):
        writes.append(payload)
        return FakeResp(200, {"status": "deleted", "itemName": "新牙刷"})

    bot.sheet_call = delete_store
    await component(AMY, old_prompt, old_button)
    check("重啟後舊刪除按鈕不能核准新的一筆刪除", writes, [])
    check("舊按鈕不會收掉新的待選刪除", bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["choices"][0]["id"], "new-toothbrush")


asyncio.run(old_delete_button_after_restart())


async def delete_choice_replaced_during_acknowledgement():
    stub()
    await bot.offer_delete_choices(FakeMsg(AMY, CHAN), [
        {"n": 1, "id": "old-fan", "itemName": "舊風扇", "spec": "", "qty": 1}])
    old_prompt, old_button = SENT[-1], SENT[-1].buttons[0]
    interaction = FakeInteraction(AMY, old_prompt)
    acknowledge = interaction.response.defer

    async def replace_while_acknowledging(**kwargs):
        await acknowledge(**kwargs)
        await bot.offer_delete_choices(FakeMsg(AMY, CHAN), [
            {"n": 1, "id": "new-toothbrush", "itemName": "新牙刷", "spec": "", "qty": 1}])

    interaction.response.defer = replace_while_acknowledging
    writes = []

    async def delete_store(payload):
        writes.append(payload)
        return FakeResp(200, {"status": "deleted", "itemName": "新牙刷"})

    bot.sheet_call = delete_store
    await bot.on_component(interaction, old_button, [])
    check("刪除按鈕 acknowledgement 期間換了一輪選項不會刪新資料", writes, [])
    check("舊刪除操作保留 acknowledgement 期間建立的新選項", bot.DELETE_CHOICES[(CHAN.id, AMY.id)]["choices"][0]["id"], "new-toothbrush")


asyncio.run(delete_choice_replaced_during_acknowledgement())


async def old_duplicate_button_after_restart():
    stub()
    writes = []

    async def duplicate_store(payload):
        writes.append(copy_state(payload))
        if payload.get("force"):
            return FakeResp(200, {"status": "added"})
        return FakeResp(409, {"status": "duplicate", "rows": [
            {"itemName": payload["itemName"], "spec": "", "qty": 1, "id": "existing-item"}]})

    bot.sheet_call = duplicate_store
    request = {"action": "add", "senderName": AMY.name, "senderId": str(AMY.id), "itemName": "舊風扇",
               "modelSpec": "", "quantity": 1, "url": "https://e.tb.cn/fan", "shareText": "買風扇"}
    await bot.send_add(FakeMsg(AMY, CHAN), request)
    old_prompt, old_button = SENT[-1], SENT[-1].buttons[0]
    bot.ADD_PENDING.clear()
    await bot.send_add(FakeMsg(AMY, CHAN), {**request, "itemName": "新牙刷", "url": "https://e.tb.cn/brush"})
    await component(AMY, old_prompt, old_button)
    check("重啟後舊重複登記按鈕不能核准另一件商品", [p for p in writes if p.get("force")], [])
    check("舊新增按鈕保留目前待確認商品", next(iter(bot.ADD_PENDING.values()))["payload"]["itemName"], "新牙刷")


asyncio.run(old_duplicate_button_after_restart())


async def mutation_acknowledged_before_waiting(kind):
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch, message = state["batch"], published()[-1]
    entered, released = asyncio.Event(), asyncio.Event()
    interaction = FakeInteraction(AMY if kind == "reassign" else CAROL, message)
    function_name = "apply_reassign" if kind == "reassign" else "apply_claims"
    original = getattr(bot, function_name)

    async def delayed(*args, **kwargs):
        entered.set()
        await released.wait()
        return await original(*args, **kwargs)

    setattr(bot, function_name, delayed)
    try:
        custom_id = f"slu:re:{batch}:1" if kind == "reassign" else f"sls:claim:{batch}"
        values = [CAROL] if kind == "reassign" else ["2"]
        operation = asyncio.create_task(bot.on_component(interaction, custom_id, values))
        await entered.wait()
        acknowledged = interaction.response.is_done()
        released.set()
        await operation
        check(f"{kind} 選單在等待表格或批次鎖前已回覆 Discord", acknowledged, True)
    finally:
        setattr(bot, function_name, original)


asyncio.run(mutation_acknowledged_before_waiting("reassign"))
asyncio.run(mutation_acknowledged_before_waiting("claim"))

print("\n雲端結算正本：先成功存檔才提交操作")


async def stop_test_ledger_sync():
    task = bot._ledger_task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    bot._ledger_task = None
    bot._sheet_ready = False
    bot.LEDGER_SYNC = False


async def cloud_claim_failure_and_retry():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch, before = state["batch"], copy_state(state)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    attempts = []
    fail = True

    async def cloud(payload):
        attempts.append(copy_state(payload))
        if payload["action"] == "states":
            return FakeResp(200, {"states": {batch: before}})
        return FakeResp(500 if fail else 200, {"status": "error" if fail else "ok"})

    bot.sheet_call = cloud
    try:
        claimed, errors = await bot.apply_claims([state], state, [2], CAROL)
        check("認領的雲端交易失敗時歸屬保持原狀", (claimed, state), ({}, before))
        check("認領失敗會告知重試且不標為已同步", (len(errors), batch in bot._ledger_synced), (1, False))
        check("認領寫入的雲端候選狀態包含新歸屬", bot.settle.find_item(attempts[0]["state"], 2)[1]["shares"],
              [{"owner": "carol", "cny": 100.0}])
        check("認領雲端請求的公開明細同步歸屬與台幣金額",
              [(row[6], row[7], row[9]) for row in attempts[0]["block"] if row[1] == 2],
              [("carol", 495, "認領中")])
        fail = False
        claimed, errors = await bot.apply_claims([state], state, [2], CAROL)
        check("雲端恢復後同一次認領可重試成功", (claimed, errors, state["_version"]), ({batch: [2]}, [], 1))
        check("成功後記憶體狀態等於已寫入的雲端狀態", state, attempts[-1]["state"])
    finally:
        await stop_test_ledger_sync()


asyncio.run(cloud_claim_failure_and_retry())


async def cloud_reassign_failure_retains_row_ids():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    item = bot.settle.find_item(state, 1)[1]
    item["row_ids"] = ["stable-fan-id"]
    before = copy_state(state)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    attempts = []

    async def failing_cloud(payload):
        attempts.append(copy_state(payload))
        raise requests.exceptions.ReadTimeout()

    bot.sheet_call = failing_cloud
    try:
        done, explanation = await bot.apply_reassign(state, [1], CAROL)
        check("改歸屬的雲端交易失敗保留原歸屬與固定列 ID", (done, state), (False, before))
        check("改歸屬把清理列和新狀態放在同一個雲端請求", (attempts[0]["action"], attempts[0]["released_ids"],
                                                            bot.settle.find_item(attempts[0]["state"], 1)[1]["row_ids"]),
              ("ledger", ["stable-fan-id"], []))
        check("失敗的改歸屬可明確重試", "重試" in explanation, True)
    finally:
        await stop_test_ledger_sync()


asyncio.run(cloud_reassign_failure_retains_row_ids())


async def cloud_close_failure_keeps_settlement_open():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    before, sent = copy_state(state), len(SENT)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    attempts = []
    fail = True

    async def failing_cloud(payload):
        attempts.append(copy_state(payload))
        if payload["action"] == "states":
            return FakeResp(200, {"states": {state["batch"]: before}})
        return FakeResp(503 if fail else 200, {"status": "error" if fail else "ok"})

    bot.sheet_call = failing_cloud
    try:
        reason = await bot.close_settlement(state, CHAN, state["message_ids"][0])
        check("結束認領存檔失敗時保留開放狀態及原有歸屬", state, before)
        check("結束認領存檔失敗會回覆原因且不公布最終款項", (bool(reason), len(SENT)), (True, sent))
        check("結束認領的雲端候選紀錄包含封單狀態與下單人承接商品",
              (attempts[0]["state"]["closed"], attempts[0]["state"]["leftover"],
               bot.settle.find_item(attempts[0]["state"], 2)[1]["shares"]),
              (True, [2], [{"owner": "amy", "cny": 100.0}]))
        check("結束認領的公開帳本每列均標為已結束", {row[9] for row in attempts[0]["block"]}, {"已結束"})
        fail = False
        reason = await bot.close_settlement(state, CHAN, state["message_ids"][0])
        check("結束認領重試成功後才公布且記憶體等於雲端正本",
              (reason, len(SENT) > sent, state), ("", True, attempts[-1]["state"]))
    finally:
        await stop_test_ledger_sync()


asyncio.run(cloud_close_failure_keeps_settlement_open())


async def cloud_accepts_mutation_despite_lost_response(operation):
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    state["_version"] = 7
    bot.settle.find_item(state, 1)[1]["row_ids"] = ["stable-fan-id"]
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    cloud_states, requests_seen = {}, []

    async def cloud(payload):
        requests_seen.append(copy_state(payload))
        if payload["action"] == "states":
            return FakeResp(200, {"states": copy_state(cloud_states)})
        cloud_states[payload["batch"]] = copy_state(payload["state"])
        raise requests.exceptions.ReadTimeout("accepted cloud mutation response lost")

    bot.sheet_call = cloud
    try:
        if operation == "claim":
            done, errors = await bot.apply_claims([state], state, [2], CAROL)
            success = done == {state["batch"]: [2]} and not errors
        elif operation == "reassign":
            success, reason = await bot.apply_reassign(state, [1], CAROL)
        else:
            success = not await bot.close_settlement(state, CHAN, state["message_ids"][0])
        check(f"{operation} 寫入逾時後讀回相同雲端狀態可確認成功",
              (success, [p["action"] for p in requests_seen], state["_version"], state),
              (True, ["ledger", "states"], 8, cloud_states[state["batch"]]))
        if operation == "reassign":
            check("改歸屬逾時讀回確認的交易包含原列釋放", requests_seen[0]["released_ids"], ["stable-fan-id"])
    finally:
        await stop_test_ledger_sync()


for operation in ("claim", "reassign", "close"):
    asyncio.run(cloud_accepts_mutation_despite_lost_response(operation))


async def cloud_readback_requires_matching_contents():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    before = copy_state(state)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    written = {}

    async def cloud(payload):
        if payload["action"] == "ledger":
            written.update(copy_state(payload["state"]))
            raise requests.exceptions.ReadTimeout("cloud result uncertain")
        stale = copy_state(written)
        bot.settle.find_item(stale, 2)[1]["shares"] = copy_state(bot.settle.find_item(before, 2)[1]["shares"])
        return FakeResp(200, {"states": {state["batch"]: stale}})

    bot.sheet_call = cloud
    try:
        done, errors = await bot.apply_claims([state], state, [2], CAROL)
        check("雲端讀回版本相同但歸屬不同時不可誤當成功", (done, bool(errors), state), ({}, True, before))
    finally:
        await stop_test_ledger_sync()


asyncio.run(cloud_readback_requires_matching_contents())


async def uncertain_claim_blocks_stale_overwrite(accepted):
    stub(sheet=[])
    shot = order([("待認領商品 A", 1, 100.0), ("待認領商品 B", 1, 100.0)], 990)
    state = await settle_and_confirm(AMY, [shot])
    batch, before = state["batch"], copy_state(state)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    cloud_states, ledger_requests = {batch: copy_state(before)}, []
    readback_available = False

    async def cloud(payload):
        if payload["action"] == "states":
            if not readback_available:
                raise requests.exceptions.ConnectionError("canonical state unavailable")
            return FakeResp(200, {"states": copy_state(cloud_states)})
        ledger_requests.append(copy_state(payload))
        if len(ledger_requests) == 1:
            if accepted:
                cloud_states[batch] = copy_state(payload["state"])
            raise requests.exceptions.ReadTimeout("first claim response lost")
        cloud_states[batch] = copy_state(payload["state"])
        return FakeResp(200, {"status": "ok"})

    bot.sheet_call = cloud
    try:
        done, errors = await bot.apply_claims([state], state, [1], CAROL)
        check(f"初次認領 accepted={accepted} 且結果無法讀回時保留不確定紀錄",
              (done, bool(errors), state, batch in bot._ledger_uncertain), ({}, True, before, True))
        done, errors = await bot.apply_claims([state], state, [2], BOB)
        check(f"前次認領 accepted={accepted} 尚無法確認時擋住另一筆舊快照覆寫",
              (done, bool(errors), len(ledger_requests), state), ({}, True, 1, before))
        readback_available = True
        done, errors = await bot.apply_claims([state], state, [2], BOB)
        if accepted:
            check("讀回已接受認領後原地還原正本且拒絕這次舊快照",
                  (done, bool(errors), state is bot.SETTLEMENTS[batch], state,
                   len(ledger_requests), batch in bot._ledger_uncertain),
                  ({}, True, True, cloud_states[batch], 1, False))
            done, errors = await bot.apply_claims([state], state, [2], BOB)
        expected = [{"owner": "carol" if accepted else "unclassified", "cny": 100.0}]
        check(f"canonical 確認 accepted={accepted} 後新認領保留前一件正確歸屬",
              (done, errors, bot.settle.find_item(state, 1)[1]["shares"],
               bot.settle.find_item(state, 2)[1]["shares"], state, len(ledger_requests),
               batch in bot._ledger_uncertain),
              ({batch: [2]}, [], expected, [{"owner": "bob", "cny": 100.0}], cloud_states[batch], 2, False))
    finally:
        await stop_test_ledger_sync()


for accepted in (True, False):
    asyncio.run(uncertain_claim_blocks_stale_overwrite(accepted))


async def prepare_sheet_uploads_existing_settlements():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    bot.settle.find_item(state, 1)[1].pop("row_ids", None)  # v1 已公布紀錄尚未存過固定 ID
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    writes = []

    async def initial_cloud(payload):
        action = payload["action"]
        if action == "layout":
            return FakeResp(200, {"done": []})
        if action == "states":
            return FakeResp(200, {"states": {}})
        if action == "all_rows":
            return FakeResp(200, {"rows": [{**SHEET[0], "id": "stable-fan-id", "batch": batch, "price": 100.0}]})
        if action == "ledger":
            writes.append(copy_state(payload))
            return FakeResp(200, {"status": "ok"})
        raise AssertionError(f"Unexpected cloud request {action}")

    bot.sheet_call = initial_cloud
    try:
        await bot.prepare_sheet()
        check("首次部署會把既有本機結算寫入雲端正本", [p["batch"] for p in writes], [batch])
        check("首次同步保留原商品與原認領資料", nos(writes[0]["state"]), [1, 2])
        check("舊版配對列可唯一對應時補固定 ID", bot.settle.find_item(state, 1)[1]["row_ids"], ["stable-fan-id"])
        check("啟動同步完成的狀態標記與記憶體一致", bot._ledger_synced[batch], bot._snapshot(state))
    finally:
        await stop_test_ledger_sync()


asyncio.run(prepare_sheet_uploads_existing_settlements())


async def prepare_sheet_prefers_newer_cloud_state():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    state["_version"] = 2
    remote = copy_state(state)
    remote["_version"] = 4
    bot.settle.claim(remote, 2, CAROL.name)
    remote["mentions"][CAROL.name] = CAROL.mention
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    writes = []

    async def restored_cloud(payload):
        action = payload["action"]
        if action == "layout":
            return FakeResp(200, {"done": []})
        if action == "states":
            return FakeResp(200, {"states": {batch: copy_state(remote)}})
        if action == "all_rows":
            return FakeResp(200, {"rows": []})
        if action == "ledger":
            writes.append(copy_state(payload))
            return FakeResp(200, {"status": "ok"})
        raise AssertionError(f"Unexpected cloud request {action}")

    bot.sheet_call = restored_cloud
    try:
        await bot.prepare_sheet()
        restored = bot.SETTLEMENTS[batch]
        check("重啟以較新版雲端狀態蓋過過時本機快取", (restored["_version"], bot.settle.find_item(restored, 2)[1]["shares"]),
              (4, [{"owner": "carol", "cny": 100.0}]))
        check("較舊的本機狀態不會反向覆寫雲端", writes, [])
    finally:
        await stop_test_ledger_sync()


asyncio.run(prepare_sheet_prefers_newer_cloud_state())


async def background_ledger_retries_transient_failure():
    stub()
    state = await settle_and_confirm(AMY, [SHOT])
    batch = state["batch"]
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    previous_retry = getattr(bot, "LEDGER_RETRY_SECONDS", 5)
    bot.LEDGER_RETRY_SECONDS = 0
    attempts = []

    async def transient_cloud(payload):
        if payload["action"] == "states":
            return FakeResp(200, {"states": {}})
        attempts.append(copy_state(payload))
        if len(attempts) == 1:
            raise requests.exceptions.ConnectionError("temporary outage")
        return FakeResp(200, {"status": "ok"})

    bot.sheet_call = transient_cloud
    try:
        await bot.sync_ledger(delay=0)
        check("背景帳本同步會自行重試暫時連線錯誤", len(attempts), 2)
        check("背景同步重試保留原批次及正確版本", ([p["batch"] for p in attempts], state["_version"]), ([batch, batch], 1))
        check("背景重試成功後才標記為已同步", bot._ledger_synced[batch], bot._snapshot(state))
    finally:
        bot.LEDGER_RETRY_SECONDS = previous_retry
        await stop_test_ledger_sync()


asyncio.run(background_ledger_retries_transient_failure())

print("\n舊資料固定 ID 遷移與追加核准快取失敗")


def legacy_row_id_migration_commits_only_complete_items():
    def legacy_item(no, labels, row_ids=None):
        item = {"no": no, "rows": labels, "shares": [{"owner": "bob", "cny": 100.0}], "line": 100.0}
        if row_ids is not None:
            item["row_ids"] = row_ids
        return item

    rows = [{"tab": "淘寶", "batch": "legacy", "buyer": "bob", "price": 100.0, "id": "stable-fan-id"}]
    failed = legacy_item(1, ["淘寶第 2 列", "京東第 2 列"])
    complete = legacy_item(2, ["淘寶第 3 列"])
    state = {"batch": "legacy", "units": [{"items": [failed, complete]}]}
    bot.migrate_row_ids(state, rows)
    check("一件商品只有部分列可遷移時不提交固定 ID", failed.get("row_ids"), None)
    check("遷移失敗的商品不占用下一件可唯一確認的固定 ID", complete.get("row_ids"), ["stable-fan-id"])

    blank = legacy_item(3, ["淘寶第 4 列"], [""])
    state["units"][0]["items"] = [blank]
    bot.migrate_row_ids(state, rows)
    check("舊快取裡空字串固定 ID 仍會補齊", blank.get("row_ids"), ["stable-fan-id"])

    partial = legacy_item(4, ["淘寶第 5 列", "京東第 5 列"], ["stable-fan-id", ""])
    state["units"][0]["items"] = [partial]
    rows.append({"tab": "京東", "batch": "legacy", "buyer": "bob", "price": 100.0, "id": "stable-earbuds-id"})
    bot.migrate_row_ids(state, rows)
    check("部分已知的固定 ID 保留原值並補齊其餘列", partial.get("row_ids"), ["stable-fan-id", "stable-earbuds-id"])


legacy_row_id_migration_commits_only_complete_items()


async def reassign_requires_complete_row_ids():
    for row_ids in ([""], ["stable-fan-id", ""]):
        stub()
        state = await settle_and_confirm(AMY, [SHOT])
        item = bot.settle.find_item(state, 1)[1]
        item["rows"] = [f"淘寶第 {i + 2} 列" for i in range(len(row_ids))]
        item["row_ids"] = list(row_ids)
        before, writes = copy_state(state), len(CALLS)
        done, reason = await bot.apply_reassign(state, [1], CAROL)
        check(f"固定 ID 未完整補齊 {row_ids!r} 時不更改歸屬或遺失原表格配對",
              (done, "無法唯一確認" in reason, state, len(CALLS)), (False, True, before, writes))


asyncio.run(reassign_requires_complete_row_ids())


async def approved_append_recovers_when_local_cache_write_fails():
    stable_rows = [{**SHEET[0], "id": "stable-fan-id"}]
    stub(sheet=stable_rows)
    state = await settle_and_confirm(AMY, [EARBUDS])
    batch, original_id = state["batch"], state["message_ids"][0]
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    original_save = bot.save_settlements
    cloud_states, write_attempts = {}, []

    async def cloud(payload):
        action = payload["action"]
        if action == "list":
            return FakeResp(200, {"rows": stable_rows})
        if action == "ledger":
            cloud_states[payload["batch"]] = copy_state(payload["state"])
            return FakeResp(200, {"status": "ok"})
        if action == "settle":
            write_attempts.append(copy_state(payload))
            if len(write_attempts) == 1:
                raise requests.exceptions.ReadTimeout("accepted append response lost")
            return FakeResp(200, {"status": "settled"})
        raise AssertionError(f"Unexpected cloud request {action}")

    bot.sheet_call = cloud
    bot.save_settlements = lambda: False
    try:
        await send(AMY, "", [SHOT], reply_to=original_id)
        draft = append_draft()
        await react("✅", AMY, draft)
        pending = bot.PENDING[(CHAN.id, AMY.id)]
        check("追加雲端核准成功而本機快取失敗仍保留核准紀錄並開始原寫入",
              ("append_attempt" in state, "append_attempt" in cloud_states[batch],
               pending.get("write_started"), len(write_attempts), nos(state)), (True, True, True, 1, [1]))
        await react("❌", AMY, draft)
        check("本機快取失敗不會讓已核准追加被取消", ("append_attempt" in state, len(bot.PENDING)), (True, 1))
        timers = [session["timer"] for session in bot.PENDING.values() if session.get("timer")]
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        bot.PENDING.clear()
        bot.SETTLEMENTS.clear()
        bot.SETTLEMENTS.update(copy_state(cloud_states))
        await bot.recover_appends()
        current = bot.SETTLEMENTS[batch]
        check("本機快取持續失敗時仍可從雲端核准紀錄接續相同追加",
              (len(write_attempts), write_attempts[0] == write_attempts[1], nos(current), "append_attempt" in current),
              (2, True, [1, 2, 3], False))
        check("追加接續完成後雲端清除核准紀錄且只公布一次更新",
              ("append_attempt" in cloud_states[batch], bot.PENDING,
               len([message for message in SENT if message.content.startswith(f"🧾 **結算 {batch} 已追加")])), (False, {}, 1))
        await bot.recover_appends()
        check("本機快取失敗後再次 ready 不會重做已完成的追加", len(write_attempts), 2)
    finally:
        bot.save_settlements = original_save
        await stop_test_ledger_sync()


asyncio.run(approved_append_recovers_when_local_cache_write_fails())

print("\n新結算核准紀錄：回寫逾時與重啟接續")


async def accepted_journal_response_timeout(mode, readback_available):
    stable_rows = [{**SHEET[0], "id": "stable-fan-id"}]
    stub(sheet=stable_rows)
    base = await settle_and_confirm(AMY, [EARBUDS]) if mode == "append" else None
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    journal_key = "append_attempt" if base else "commit_attempt"
    cloud_states, row_writes, journals = {}, [], []

    async def cloud(payload):
        action = payload["action"]
        if action == "list":
            return FakeResp(200, {"rows": stable_rows})
        if action == "validate_settle":
            return FakeResp(200, {"status": "ok"})
        if action == "states":
            if not readback_available:
                raise requests.exceptions.ConnectionError("readback temporarily unavailable")
            return FakeResp(200, {"states": copy_state(cloud_states)})
        if action == "ledger":
            cloud_states[payload["batch"]] = copy_state(payload["state"])
            if journal_key in payload["state"]:
                journals.append(copy_state(payload["state"][journal_key]))
                raise requests.exceptions.ReadTimeout("accepted journal response lost")
            return FakeResp(200, {"status": "ok"})
        if action == "settle":
            row_writes.append(copy_state(payload))
            return FakeResp(200, {"status": "settled"})
        raise AssertionError(f"Unexpected cloud request {action}")

    bot.sheet_call = cloud
    uncertain = not readback_available
    try:
        await send(AMY, "" if base else "結算", [SHOT], reply_to=base["message_ids"][0] if base else None)
        pending = bot.PENDING[(CHAN.id, AMY.id)]
        draft, batch = pending["drafts"][-1], pending["state"]["batch"]
        await react("✅", AMY, draft)
        if uncertain:
            check(f"{mode} 核准已接受但讀回失敗時凍結草稿並保留兩端核准紀錄",
                  (journal_key in bot.SETTLEMENTS[batch], journal_key in cloud_states[batch],
                   pending.get("write_started"), len(row_writes)), (True, True, True, 0))
            await react("❌", AMY, draft)
            shot_count = len(pending["shots"])
            await send(AMY, "", [EARBUDS])
            check(f"{mode} 未確認的雲端核准結果不能取消或修改原截圖",
                  (len(bot.PENDING), len(pending["shots"]), pending["drafts"][-1].id), (1, shot_count, draft.id))
            readback_available = True
            await react("✅", AMY, draft)
            check(f"{mode} 核准逾時重試直接接續已接受的原核准內容",
                  (len(journals), row_writes[0]["rows"]), (1, journals[0]["writes"]))
        current = bot.SETTLEMENTS[batch]
        check(f"{mode} 核准逾時後{'重試' if uncertain else '讀回'}完成且只回寫一次商品列",
              (journal_key in current, journal_key in cloud_states[batch], len(row_writes),
               bot.PENDING, nos(current)),
              (False, False, 1, {}, [1, 2, 3] if base else [1, 2]))
    finally:
        await stop_test_ledger_sync()


for mode in ("normal", "append"):
    for readback_available in (True, False):
        asyncio.run(accepted_journal_response_timeout(mode, readback_available))


async def approved_settlement_requires_durable_journal_before_row_write():
    stable_rows = [{**SHEET[0], "id": "stable-fan-id"}]
    stub(sheet=stable_rows)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    actions = []
    fail = True

    async def cloud(payload):
        action = payload["action"]
        actions.append(action)
        if action == "list":
            return FakeResp(200, {"rows": stable_rows})
        if action == "states":
            return FakeResp(200, {"states": {}})
        if action == "ledger" and fail:
            raise requests.exceptions.ConnectionError("journal unavailable")
        return FakeResp(200, {"status": "ok"})

    bot.sheet_call = cloud
    try:
        await send(AMY, "結算", [SHOT])
        draft = drafts()[-1]
        pending = bot.PENDING[(CHAN.id, AMY.id)]
        before = copy_state(pending["state"])
        actions.clear()
        await react("✅", AMY, draft)
        check("新結算核准紀錄失敗時尚未回寫商品列", actions, ["validate_settle", "ledger", "states"])
        after = copy_state(pending["state"])
        after.pop("commit_attempt")
        check("核准紀錄失敗時保留原金額與草稿並凍結同一筆核准操作",
              (after, len(bot.PENDING), pending.get("write_started"), len(published())), (before, 1, True, 0))
        await react("❌", AMY, draft)
        check("核准紀錄寫入結果未明時無法取消遺失操作", len(bot.PENDING), 1)
        fail = False
        actions.clear()
        await react("✅", AMY, draft)
        check("新結算重試依序確認核准紀錄、回寫及保存完成狀態",
              actions, ["states", "ledger", "settle", "ledger"])
        check("核准紀錄恢復後完成同一張草稿", (bot.PENDING, len(published())), ({}, 1))
    finally:
        await stop_test_ledger_sync()


asyncio.run(approved_settlement_requires_durable_journal_before_row_write())


async def approved_settlement_recovers_after_row_write_timeout(restore_from):
    stable_rows = [{**SHEET[0], "id": "stable-fan-id"}]
    stub(sheet=stable_rows)
    with tempfile.TemporaryDirectory() as state_dir:
        bot.STATE_DIR = state_dir
        bot.LEDGER_SYNC, bot._sheet_ready = True, False
        bot._ledger_synced.clear()
        cloud_states, write_attempts, applied_rows = {}, [], set()

        async def cloud(payload):
            action = payload["action"]
            if action == "list":
                return FakeResp(200, {"rows": stable_rows})
            if action == "validate_settle":
                return FakeResp(200, {"status": "ok"})
            if action == "ledger":
                cloud_states[payload["batch"]] = copy_state(payload["state"])
                return FakeResp(200, {"status": "ok"})
            if action == "settle":
                write_attempts.append(copy_state(payload))
                applied_rows.update(row["id"] for row in payload["rows"])
                if len(write_attempts) == 1:
                    raise requests.exceptions.ReadTimeout("response lost after accepted row write")
                return FakeResp(200, {"status": "settled"})
            raise AssertionError(f"Unexpected cloud request {action}")

        bot.sheet_call = cloud
        try:
            await send(AMY, "結算", [SHOT])
            draft = drafts()[-1]
            batch = bot.PENDING[(CHAN.id, AMY.id)]["state"]["batch"]
            await react("✅", AMY, draft)
            saved = json.loads((Path(state_dir) / "settlements.json").read_text())
            check(f"{restore_from} 恢復前的新結算核准紀錄已存本機與雲端",
                  ("commit_attempt" in saved[batch], "commit_attempt" in cloud_states[batch]), (True, True))
            check(f"{restore_from} 新結算回寫逾時保留同草稿且尚未公布", (len(bot.PENDING), len(published())), (1, 0))
            timers = [session["timer"] for session in bot.PENDING.values() if session.get("timer")]
            for timer in timers:
                timer.cancel()
            await asyncio.gather(*timers, return_exceptions=True)
            bot.PENDING.clear()
            bot.SETTLEMENTS.clear()
            if restore_from == "local":
                bot.load_settlements()
            else:
                bot.SETTLEMENTS.update(copy_state(cloud_states))
            await bot.recover_commits()
            current = bot.SETTLEMENTS[batch]
            check(f"{restore_from} 重啟接續新結算使用相同批次與固定列 ID", (len(write_attempts), write_attempts[0] == write_attempts[1]), (2, True))
            check(f"{restore_from} 重啟後同商品列只保留一筆結算", (applied_rows, list(bot.SETTLEMENTS), nos(current)),
                  ({"stable-fan-id"}, [batch], [1, 2]))
            check(f"{restore_from} 恢復成功後清除核准紀錄並只公布一次",
                  ("commit_attempt" in current, "commit_attempt" in cloud_states[batch], bot.PENDING, len(published())),
                  (False, False, {}, 1))
            check(f"{restore_from} 接續新結算的每人金額正確", bot.settle.totals(current)["twd"], {"bob": 495, "unclassified": 495})
            await bot.recover_commits()
            check(f"{restore_from} 再次 ready 不會重播已完成新結算", (len(write_attempts), len(published())), (2, 1))
        finally:
            bot.STATE_DIR = None
            await stop_test_ledger_sync()


for restore_from in ("local", "cloud"):
    asyncio.run(approved_settlement_recovers_after_row_write_timeout(restore_from))


async def approved_settlement_retains_journal_until_final_ledger_succeeds():
    stable_rows = [{**SHEET[0], "id": "stable-fan-id"}]
    stub(sheet=stable_rows)
    bot.LEDGER_SYNC, bot._sheet_ready = True, False
    bot._ledger_synced.clear()
    cloud_states, ledger_attempts, row_attempts = {}, [], []

    async def cloud(payload):
        action = payload["action"]
        if action == "list":
            return FakeResp(200, {"rows": stable_rows})
        if action == "states":
            return FakeResp(200, {"states": copy_state(cloud_states)})
        if action == "validate_settle":
            return FakeResp(200, {"status": "ok"})
        if action == "ledger":
            ledger_attempts.append(copy_state(payload))
            if len(ledger_attempts) == 2:
                return FakeResp(503, {"status": "error"})
            cloud_states[payload["batch"]] = copy_state(payload["state"])
            return FakeResp(200, {"status": "ok"})
        if action == "settle":
            row_attempts.append(copy_state(payload))
            return FakeResp(200, {"status": "settled"})
        raise AssertionError(f"Unexpected cloud request {action}")

    bot.sheet_call = cloud
    try:
        await send(AMY, "結算", [SHOT])
        draft = drafts()[-1]
        batch = bot.PENDING[(CHAN.id, AMY.id)]["state"]["batch"]
        await react("✅", AMY, draft)
        current = bot.SETTLEMENTS[batch]
        check("列回寫成功但最終帳本失敗時保留核准紀錄與草稿",
              (len(row_attempts), "commit_attempt" in current, "commit_attempt" in cloud_states[batch], len(bot.PENDING), len(published())),
              (1, True, True, 1, 0))
        await react("❌", AMY, draft)
        check("已回寫的新結算無法用取消丟棄核准紀錄", ("commit_attempt" in current, len(bot.PENDING)), (True, 1))
        await react("✅", AMY, draft)
        check("最終帳本恢復後可以重試完成新結算",
              ("commit_attempt" in current, "commit_attempt" in cloud_states[batch], bot.PENDING, len(published())),
              (False, False, {}, 1))
        check("新結算的最終帳本重試維持同一列回寫內容", row_attempts[0], row_attempts[1])
    finally:
        await stop_test_ledger_sync()


asyncio.run(approved_settlement_retains_journal_until_final_ledger_succeeds())
bot.sheet_call, bot.parse_with_llm = real_post, real_parse

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
