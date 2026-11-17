#!/usr/bin/env python3
"""settle.py 的測試（不連 Discord、n8n、LLM）。

    python3 test_settle.py

商品資料取自 2026-11-12 的真實截圖和表格，視覺模型的輸出照實際讀到的樣子寫。
"""
import copy
import sys

import settle
from settle import UNCLASSIFIED

PASSED = 0


def check(name, got, expect):
    global PASSED
    if got != expect:
        print(f"  ✗ {name}\n      實際: {got!r}\n      預期: {expect!r}")
        sys.exit(1)
    PASSED += 1
    print(f"  ✓ {name}")


def shot(items, twd=0, order_no=""):
    return settle.clean_shot({
        "items": [{"name": n, "spec": s, "qty": q, "paid_price": p} for n, s, q, p in items],
        "total_twd": twd, "order_no": order_no})


GLASSES = ("轻奢钛架男士高清防蓝光眼镜", "颜色:Titanium-冰川灰C2【轻奢高级】;", 1, 284.66)
FILM = ("【除尘秒贴】闪魔iPad耐刮钢化膜", "适配型号:iPad Pro(11英寸)2018/2020/2021版;", 1, 61.58)
CABINET = ("盲盒收纳展示架手办亚克力柜", "颜色分类:集装箱手办盒 特大号透明【单个装】;", 2, 24.6)
CHIIKAWA = ("chiikawa乌萨奇公仔收纳盒棉花娃", "颜色分类:1个透明 小洋房【可放三层】36-13.5-27cm;", 4, 26.27)
SHOES = ("TANZ 002 夜侠361男鞋运动鞋20", "颜色分类:曜石黑[【琴瑟】];鞋码:42.5;", 1, 352.33)   # 模型把「倓」讀成「侠」

# 確認訂單頁：三張截圖共用 TWD 4540，第 2、3 張重疊拍到 chiikawa
CONFIRM = [shot([GLASSES, FILM], 4540), shot([CABINET, CHIIKAWA], 4540), shot([CHIIKAWA, SHOES], 4540)]

ROWS = [
    {"tab": "淘寶", "row": 2, "name": "闪魔适用三星S24ultra钢化膜S25Ultra钢化膜", "spec": "全屏超清 2片装", "qty": 1, "buyer": "mattmatt1223"},
    {"tab": "淘寶", "row": 75, "name": "TANZ 002 夜倓361男鞋运动鞋2026秋季篮球文化鞋跑步潮流休闲鞋", "spec": "曜石黑, 鞋碼 42.5, CZ007", "qty": 1, "buyer": "ymj1123"},
    {"tab": "淘寶", "row": 77, "name": "集裝箱手辦盒", "spec": "特大號透明【單個裝】", "qty": 1, "buyer": "chezz1025"},
    {"tab": "淘寶", "row": 78, "name": "chiikawa乌萨奇公仔收纳盒棉花娃娃玩偶吉伊卡哇展示柜透明展示架", "spec": "透明, 36-13.5-27cm", "qty": 4, "buyer": "chezz1025"},
    {"tab": "淘寶", "row": 79, "name": "TANZ 002 夜倓361男鞋运动鞋2026秋季篮球文化鞋跑步潮流休闲鞋", "spec": "曜石黑, 鞋碼 42.5", "qty": 1, "buyer": "ymj1123"},
    {"tab": "京東", "row": 9, "name": "绿联55W自带线10000毫安充电宝", "spec": "", "qty": 1, "buyer": "mattmatt1223"},
]
# LLM 的比對結果：#1 沒對到；#2 對三星膜那列沒把握（low）
LLM = [
    {"no": 1, "rows": [], "confidence": "high"},
    {"no": 2, "rows": ["淘寶2"], "confidence": "low"},
    {"no": 3, "rows": ["淘寶77"], "confidence": "high"},
    {"no": 4, "rows": ["淘寶78"], "confidence": "high"},
    {"no": 5, "rows": ["淘寶75", "淘寶79"], "confidence": "high"},
]


def build(shots=CONFIRM, rows=ROWS, llm=LLM, payer="ymj1123", market=None):
    units, errors, notes = settle.merge_shots(copy.deepcopy(shots))
    payload = settle.match_payload(units, rows)
    writes, more = settle.assign(units, rows, payload, llm)
    return {"batch": "20261112-1510", "payer": payer, "units": units, "notes": notes + more, "market_rate": market}, writes, payload, errors


