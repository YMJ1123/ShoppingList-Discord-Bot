"""結算：下單截圖 → 品項 → 對到表格上的人 → 每人應付金額。

這裡只有純邏輯，不碰 Discord、n8n、LLM，所以可以直接測。
"""
import math
import re

from opencc import OpenCC

UNCLASSIFIED = "unclassified"
_t2s = OpenCC("t2s").convert

EXTRACT_PROMPT = """這是淘寶 App 的訂單截圖（「确认订单」頁或訂單詳情頁）。抽出畫面中每一個商品，只輸出 JSON：
{"items":[{"name":"","spec":"","qty":1,"paid_price":0.0,"list_price":0.0}],"total_twd":0,"order_no":""}
規則：
- name、spec 照畫面上的字逐字抄寫，不要補完被截斷的字，不要翻譯，不要做繁簡轉換。
- paid_price 是「券后 ¥」或「实付价 ¥」後面的數字；list_price 是灰色的原價。
- qty 是商品右側的 xN。
- total_twd 是「立即支付TWD」或「实付款TWD」後面的數字，畫面上沒有就填 0。
- order_no 是「订单信息」那一行的長數字，畫面上沒有就填空字串。
- 價格被畫面切掉的商品不要輸出。"""

MATCH_PROMPT = """你在幫團購對帳。order 是下單截圖讀到的商品（name 可能被截斷、可能有個別錯字），candidates 是團員登記在表格裡的列。
判斷每個 order 商品對應到哪些表格列（同一商品可能有多人登記，所以可以多列；也可能沒有任何一列）。
規則：
- 必須是同一個商品而且規格相容才算對應。品牌相同但適用機型或品項不同（例如 iPad 膜對三星手機膜）不算。
- 團員登記的 name 有時是完整標題，有時只寫了規格名稱（例如只寫「集裝箱手辦盒」），兩邊的 name 和 spec 要交叉比對。
- 表格可能是繁體、截圖是簡體，視為相同。
- 沒把握就把 confidence 設成 low。
只輸出 JSON：{"results":[{"no":1,"rows":["淘寶12"],"confidence":"high","reason":"一句話"}]}"""


def norm(text) -> str:
    """比對用：繁轉簡、小寫、去掉標點和空白。"""
    return re.sub(r"[\W_]+", "", _t2s(str(text or "")).lower())


def row_id(row: dict) -> str:
    return f"{row['tab']}{row['row']}"


# ---------------------------------------------------------------- 截圖 → 訂單

def clean_shot(raw: dict) -> dict:
    """把視覺模型的輸出整理成可信任的型別，丟掉讀不完整的商品。"""
    items = []
    for it in raw.get("items") or []:
        try:
            qty = int(it.get("qty") or 1)
            price = round(float(it.get("paid_price") or 0), 2)
        except (TypeError, ValueError, AttributeError):
            continue
        name = str(it.get("name") or "").strip()
        if name and qty >= 1 and price > 0:
            items.append({"name": name, "spec": str(it.get("spec") or "").strip(), "qty": qty, "price": price})
    try:
        total = int(round(float(raw.get("total_twd") or 0))) or None
    except (TypeError, ValueError):
        total = None
    order_no = re.sub(r"\D", "", str(raw.get("order_no") or ""))
    return {"items": items, "total_twd": total, "order_no": order_no if len(order_no) >= 10 else ""}


