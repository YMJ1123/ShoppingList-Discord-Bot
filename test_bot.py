#!/usr/bin/env python3
"""bot.py 裡純函式的測試（不連 Discord、不連 n8n、不呼叫 LLM）。

    python3 test_bot.py
"""
import sys

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
MSG_DELETE_NL = f"刪除 {TB_PASTED}"
MSG_DELETE_OLD = f"~刪除 {TB_PASTED}"

print("\nextract_first_url：貼上的淘寶長網址（2026-09-10 迴歸）")

added = bot.extract_first_url(MSG_ADD)
check("markdown 包裝 → 取括號裡真正能點的網址", added, TB_REAL)
check("抽到的網址不含 markdown 分隔", "](" in added, False)
check("新增與自然語言刪除抽到同一個網址", bot.extract_first_url(MSG_DELETE_NL), added)
check("新增與 ~刪除 抽到同一個網址", bot.extract_first_url(MSG_DELETE_OLD), added)
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

print("\n其他純函式")

for label, got, expect in [
    ("safe_quantity(3)", bot.safe_quantity(3), 3),
    ("safe_quantity('2')", bot.safe_quantity("2"), 2),
    ("safe_quantity('兩')", bot.safe_quantity("兩"), 1),
    ("safe_quantity(None)", bot.safe_quantity(None), 1),
    ("safe_quantity(-5)", bot.safe_quantity(-5), 1),
    ("safe_price(120)", bot.safe_price(120), 120.0),
    ("safe_price('1,200')", bot.safe_price("1,200"), 1200.0),
    ("safe_price('abc')", bot.safe_price("abc"), 0.0),
    ("safe_price(-5)", bot.safe_price(-5), 0.0),
]:
    check(label, got, expect)

_long = "\n".join(f"• 第 {i} 行商品" for i in range(400))
_chunks = bot.split_message(_long)
check("split_message 有切開", len(_chunks) > 1, True)
check(
    "split_message 每段都在上限內",
    all(len(c) <= bot.DISCORD_CHUNK_LIMIT for c in _chunks),
    True,
)
check("split_message 內容無損", "\n".join(_chunks), _long)
check("split_message 硬切超長單行", "".join(bot.split_message("x" * 5000)), "x" * 5000)

for label, text, should_match in [
    ("結單", "結單", True),
    ("刪除電動牙刷", "刪除電動牙刷", True),
    ("小米風扇不用買了", "小米風扇不用買了", True),
    ("價格 牙刷 120", "價格 牙刷 120", True),
    ("清單", "清單", True),
    ("大家要付多少", "大家要付多少", True),
    ("分帳", "分帳", True),
    ("閒聊不該命中", "今天天氣真好", False),
]:
    check(f"意圖關鍵字：{label}", bool(bot.INTENT_RE.search(text)), should_match)

print(f"\n全部 {PASSED} 項測試通過 ✓\n")