# ------------------------------------------------------------------
print("\nclean_shot：不信任模型的輸出")
c = settle.clean_shot({"items": [
    {"name": "好的", "spec": None, "qty": "2", "paid_price": "12.5"},
    {"name": "", "qty": 1, "paid_price": 5},
    {"name": "沒價格", "qty": 1, "paid_price": 0},
    {"name": "數量亂寫", "qty": "x", "paid_price": 5},
    "不是物件",
], "total_twd": "164", "order_no": "5127 4811 9047 0077 704"})
check("只留下完整的商品，並轉成正確型別", c["items"], [{"name": "好的", "spec": "", "qty": 2, "price": 12.5}])
check("讀不完整的商品有記下件數", c["dropped"], 4)
check("台幣轉成整數", c["total_twd"], 164)
check("訂單號只留數字", c["order_no"], "5127481190470077704")
check("台幣 0 視為沒讀到", settle.clean_shot({"items": [], "total_twd": 0})["total_twd"], None)
check("太短的數字不當訂單號", settle.clean_shot({"order_no": "12345"})["order_no"], "")

loose = settle.clean_shot({"items": [{"name": "适用applepencil笔尖", "qty": "x1", "paid_price": "¥9.8"},
                                    {"name": "粘毛器", "qty": "2件", "paid_price": "6.38元"}],
                          "total_twd": "TWD 1,046", "order_no": "3317017045092056761"})
check("模型把數字寫成 x1、¥9.8、6.38元、TWD 1,046 也讀得懂",
      (loose["items"], loose["total_twd"], loose["dropped"]),
      ([{"name": "适用applepencil笔尖", "spec": "", "qty": 1, "price": 9.8}, {"name": "粘毛器", "spec": "", "qty": 2, "price": 6.38}], 1046, 0))
check("模型回傳的不是物件也不會出錯", settle.clean_shot(None), {"items": [], "total_twd": None, "order_no": "", "dropped": 0})

print("\nmerge_shots：讀不到的截圖不能默默不見（2026-11-16 applepencil 筆尖被略過）")
empty = settle.clean_shot({"items": [], "total_twd": 46, "order_no": "3317017045092056761"})
partial = settle.clean_shot({"items": [{"name": "甲", "qty": 1, "paid_price": 10}, {"name": "乙", "qty": 1, "paid_price": "?"}], "total_twd": 60})
units, errors, notes = settle.merge_shots([shot([("Wilson篮球", "", 1, 87.22)], 415, "3317017045092038397"), empty, partial])
check("整張沒讀到 → 提醒是第幾張", notes[0], "第 2 張截圖沒讀到任何商品，可能讀錯了，請檢查那張或重傳（那張本來就沒有商品的話可以忽略）")
check("部分商品讀不完整 → 提醒件數", notes[1] if len(notes) > 1 else "（沒有提醒）", "第 3 張截圖有 1 件商品讀不完整，被略過了，請檢查那張或重傳")
check("其他讀到的照常結算", [it["name"] for u in units for it in u["items"]], ["Wilson篮球", "甲"])
_, _, notes = settle.merge_shots([shot([("甲", "", 1, 10)], 50), settle.clean_shot({"items": []})])
check("連台幣都沒讀到的也會提醒", notes, ["第 2 張截圖沒讀到任何商品，可能讀錯了，請檢查那張或重傳（那張本來就沒有商品的話可以忽略）"])

print("\nmerge_shots：確認訂單頁（多張截圖共用一個總額）")
state, writes, payload, errors = build()
unit = state["units"][0]
check("沒有錯誤", errors, [])
check("合併成一筆付款", len(state["units"]), 1)
check("重疊拍到的 chiikawa 只算一次", [it["name"][:8] for it in unit["items"]],
      ["轻奢钛架男士高清", "【除尘秒贴】闪魔", "盲盒收纳展示架手", "chiikawa", "TANZ 002"])
check("人民幣合計 = 單價 × 數量加總", unit["cny"], 852.85)
check("回算匯率 = 台幣 / 人民幣", round(unit["rate"], 4), 5.3233)
check("商品編號連續", [it["no"] for it in unit["items"]], [1, 2, 3, 4, 5])

print("\nmerge_shots：訂單詳情頁（每張訂單自己的金額）")
DETAIL = [
    shot([("2026新款M5适用苹果macbook...", "2020年款Pro13寸 (M1) (A2338/A...", 1, 33.17)], 164, "5127481190470077704"),
    shot([("AKAZ定制带ai按键适用苹果17/...", "iPhone 17 Pro;【黑色】", 1, 38.7),
          ("AKAZ磁吸双C数据线PD快充240...", "【全新升级工艺+包装】AKAZ 240...", 1, 29)], 336, "5127481190470104532"),
]
units, errors, notes = settle.merge_shots(copy.deepcopy(DETAIL))
check("兩張訂單 = 兩筆付款", [(u["order_no"][-4:], u["total_twd"], u["cny"]) for u in units], [("7704", 164, 33.17), ("4532", 336, 67.7)])
check("各自回算匯率", [round(u["rate"], 3) for u in units], [4.944, 4.963])
check("編號跨訂單連續", [it["no"] for u in units for it in u["items"]], [1, 2, 3])

long_order = [shot([("甲", "", 1, 10)], 0, "5127481190470000001"), shot([("甲", "", 1, 10), ("乙", "", 1, 20)], 150, "5127481190470000001")]
units, errors, _ = settle.merge_shots(long_order)
check("同一張訂單分兩張截圖：合併、去重、取到金額", [(len(u["items"]), u["total_twd"]) for u in units], [(2, 150)])

units, _, _ = settle.merge_shots(copy.deepcopy(DETAIL), start=8)
check("編號可以從指定號碼開始（同頻道不撞號）", [it["no"] for u in units for it in u["items"]], [8, 9, 10])

print("\nmerge_shots：讀不到金額要擋下來")
_, errors, _ = settle.merge_shots([shot([("甲", "", 1, 10)], 0, "5127481190470000001")])
check("訂單詳情頁沒拍到实付款 → 錯誤", len(errors), 1)
_, errors, _ = settle.merge_shots([shot([("甲", "", 1, 10)])])
check("完全沒有金額 → 錯誤", len(errors), 1)
units, errors, _ = settle.merge_shots([shot([("甲", "", 1, 10)], 100), shot([("乙", "", 1, 10)])])
check("確認訂單頁其中一張沒拍到按鈕 → 併進同一筆", (len(units), len(units[0]["items"]), errors), (1, 2, []))
_, _, notes = settle.merge_shots([shot([("甲", "", 1, 10)], 4540), shot([("乙", "", 1, 10)], 4640)])
check("讀到兩個不同總額 → 提醒", len(notes), 1)

print("\ncandidates：粗篩")
ids = {p["no"]: [x["id"] for x in p["candidates"]] for p in payload}
check("繁體規格名對得到簡體截圖", "淘寶77" in ids[3], True)
check("被截斷的標題對得到完整標題", ids[4][0], "淘寶78")
check("模型讀錯一個字還是找得到", ids[5][:2], ["淘寶75", "淘寶79"])
check("不相干的京東充電寶不會進候選", any("京東9" in v for v in ids.values()), False)

print("\nassign：誰的商品")
owners = {it["no"]: [(s["owner"], s["cny"]) for s in it["shares"]] for it in unit["items"]}
check("沒對到 → unclassified", (owners[1], unit["items"][0]["hint"]), ([(UNCLASSIFIED, 284.66)], ""))
check("低信心 → unclassified，但留下提示", (owners[2], unit["items"][1]["hint"]), ([(UNCLASSIFIED, 61.58)], "可能是 mattmatt1223"))
check("低信心的列不會被寫回表格", any(w["row"] == 2 for w in writes), False)
check("提示會出現在訊息裡", "#2 【除尘秒贴】闪魔iPad耐刮钢化膜 ×1　¥61.58 ≈ TWD 328（可能是 mattmatt1223）" in settle.render(state, final=False), True)
check("數量相符 → 算給登記的人", owners[4], [("chezz1025", 105.08)])
check("數量不符但只有一個人登記 → 全算給他", owners[3], [("chezz1025", 49.2)])
check("同一人重複登記 → 只算一次", owners[5], [("ymj1123", 352.33)])
check("數量不符會列入提醒", [n[:2] for n in state["notes"]], ["#3", "#5"])
check("寫回表格：價格只寫在第一列，重複登記的那列只標批次",
      [(w["tab"], w["row"], w["price"]) for w in writes],
      [("淘寶", 77, 49.2), ("淘寶", 78, 105.08), ("淘寶", 75, 352.33), ("淘寶", 79, None)])

two = [{"tab": "淘寶", "row": 25, "name": "AKAZ磁吸双C数据线PD快充240W吸附收纳编织", "spec": "1米", "qty": 1, "buyer": "mattmatt1223"},
       {"tab": "淘寶", "row": 68, "name": "AKAZ磁吸双C数据线PD快充240W吸附收纳编织", "spec": "2m CZ321", "qty": 1, "buyer": "ymj1123"}]
cable = [shot([("AKAZ磁吸双C数据线PD快充240...", "【全新升级工艺+包装】AKAZ 240...", 1, 29)], 144, "5127481190470104532")]
s2, w2, _, _ = build(cable, two, [{"no": 1, "rows": ["淘寶25", "淘寶68"], "confidence": "high"}])
it = s2["units"][0]["items"][0]
check("兩個人登記、訂單只買一個 → 不亂猜，放 unclassified 並提示", (it["shares"][0]["owner"], it["hint"], w2), (UNCLASSIFIED, "可能是 mattmatt1223 或 ymj1123", []))

split = [shot([("AKAZ磁吸双C数据线PD快充240...", "", 2, 29)], 288, "5127481190470104532")]
s3, w3, _, _ = build(split, two, [{"no": 1, "rows": ["淘寶25", "淘寶68"], "confidence": "high"}])
check("兩個人各登記一個、訂單買兩個 → 各付一半", [(s["owner"], s["cny"]) for s in s3["units"][0]["items"][0]["shares"]],
      [("mattmatt1223", 29.0), ("ymj1123", 29.0)])

s4, w4, _, _ = build(llm=[{"no": 4, "rows": ["淘寶999", "京東9"], "confidence": "high"}])
check("LLM 回了不在候選名單裡的列 → 不採用", (s4["units"][0]["items"][3]["shares"][0]["owner"], w4), (UNCLASSIFIED, []))
dup = [shot([("chiikawa乌萨奇公仔收纳盒棉花娃", "透明", 4, 26.27), ("chiikawa乌萨奇公仔收纳盒棉花娃", "粉色", 4, 30)], 1000)]
s5, w5, _, _ = build(dup, llm=[{"no": 1, "rows": ["淘寶78"], "confidence": "high"}, {"no": 2, "rows": ["淘寶78"], "confidence": "high"}])
check("同一列不能對到兩個商品", [it["shares"][0]["owner"] for it in s5["units"][0]["items"]], ["chezz1025", UNCLASSIFIED])
s6, _, _, _ = build(llm=None)
check("LLM 完全沒回結果 → 全部 unclassified", {s["owner"] for it in s6["units"][0]["items"] for s in it["shares"]}, {UNCLASSIFIED})

print("\ntotals：每人應付")
t = settle.totals(state)
check("各人台幣", t["twd"], {UNCLASSIFIED: 1843, "chezz1025": 821, "ymj1123": 1876})
check("加總剛好等於實付金額", sum(t["twd"].values()), 4540)
check("各人人民幣", t["cny"], {UNCLASSIFIED: 346.24, "chezz1025": 154.28, "ymj1123": 352.33})
thirds = {"batch": "x", "payer": "p", "units": [{"order_no": "", "total_twd": 100, "cny": 30.0, "rate": 100 / 30, "items": [
    {"no": i, "name": "n", "qty": 1, "line": 10.0, "shares": [{"owner": o, "cny": 10.0}]} for i, o in enumerate("abc", 1)]}]}
check("除不盡時不會多收或少收", sorted(settle.totals(thirds)["twd"].values()), [33, 33, 34])
multi, _, _, _ = build(DETAIL, [], [])
check("多筆訂單：各用自己的匯率，總和等於各筆實付相加", (settle.totals(multi)["total_twd"], sum(settle.totals(multi)["twd"].values())), (500, 500))

print("\nclaim：認領")
check("認領 unclassified 的商品", settle.claim(state, 2, "wakaru_0216"), "")
t = settle.totals(state)
check("認領後金額移到那個人身上", (t["twd"]["wakaru_0216"], t["twd"][UNCLASSIFIED], sum(t["twd"].values())), (328, 1515, 4540))
check("已經有主的不能再認領", settle.claim(state, 2, "erictw13")[:9], "#2 已經算在 w")
check("對到表格的商品不能被認領", settle.claim(state, 4, "erictw13")[:5], "#4 已經")
check("沒有這個編號", settle.claim(state, 99, "erictw13"), "這次結算沒有 #99 這個商品")

print("\nclose：結束認領")
closing = copy.deepcopy(state)   # 此時 #2 已被 wakaru_0216 認領，#1 還沒人領
check("結束前還有沒人領的", settle.has_unclassified(closing), True)
check("回傳被算給下單人的編號", settle.close(closing), [1])
t = settle.totals(closing)
check("沒人領的算給下單人，總額不變", (t["twd"].get(UNCLASSIFIED), t["twd"]["ymj1123"], sum(t["twd"].values())), (None, 1876 + 1515, 4540))
check("其他人的金額不變", (t["twd"]["chezz1025"], t["twd"]["wakaru_0216"]), (821, 328))
check("結束後沒有沒人領的", settle.has_unclassified(closing), False)
check("結束後不能再認領", settle.claim(closing, 1, "erictw13"), "結算 20261112-1510 已經結束認領了，有問題請直接找下單的人")
check("最終訊息", settle.render_closed(closing, {"ymj1123": "<@1>", "chezz1025": "<@2>"}),
      "🏁 **結算 20261112-1510 結束認領**｜請轉帳給 <@1>\n"
      "\n"
      "**<@2>　TWD 821**\n"
      "　#3 盲盒收纳展示架手办亚克力柜 ×2　¥49.20\n"
      "　#4 chiikawa乌萨奇公仔收纳盒棉花娃 ×4　¥105.08\n"
      "**wakaru_0216　TWD 328**\n"
      "　#2 【除尘秒贴】闪魔iPad耐刮钢化膜 ×1　¥61.58\n"
      "\n"
      "下單人自己的 TWD 3,391 不用轉，含沒人認領的 #1\n"
      "　#1 轻奢钛架男士高清防蓝光眼镜 ×1　¥284.66\n"
      "　#5 TANZ 002 夜侠361男鞋运动鞋20 ×1　¥352.33")
nothing_left = copy.deepcopy(closing); nothing_left["leftover"] = []
closed_text = settle.render_closed(nothing_left)
check("沒有剩下的就不寫「含沒人認領」", ("含沒人認領" in closed_text, "下單人自己的 TWD 3,391 不用轉" in closed_text), (False, True))
solo = {"batch": "B", "payer": "p", "units": [{"order_no": "", "total_twd": 50, "cny": 10.0, "rate": 5.0, "items": [
    {"no": 1, "name": "n", "qty": 1, "line": 10.0, "shares": [{"owner": UNCLASSIFIED, "cny": 10.0}]}]}]}
settle.close(solo)
check("全部都是下單人自己的", settle.render_closed(solo),
      "🏁 **結算 B 結束認領**｜請轉帳給 p\n\n沒有人需要轉帳。\n\n下單人自己的 TWD 50 不用轉，含沒人認領的 #1\n　#1 n ×1　¥10.00")

print("\nduplicate_notes：重複結算")
# 2026-11-16 的真實情況：11/12 用確認頁結算過，11/16 又用訂單頁結算同一批（標題寫法不同、OCR 也有錯字）
NOV12 = {"batch": "20261112-145453", "units": [{"order_no": "", "items": [
    {"no": 1, "name": "盲盒收纳展示架手办亚克力柜", "qty": 2, "price": 24.6},
    {"no": 3, "name": "轻奢钛架男士高清防蓝光眼镜", "qty": 1, "price": 284.66},
    {"no": 5, "name": "TANZ 002 夜侦361男鞋运动鞋20", "qty": 1, "price": 352.33}]}]}
nov16, _, _ = settle.merge_shots([
    shot([("盲盒收纳展示架手办子透明防...", "", 2, 24.6)], 272),
    shot([("正品澳洲摩洛哥护发精油经典黑...", "", 1, 110.29)], 565, "5127927841059004149"),
    shot([("TANZ 002 夜梭361男鞋运动...", "", 1, 352.33)], 1773),
    shot([("轻奢手工钛架眼镜男款近视度...", "", 1, 284.66)], 1426),
])
dups = settle.duplicate_notes(nov16, [NOV12])
check("真實案例：三件重複都抓到，護髮精油沒有", [d.split(" ")[0] for d in dups], ["#1", "#3", "#4"])
check("提醒寫出是哪一次結算的哪一號", dups[0], "#1 盲盒收纳展示架手办子透明防... ×2 ¥24.60 和結算 20261112-145453 的 #1 看起來是同一件，可能重複結算")
check("同價但不同商品 → 不提醒", settle.duplicate_notes(settle.merge_shots([shot([("净得丽粘尘纸粘毛器10cm", "", 2, 24.6)], 100)])[0], [NOV12]), [])
check("同名但數量不同 → 不提醒", settle.duplicate_notes(settle.merge_shots([shot([("盲盒收纳展示架手办亚克力柜", "", 1, 24.6)], 100)])[0], [NOV12]), [])
check("沒有之前的結算 → 不提醒", settle.duplicate_notes(nov16, []), [])
old_order = {"batch": "B0", "units": [{"order_no": "5127927841059004149", "items": [{"no": 1, "name": "x", "qty": 1, "price": 1.0}]}]}
check("同一個訂單號 → 整筆提醒一次", settle.duplicate_notes(nov16, [old_order]), ["訂單 …004149 已經在結算 B0 算過了，可能重複結算"])
same_draft, _, _ = settle.merge_shots([
    shot([("盲盒收纳展示架手办亚克力柜", "颜色分类:集装箱", 2, 24.6), ("TANZ 002 夜倓361男鞋运动鞋20", "", 1, 352.33)], 4540),
    shot([("盲盒收纳展示架手办子透明防...", "", 2, 24.6)], 272, "5127927841059000001"),
])
check("同一張草稿裡確認頁＋訂單頁拍到同一件 → 提醒算了兩次",
      settle.duplicate_notes(same_draft, []), ["#3 盲盒收纳展示架手办子透明防... ×2 ¥24.60 和這張草稿的 #1 看起來是同一件（同一筆訂單的不同頁面？），可能算了兩次"])

print("\nrate_notes：回算匯率檢查")
check("比市場匯率高 8% → 提醒可能漏截圖", [n[:28] for n in settle.rate_notes(state["units"], 4.93)], ["TWD 4,540 這筆付款的回算匯率 5.323 比今"])
check("和市場匯率差不多 → 不提醒", settle.rate_notes(multi["units"], 4.93), [])
check("沒有市場匯率、只有一兩筆 → 無從判斷", settle.rate_notes(state["units"]), [])
odd = copy.deepcopy(DETAIL) + [shot([("丙", "", 1, 79.52)], 394, "5127481190470068522"), shot([("丁", "", 1, 96.02)], 776, "5127481190470168905")]
check("沒有市場匯率時和其他訂單比：金額讀錯的那筆被抓出來", [n[:10] for n in settle.rate_notes(settle.merge_shots(odd)[0])], ["訂單 …168905"])

print("\nrender：訊息內容")
draft = settle.render(state, final=False)
check("草稿不會 @ 人", "<@" in draft, False)
check("草稿有確認提示", "按 ✅ 確認" in draft, True)
final = settle.render(state, final=True, mentions={"ymj1123": "<@1>", "chezz1025": "<@2>"})
check("正式版 @ 下單人", "請轉帳給 <@1>" in final, True)
check("正式版 @ 應付的人", "**<@2>　TWD 821**" in final, True)
check("下單人自己的份標明不用轉", "**<@1>　TWD 1,876**（¥352.33）（下單人自己的，不用轉）" in final, True)
check("unclassified 有認領說明", "回覆 `認領 編號`" in final, True)
check("unclassified 的商品附上大約台幣", "#1 轻奢钛架男士高清防蓝光眼镜 ×1　¥284.66 ≈ TWD 1,515\n" in final, True)
proxy = dict(state, poster="amy")
check("代傳：草稿標出代傳的人，兩個人都能確認",
      settle.render(proxy, final=False).split("\n")[0] + " / " + settle.render(proxy, final=False).split("\n")[-2],
      "🧾 **結算草稿**｜下單人 ymj1123（amy 代傳） / ymj1123 或 amy 按 ✅ 確認並寫入表格，按 ❌ 取消（10 分鐘內有效）")
check("代傳：正式版請大家轉給下單人，不是代傳的人",
      settle.render(proxy, final=True, mentions={"ymj1123": "<@1>"}).split("\n")[0], "🧾 **結算 20261112-1510**｜請轉帳給 <@1>（amy 代傳）")
check("代傳：下單人自己的份仍標不用轉", "**<@1>　TWD 1,876**（¥352.33）（下單人自己的，不用轉）" in settle.render(proxy, final=True, mentions={"ymj1123": "<@1>"}), True)
check("沒有代傳時不顯示", "代傳" in settle.render(state, final=False), False)
check("沒有代傳時寫「下單人按 ✅」", settle.render(state, final=False).split("\n")[-2], "下單人按 ✅ 確認並寫入表格，按 ❌ 取消（10 分鐘內有效）")
check("草稿最後提示可以追加截圖", settle.render(state, final=False).split("\n")[-1], "還有截圖的話直接再傳（不用打「結算」），會併進這張草稿，編號接著排")
check("正式結果沒有追加提示", "直接再傳" in settle.render(state, final=True), False)
nobody = copy.deepcopy(state)
for u in nobody["units"]:
    for it in u["items"]:
        it["shares"] = [{"owner": UNCLASSIFIED, "cny": it["line"]}]
check("全部沒對到人時不會出現連續空行", any("\n\n\n" in settle.render(nobody, final=f) for f in (True, False)), False)
check("正式版有 🏁 說明", "　→ ymj1123 按 🏁 結束認領，到時還沒人認領的算 ymj1123 自己的" in final, True)
check("代傳時兩個人都能按 🏁", "　→ ymj1123 或 amy 按 🏁 結束認領" in settle.render(proxy, final=True), True)
check("草稿沒有 🏁 說明", "🏁" in settle.render(state, final=False), False)
check("認領後的簡表", settle.render_totals(state), "chezz1025　TWD 821\nwakaru_0216　TWD 328\nymj1123　TWD 1,876\n❓ 還沒人認領　TWD 1,515（#1）")

print("\nsplit_message")
check("短訊息不切", settle.split_message("a\nb"), ["a\nb"])
check("超過上限照行切", settle.split_message("aaaa\nbbbb\ncccc", limit=9), ["aaaa\nbbbb", "cccc"])
check("單行超長也切得開", settle.split_message("x" * 25, limit=10), ["x" * 10, "x" * 10, "x" * 5])
check("每段都不超過上限", max(len(c) for c in settle.split_message(final, limit=120)) <= 120, True)

print("\nprepare_append：已公布結算追加漏掉的商品")
old_items = [(f"原商品{i}", "", 1, 10.0) for i in range(1, 15)]
append_base, _, _, _ = build([shot(old_items, 6404)], [], [], payer="payer")
append_base.update(poster="proxy", mentions={"payer": "<@100>", "carol": "<@300>"}, message_ids=[123])
settle.claim(append_base, 1, "bob")
append_snapshot = copy.deepcopy(append_base)
pencil_units, _, _ = settle.merge_shots([
    shot([("适用applepencil笔尖", "白色【2 枚装】", 1, 9.8)], 46, "3317017045092056761")
], start=15)
pencil_snapshot = copy.deepcopy(pencil_units)
appended, delta, errors = settle.prepare_append(append_base, pencil_units)
check("獨立筆尖訂單保留原批次，追加成 #15", (errors, appended["batch"], [it["no"] for u in appended["units"] for it in u["items"]]),
      ([], append_base["batch"], list(range(1, 16))))
check("實付只加筆尖那筆的 46 元", (settle.totals(appended)["total_twd"], sum(settle.totals(appended)["twd"].values())), (6450, 6450))
check("原結算和輸入資料都未被修改", (append_base, pencil_units), (append_snapshot, pencil_snapshot))
check("付款人、代傳人與舊訊息識別碼保留", (appended["payer"], appended["poster"], appended["message_ids"]), ("payer", "proxy", [123]))
check("原本手動認領的人保留", appended["units"][0]["items"][0]["shares"], [{"owner": "bob", "cny": 10.0}])
check("delta 只含 #15 並使用該筆付款匯率", ([it["no"] for u in delta for it in u["items"]], delta[0]["rate"]), ([15], 46 / 9.8))
pencil_rows = [{"tab": "淘寶", "row": 90, "name": "适用applepencil笔尖", "spec": "白色【2 枚装】", "qty": 1, "buyer": "carol"}]
pencil_payload = settle.match_payload(delta, pencil_rows)
pencil_writes, _ = settle.assign(delta, pencil_rows, pencil_payload, [{"no": 15, "rows": ["淘寶90"], "confidence": "high"}])
check("配對 delta 同步更新完整結算的新商品", appended["units"][-1]["items"][0]["shares"], [{"owner": "carol", "cny": 9.8}])
check("只產生新配對列的表格寫入", pencil_writes, [{"tab": "淘寶", "row": 90, "name": "适用applepencil笔尖", "buyer": "carol", "price": 9.8}])

same_base, _, _, _ = build([shot([("原登記商品", "白色", 1, 10), ("已認領商品", "", 1, 10)], 150, "5127481190470000001")], [], [])
settle.claim(same_base, 1, "bob")
settle.claim(same_base, 2, "carol")
same_base["mentions"] = {"bob": "<@200>", "carol": "<@300>"}
same_snapshot = copy.deepcopy(same_base)
same_incoming = settle.merge_shots([shot([("原登记商品", "白色", 1, 10), ("漏掉商品", "", 1, 10)], 150, "5127481190470000001")], start=3)[0]
same_after, same_delta, errors = settle.prepare_append(same_base, same_incoming)
check("同訂單截圖重疊商品去重，沿用新編號並保留間隔", (errors, [it["no"] for it in same_after["units"][0]["items"]]), ([], [1, 2, 4]))
check("同筆付款只重算匯率，實付金額維持 150", (len(same_after["units"]), same_after["units"][0]["cny"], same_after["units"][0]["rate"], settle.totals(same_after)["total_twd"]), (1, 30.0, 5.0, 150))
check("delta 視圖使用合併後付款匯率", same_delta[0]["rate"], 5.0)
check("舊的歸屬、表格配對與手動認領完整保留", same_after["units"][0]["items"][:2], same_snapshot["units"][0]["items"])
settle.assign(same_delta, [], [], [])
check("重算後每人金額包含原成員的減少", settle.totals(same_after)["twd"], {"bob": 50, "carol": 50, UNCLASSIFIED: 50})
check("同付款追加也不修改原結算", same_base, same_snapshot)
shared_base, _, _, _ = build([shot([("原商品", "", 1, 10)], 100)], [], [])
shared_incoming = settle.merge_shots([shot([("新商品", "", 1, 10)], 100)], start=2)[0]
shared_after, _, errors = settle.prepare_append(shared_base, shared_incoming)
check("沒有訂單號時以相同付款總額合併", (errors, len(shared_after["units"]), shared_after["units"][0]["rate"]), ([], 1, 5.0))
deduped, no_delta, errors = settle.prepare_append(same_base, settle.merge_shots([shot([("原登记商品", "白色", 1, 10)], 150, "5127481190470000001")], start=3)[0])
check("只有重複商品時回覆沒有新商品，完整結算維持原值", (deduped, no_delta, "沒有新的商品" in errors[0]), (same_snapshot, [], True))
conflicting = settle.merge_shots([shot([("漏掉商品", "", 1, 10)], 151, "5127481190470000001")], start=3)[0]
rejected, no_delta, errors = settle.prepare_append(same_base, pencil_units + conflicting)
check("同訂單付款金額衝突時整次拒絕，連前面的有效追加也不留下", (rejected, no_delta, "台幣金額和原結算不同" in errors[0]), (same_snapshot, [], True))
ambiguous = copy.deepcopy(shared_base)
ambiguous["units"].append(copy.deepcopy(ambiguous["units"][0]))
rejected, no_delta, errors = settle.prepare_append(ambiguous, shared_incoming)
check("多筆相同付款總額時說明需要訂單號", (rejected, no_delta, "多筆相同付款" in errors[0]), (ambiguous, [], True))
closed_append = copy.deepcopy(same_base)
settle.close(closed_append)
rejected, no_delta, errors = settle.prepare_append(closed_append, pencil_units)
check("已按 🏁 的結算拒絕追加", (rejected, no_delta, "已經結束認領" in errors[0]), (closed_append, [], True))

print("\nappend_overlap_notes：同付款的 OCR 差異要提醒重複")
overlap_base, _, _, _ = build([shot([("适用applepencil笔尖...", "白色2枚", 1, 9.8)], 46, "3317017045092056761")], [], [])
overlap_incoming = settle.merge_shots([shot([("适用applepencil笔尖苹果pencil", "白色2枚", 1, 9.8)], 46, "3317017045092056761")], start=2)[0]
overlap_after, overlap_delta, _ = settle.prepare_append(overlap_base, overlap_incoming)
overlap_notes = settle.append_overlap_notes(overlap_base, overlap_delta)
check("同訂單名稱被 OCR 截斷成不同寫法時提醒原編號", overlap_notes,
      ["#2 适用applepencil笔尖苹… 和原結算的 #1 看起來是同一件，可能重複，請確認這次追加的商品"])
check("模糊相似只提醒，保留商品讓確認者判斷", [it["no"] for it in overlap_after["units"][0]["items"]], [1, 2])
shared_overlap_base = copy.deepcopy(overlap_base)
shared_overlap_base["units"][0]["order_no"] = ""
shared_overlap_delta = copy.deepcopy(overlap_delta)
shared_overlap_delta[0]["order_no"] = ""
check("同樣沒有訂單號且付款相同也提醒", settle.append_overlap_notes(shared_overlap_base, shared_overlap_delta), overlap_notes)
different_order_delta = copy.deepcopy(overlap_delta)
different_order_delta[0]["order_no"] = "3317017045092056762"
check("不同訂單不產生同付款的模糊提醒", settle.append_overlap_notes(overlap_base, different_order_delta), [])
shared_overlap_delta[0]["total_twd"] = 47
check("沒有訂單號且付款不同不產生同付款提醒", settle.append_overlap_notes(shared_overlap_base, shared_overlap_delta), [])
different_price_delta = copy.deepcopy(overlap_delta)
different_price_delta[0]["items"][0]["price"] = 9.9
check("名稱相近但單價不同不提醒", settle.append_overlap_notes(overlap_base, different_price_delta), [])

print("\n追加通知：只列新商品、金額變化，通知台幣真的有變的人")
append_draft = settle.render_append_draft(append_base, appended, delta, [])
check("追加草稿只列新商品描述", ("#15 适用applepencil笔尖" in append_draft, "原商品" in append_draft), (True, False))
check("追加草稿顯示實付前後金額與差額", "實付合計 TWD 6,404 → TWD 6,450（+46）" in append_draft, True)
check("追加草稿沿用原付款與代傳確認權限，使用純名稱", ("payer 或 proxy 按 ✅" in append_draft, "<@" in append_draft), (True, False))
same_draft = settle.render_append_draft(same_base, same_after, same_delta, ["測試提醒"])
check("同付款草稿顯示每人金額減少與未認領增加", all(s in same_draft for s in ("bob　TWD 75 → TWD 50（-25）", "carol　TWD 75 → TWD 50（-25）", "還沒人認領　TWD 0 → TWD 50（+50）", "測試提醒")), True)
append_result = settle.render_append_result(append_base, appended, delta)
check("追加結果列出完整更新金額與正確付款對象", all(s in append_result for s in ("請轉帳給 <@100>", "<@300>　TWD 46", "bob　TWD", "按 🏁")), True)
check("追加結果同樣只列新商品描述", ("#15 适用applepencil笔尖" in append_result, "原商品" in append_result), (True, False))
check("尚有未認領時保留認領說明", "回覆 `認領 編號`" in append_result, True)
check("同付款金額變動名單排除 unclassified", settle.changed_owners(same_base, same_after), {"bob", "carol"})
round_before, _, _, _ = build([shot([("a", "", 1, 6), ("b", "", 1, 4)], 1)], [], [])
settle.claim(round_before, 1, "a")
settle.claim(round_before, 2, "b")
round_incoming = settle.merge_shots([shot([("c", "", 1, 3), ("d", "", 1, 3), ("e", "", 1, 4)], 1, "5127481190470000009")], start=3)[0]
round_after, round_delta, _ = settle.prepare_append(round_before, round_incoming)
for no, owner in zip((3, 4, 5), ("c", "d", "e")):
    settle.claim(round_after, no, owner)
check("獨立新付款的尾數分配也會改到原成員", (settle.totals(round_before)["twd"], settle.totals(round_after)["twd"]), ({"a": 1, "b": 0}, {"a": 1, "b": 1, "c": 0, "d": 0, "e": 0}))
check("通知比較完整結算的整數台幣，新增但仍為零的人略過", settle.changed_owners(round_before, round_after), {"b"})

print(f"\n全部 {PASSED} 項測試通過 ✓\n")