def merge_shots(shots: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """把多張截圖合併成「付款單位」：每個單位有自己的台幣實付金額。

    訂單詳情頁一張訂單一個單位（用訂單號認）；確認訂單頁所有截圖共用一個總額，
    捲動時重疊拍到的商品要去重。回傳 (units, errors, notes)，errors 非空就不該繼續。
    """
    groups: dict[str, dict] = {}
    orphans, errors = [], []

    def add(unit, shot):
        for it in shot["items"]:
            key = (norm(it["name"]), norm(it["spec"]), it["qty"], it["price"])
            if key not in unit["_seen"]:
                unit["_seen"].add(key)
                unit["items"].append(dict(it))

    for idx, shot in enumerate(shots, 1):
        if shot["order_no"]:
            key = "order:" + shot["order_no"]
        elif shot["total_twd"]:
            key = f"total:{shot['total_twd']}"
        else:
            orphans.append((idx, shot))
            continue
        unit = groups.setdefault(key, {"order_no": shot["order_no"], "total_twd": None, "items": [], "_seen": set()})
        if shot["total_twd"]:
            if unit["total_twd"] and unit["total_twd"] != shot["total_twd"]:
                errors.append(f"第 {idx} 張截圖和同一張訂單的其他截圖，台幣金額不一樣")
            unit["total_twd"] = unit["total_twd"] or shot["total_twd"]
        add(unit, shot)

    shared = [u for k, u in groups.items() if k.startswith("total:")]
    for idx, shot in orphans:
        if len(shared) == 1 and len(groups) == 1:
            add(shared[0], shot)
        elif shot["items"]:
            errors.append(f"第 {idx} 張截圖看不到台幣金額，沒辦法判斷屬於哪一筆付款")

    units, no = [], 0
    for unit in groups.values():
        del unit["_seen"]
        if not unit["items"]:
            continue
        if not unit["total_twd"]:
            errors.append(f"訂單 {unit['order_no']} 的截圖看不到「实付款TWD」")
            continue
        for it in unit["items"]:
            no += 1
            it["no"] = no
            it["line"] = round(it["price"] * it["qty"], 2)
        unit["cny"] = round(sum(it["line"] for it in unit["items"]), 2)
        unit["rate"] = unit["total_twd"] / unit["cny"]
        units.append(unit)

    notes = []
    shared_totals = [u["total_twd"] for u in units if not u["order_no"]]
    if len(shared_totals) > 1:
        notes.append("截圖裡讀到 " + "、".join(f"TWD {t:,}" for t in shared_totals)
                     + " 這幾個不同的總額，已當成不同筆付款；如果其實是同一筆，代表有金額讀錯了")
    return units, errors, notes


# ---------------------------------------------------------------- 對到表格

def _grams(text) -> set:
    s = norm(text)
    return {s[i:i + 2] for i in range(len(s) - 1)} | set(s)


def candidates(item: dict, rows: list[dict], k: int = 6) -> list[dict]:
    """用字元重疊粗篩出最像的幾列，交給 LLM 做最後判斷。"""
    whole, name_only = _grams(item["name"] + item["spec"]), _grams(item["name"])
    scored = []
    for row in rows:
        g = _grams(row["name"] + row["spec"])
        score = len(whole & g) / max(1, min(len(whole), len(g))) + 0.5 * len(name_only & g) / max(1, len(name_only))
        scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return [row for score, row in scored[:k] if score > 0.25]


def match_payload(units: list[dict], rows: list[dict]) -> list[dict]:
    payload = []
    for unit in units:
        for it in unit["items"]:
            payload.append({
                "no": it["no"], "name": it["name"], "spec": it["spec"], "qty": it["qty"],
                "candidates": [{"id": row_id(r), "buyer": r["buyer"], "name": r["name"], "spec": r["spec"], "qty": r["qty"]}
                               for r in candidates(it, rows)],
            })
    return payload


def assign(units: list[dict], rows: list[dict], payload: list[dict], results: list[dict]) -> tuple[list[dict], list[str]]:
    """依 LLM 的比對結果決定每個商品算在誰頭上。回傳 (要寫回表格的列, 提醒事項)。

    只信任 LLM 回傳、而且確實在候選名單裡的列；同一列只能對到一個商品。
    """
    by_id = {row_id(r): r for r in rows}
    allowed = {p["no"]: {c["id"] for c in p["candidates"]} for p in payload}
    found = {}
    for r in results or []:
        try:
            found[int(r.get("no"))] = r
        except (TypeError, ValueError, AttributeError):
            continue

    used, writes, notes = set(), [], []
    for unit in units:
        for it in unit["items"]:
            res = found.get(it["no"]) or {}
            ids = [i for i in dict.fromkeys(res.get("rows") or []) if i in allowed.get(it["no"], ()) and i not in used]
            matched = [by_id[i] for i in ids]
            buyers = list(dict.fromkeys(r["buyer"] for r in matched))
            sheet_qty = sum(r["qty"] for r in matched)
            it["shares"], it["hint"], it["rows"] = [{"owner": UNCLASSIFIED, "cny": it["line"]}], "", []

            if not matched:
                continue
            if res.get("confidence") != "high":
                it["hint"] = "可能是 " + " 或 ".join(buyers)
                continue

            if sheet_qty == it["qty"]:
                it["shares"] = [{"owner": r["buyer"], "cny": round(it["price"] * r["qty"], 2)} for r in matched]
                prices = [s["cny"] for s in it["shares"]]
            elif len(buyers) == 1:
                it["shares"] = [{"owner": buyers[0], "cny": it["line"]}]
                prices = [it["line"]] + [None] * (len(matched) - 1)
                notes.append(f"#{it['no']} 訂單買 {it['qty']} 個，表格登記 {sheet_qty} 個（{'、'.join(ids)}），先全部算給 {buyers[0]}")
            else:
                it["hint"] = "可能是 " + " 或 ".join(buyers)
                notes.append(f"#{it['no']} 有 {len(buyers)} 個人登記，但數量加起來（{sheet_qty}）和訂單（{it['qty']}）對不上")
                continue

            it["rows"] = ids
            used.update(ids)
            writes += [{"tab": r["tab"], "row": r["row"], "name": r["name"], "buyer": r["buyer"], "price": p}
                       for r, p in zip(matched, prices)]
    return writes, notes


# ---------------------------------------------------------------- 金額

def totals(state: dict) -> dict:
    """每個人的人民幣小計與應付台幣。台幣取整數，而且加總一定等於實付金額。"""
    cny, raw = {}, {}
    for unit in state["units"]:
        for it in unit["items"]:
            for share in it["shares"]:
                cny[share["owner"]] = cny.get(share["owner"], 0) + share["cny"]
                raw[share["owner"]] = raw.get(share["owner"], 0) + share["cny"] * unit["rate"]
    total_twd = sum(u["total_twd"] for u in state["units"])
    twd = {o: math.floor(v) for o, v in raw.items()}
    leftover = total_twd - sum(twd.values())
    for owner in sorted(raw, key=lambda o: raw[o] - twd[o], reverse=True)[:leftover]:
        twd[owner] += 1
    total_cny = round(sum(u["cny"] for u in state["units"]), 2)
    return {"cny": {o: round(v, 2) for o, v in cny.items()}, "twd": twd,
            "total_twd": total_twd, "total_cny": total_cny, "rate": total_twd / total_cny}


def find_item(state: dict, no: int):
    for unit in state["units"]:
        for it in unit["items"]:
            if it["no"] == no:
                return unit, it
    return None, None


def is_unclassified(item: dict) -> bool:
    return any(s["owner"] == UNCLASSIFIED for s in item["shares"])


def claim(state: dict, no: int, owner: str) -> str:
    """把 unclassified 的商品歸給 owner。成功回空字串，失敗回原因。"""
    _, item = find_item(state, no)
    if item is None:
        return f"這次結算沒有 #{no} 這個商品"
    if not is_unclassified(item):
        return f"#{no} 已經算在 {'、'.join(s['owner'] for s in item['shares'])} 名下了"
    item["shares"] = [{"owner": owner, "cny": item["line"]}]
    item["hint"] = ""
    return ""


# ---------------------------------------------------------------- 訊息

def _short(text: str, limit: int = 30) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def rate_notes(units: list[dict], market=None) -> list[str]:
    """回算匯率離譜通常代表漏截圖或金額讀錯。有市場匯率就跟它比，否則跟其他訂單的中位數比。"""
    rates = sorted(u["rate"] for u in units)
    if market:
        ref, ref_name = market, "今天的市場匯率"
    elif len(units) >= 3:
        ref, ref_name = rates[len(rates) // 2], "其他訂單"
    else:
        return []
    notes = []
    for u in units:
        diff = u["rate"] / ref - 1
        if abs(diff) <= 0.04:
            continue
        label = f"訂單 …{u['order_no'][-6:]}" if u["order_no"] else f"TWD {u['total_twd']:,} 這筆付款"
        why = "可能漏了商品截圖，或有額外的稅費" if diff > 0 else "可能有商品被重複計算，或台幣金額讀錯了"
        notes.append(f"{label}的回算匯率 {u['rate']:.3f} 比{ref_name}（{ref:.3f}）{'高' if diff > 0 else '低'} {abs(diff):.0%}，{why}")
    return notes


def render(state: dict, final: bool, mentions: dict | None = None) -> str:
    mentions = mentions or {}
    t = totals(state)
    payer = state["payer"]
    who = lambda name: mentions.get(name, name)

    n = len(state["units"])
    head = f"🧾 **結算 {state['batch']}**｜請轉帳給 {who(payer)}" if final else f"🧾 **結算草稿**｜下單人 {payer}"
    paid = f"{n} 筆訂單，實付合計" if n > 1 else "實付"
    lines = [head, f"{paid} **TWD {t['total_twd']:,}**｜商品合計 ¥{t['total_cny']:.2f}｜1 CNY ≈ {t['rate']:.3f} TWD", ""]

    def item_lines(owner):
        out = []
        for unit in state["units"]:
            for it in unit["items"]:
                for share in it["shares"]:
                    if share["owner"] != owner:
                        continue
                    line = f"　#{it['no']} {_short(it['name'])} ×{it['qty']}　¥{share['cny']:.2f}"
                    if owner == UNCLASSIFIED:
                        line += f" ≈ TWD {share['cny'] * unit['rate']:,.0f}"
                        if it.get("hint"):
                            line += f"（{it['hint']}）"
                    out.append(line)
        return out

    for owner in sorted(o for o in t["twd"] if o != UNCLASSIFIED):
        tag = "（下單人自己的，不用轉）" if owner == payer else ""
        lines.append(f"**{who(owner)}　TWD {t['twd'][owner]:,}**（¥{t['cny'][owner]:.2f}）{tag}")
        lines += item_lines(owner)

    if UNCLASSIFIED in t["twd"]:
        lines += ["", f"❓ **{UNCLASSIFIED}　TWD {t['twd'][UNCLASSIFIED]:,}**（¥{t['cny'][UNCLASSIFIED]:.2f}）沒對到登記的人"]
        lines += item_lines(UNCLASSIFIED)
        if final:
            lines.append("　→ 是你的就回覆 `認領 編號`；幫別人認領用 `認領 編號 @某人`")

    notes = list(state.get("notes") or []) + rate_notes(state["units"], state.get("market_rate"))
    if notes:
        lines += ["", "⚠️ 請確認："] + [f"　{x}" for x in notes]

    if not final:
        lines += ["", "下單人按 ✅ 確認並寫入表格，按 ❌ 取消（10 分鐘內有效）"]
    return "\n".join(lines)


def render_totals(state: dict, mentions: dict | None = None) -> str:
    mentions = mentions or {}
    t = totals(state)
    lines = [f"{mentions.get(o, o)}　TWD {t['twd'][o]:,}" for o in sorted(t["twd"]) if o != UNCLASSIFIED]
    if UNCLASSIFIED in t["twd"]:
        left = [f"#{it['no']}" for u in state["units"] for it in u["items"] if is_unclassified(it)]
        lines.append(f"❓ 還沒人認領　TWD {t['twd'][UNCLASSIFIED]:,}（{'、'.join(left)}）")
    return "\n".join(lines)


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Discord 一則訊息上限 2000 字，照行切開。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks
