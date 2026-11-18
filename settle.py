"""結算：下單截圖 → 品項 → 對到表格上的人 → 每人應付金額。

這裡只有純邏輯，不碰 Discord、n8n、LLM，所以可以直接測。
"""
import copy
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

def _number(value):
    """模型有時把數字寫成 "x1"、"¥9.8"、"9.80元"、"1,234"，取出裡面的數字；沒有數字回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(m.group()) if m else None


def clean_shot(raw: dict) -> dict:
    """把視覺模型的輸出整理成可信任的型別。讀不完整的商品不收，但要記下幾件（dropped），不能默默不見。"""
    raw = raw if isinstance(raw, dict) else {}
    items, dropped = [], 0
    for it in raw.get("items") if isinstance(raw.get("items"), list) else []:
        if not isinstance(it, dict):
            dropped += 1
            continue
        name = str(it.get("name") or "").strip()
        qty = 1 if it.get("qty") in (None, "") else _number(it.get("qty"))
        price = _number(it.get("paid_price"))
        if name and qty and qty >= 1 and price and price > 0:
            items.append({"name": name, "spec": str(it.get("spec") or "").strip(), "qty": int(qty), "price": round(price, 2)})
        else:
            dropped += 1
    total = _number(raw.get("total_twd"))
    order_no = re.sub(r"\D", "", str(raw.get("order_no") or ""))
    return {"items": items, "total_twd": int(round(total)) or None if total else None,
            "order_no": order_no if len(order_no) >= 10 else "", "dropped": dropped}


def merge_shots(shots: list[dict], start: int = 1) -> tuple[list[dict], list[str], list[str]]:
    """把多張截圖合併成「付款單位」：每個單位有自己的台幣實付金額。

    訂單詳情頁一張訂單一個單位（用訂單號認）；確認訂單頁所有截圖共用一個總額，
    捲動時重疊拍到的商品要去重。商品編號從 start 開始。回傳 (units, errors, notes)，errors 非空就不該繼續。
    """
    groups: dict[str, dict] = {}
    orphans, errors, notes = [], [], []

    def add(unit, shot):
        for it in shot["items"]:
            key = (norm(it["name"]), norm(it["spec"]), it["qty"], it["price"])
            if key not in unit["_seen"]:
                unit["_seen"].add(key)
                unit["items"].append(dict(it))

    for idx, shot in enumerate(shots, 1):
        label = f"第 {idx} 張截圖"  # 草稿裡所有截圖照傳的順序排，追加的接在後面
        if not shot["items"]:
            notes.append(f"{label}沒讀到任何商品，可能讀錯了，請檢查那張或重傳（那張本來就沒有商品的話可以忽略）")
        elif shot.get("dropped"):
            notes.append(f"{label}有 {shot['dropped']} 件商品讀不完整，被略過了，請檢查那張或重傳")
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
                errors.append(f"{label}和同一張訂單的其他截圖，台幣金額不一樣")
            unit["total_twd"] = unit["total_twd"] or shot["total_twd"]
        add(unit, shot)

    shared = [u for k, u in groups.items() if k.startswith("total:")]
    for idx, shot in orphans:
        if len(shared) == 1 and len(groups) == 1:
            add(shared[0], shot)
        elif shot["items"]:
            errors.append(f"第 {idx} 張截圖看不到台幣金額，沒辦法判斷屬於哪一筆付款")

    units, no = [], start - 1
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

    shared_totals = [u["total_twd"] for u in units if not u["order_no"]]
    if len(shared_totals) > 1:
        notes.append("截圖裡讀到 " + "、".join(f"TWD {t:,}" for t in shared_totals)
                     + " 這幾個不同的總額，已當成不同筆付款；如果其實是同一筆，代表有金額讀錯了")
    return units, errors, notes


def prepare_append(base: dict, incoming_units: list[dict]) -> tuple[dict, list[dict], list[str]]:
    """追加商品，保留原有編號及認領。delta 的新商品與 combined 共用物件，供 assign 寫入歸屬。"""
    combined = copy.deepcopy(base)
    if base.get("closed"):
        return combined, [], [f"結算 {base['batch']} 已經結束認領，不能再追加商品"]

    additions = {}
    for incoming in copy.deepcopy(incoming_units):
        order_no = incoming.get("order_no") or ""
        matches = [unit for unit in combined["units"]
                   if (order_no and unit.get("order_no") == order_no)
                   or (not order_no and not unit.get("order_no") and unit["total_twd"] == incoming["total_twd"])]
        if len(matches) > 1:
            return copy.deepcopy(base), [], ["追加截圖對到多筆相同付款，無法判斷要併進哪一筆，請提供有訂單號的截圖"]
        if matches and matches[0]["total_twd"] != incoming["total_twd"]:
            return copy.deepcopy(base), [], [f"訂單 …{order_no[-6:]} 的台幣金額和原結算不同，請確認截圖"]

        if matches:
            unit = matches[0]
        else:
            unit = dict(incoming, items=[])
            combined["units"].append(unit)
        identity = lambda it: (norm(it["name"]), norm(it.get("spec")), it["qty"], it["price"])
        seen = {identity(it) for it in unit["items"]}
        for item in incoming["items"]:
            key = identity(item)
            if key in seen:
                continue
            seen.add(key)
            item.setdefault("shares", [{"owner": UNCLASSIFIED, "cny": item["line"]}])
            item.setdefault("hint", "")
            item.setdefault("rows", [])
            unit["items"].append(item)
            additions.setdefault(id(unit), (unit, []))[1].append(item)
        unit["cny"] = round(sum(it["line"] for it in unit["items"]), 2)
        unit["rate"] = unit["total_twd"] / unit["cny"]

    if not additions:
        return copy.deepcopy(base), [], ["這些截圖的商品都已經在這筆結算裡，沒有新的商品可以追加"]
    added_units = [dict(unit, items=items) for unit, items in additions.values()]
    return combined, added_units, []


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


def _same_goods(a: dict, b: dict) -> bool:
    """同數量、同單價（到分），名稱又夠像，就當成同一件。確認頁和訂單頁的標題寫法不同，所以只看相似度。"""
    if a["qty"] != b["qty"] or abs(a["price"] - b["price"]) >= 0.005:
        return False
    ga, gb = _grams(a["name"]), _grams(b["name"])
    return len(ga & gb) / max(1, min(len(ga), len(gb))) >= 0.3


def append_overlap_notes(base: dict, added_units: list[dict]) -> list[str]:
    """同筆付款的 OCR 名稱有差異時保留新商品，但提醒確認是否重複。"""
    notes = []
    for unit in added_units:
        order_no = unit.get("order_no") or ""
        previous = [old for old in base["units"]
                    if (order_no and old.get("order_no") == order_no)
                    or (not order_no and not old.get("order_no") and old["total_twd"] == unit["total_twd"])]
        for item in unit["items"]:
            overlap = next((old for payment in previous for old in payment["items"] if _same_goods(old, item)), None)
            if overlap is not None:
                notes.append(f"#{item['no']} {_short(item['name'], 16)} 和原結算的 #{overlap['no']} 看起來是同一件，可能重複，請確認這次追加的商品")
    return notes


def duplicate_notes(units: list[dict], previous: list[dict]) -> list[str]:
    """這張草稿裡的商品，如果在之前的結算或同一張草稿的其他付款裡出現過，就提醒可能重複結算。"""
    notes, seen = [], []
    for unit in units:
        hit = next((s for s in previous for u in s["units"] if unit["order_no"] and u["order_no"] == unit["order_no"]), None)
        if hit:
            notes.append(f"訂單 …{unit['order_no'][-6:]} 已經在結算 {hit['batch']} 算過了，可能重複結算")
            seen += [(None, it) for it in unit["items"]]
            continue
        for it in unit["items"]:
            label = f"#{it['no']} {_short(it['name'], 16)} ×{it['qty']} ¥{it['price']:.2f}"
            old = next(((s["batch"], o) for s in previous for u in s["units"] for o in u["items"] if _same_goods(o, it)), None)
            if old:
                notes.append(f"{label} 和結算 {old[0]} 的 #{old[1]['no']} 看起來是同一件，可能重複結算")
            else:
                twin = next((o for u_, o in seen if u_ is not unit and _same_goods(o, it)), None)
                if twin:
                    notes.append(f"{label} 和這張草稿的 #{twin['no']} 看起來是同一件（同一筆訂單的不同頁面？），可能算了兩次")
            seen.append((unit, it))
    return notes


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
            it["shares"], it["hint"], it["rows"], it["row_ids"] = [{"owner": UNCLASSIFIED, "cny": it["line"]}], "", [], []

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
            it["row_ids"] = [r.get("id", "") for r in matched]  # 表格上不會變的列 ID，寫回和改歸屬時用
            used.update(ids)
            writes += [{"id": r.get("id", ""), "tab": r["tab"], "row": r["row"], "name": r["name"], "buyer": r["buyer"], "price": p}
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
    if state.get("closed"):
        return f"結算 {state['batch']} 已經結束認領了，有問題請直接找下單的人"
    _, item = find_item(state, no)
    if item is None:
        return f"這次結算沒有 #{no} 這個商品"
    if not is_unclassified(item):
        return f"#{no} 已經算在 {'、'.join(s['owner'] for s in item['shares'])} 名下了"
    item["shares"] = [{"owner": owner, "cny": item["line"]}]
    item["hint"] = ""
    return ""


def reassign(state: dict, no: int, owner: str) -> tuple[str, list[str]]:
    """下單人把某件商品改給 owner（對錯人、或幫沒上線的人指定）。

    回傳 (錯誤訊息, 要從表格上取消結算的列 ID)。原本對到的登記列其實不是這次買的，
    所以要把寫在那些列上的價格和批次清掉，之後它們才會再被拿來比對。
    """
    if state.get("closed"):
        return f"結算 {state['batch']} 已經結束認領了，不能再改", []
    _, item = find_item(state, no)
    if item is None:
        return f"這次結算沒有 #{no} 這個商品", []
    if [s["owner"] for s in item["shares"]] == [owner]:
        return f"#{no} 本來就算在 {owner} 名下", []
    released = [i for i in item.get("row_ids") or [] if i]
    item["shares"] = [{"owner": owner, "cny": item["line"]}]
    item["hint"], item["rows"], item["row_ids"] = "", [], []
    return "", released


def ledger_block(state: dict, updated: str) -> list[list]:
    """這筆結算在「結算紀錄」分頁的內容：每件商品（每個歸屬）一列，最後每個人一列小計。"""
    t = totals(state)
    status = "已結束" if state.get("closed") else "認領中"
    who = lambda o: "（沒人認領）" if o == UNCLASSIFIED else o
    block, allocations = [], {}
    for unit in state["units"]:
        for it in unit["items"]:
            for share in it["shares"]:
                qty = it["qty"] if len(it["shares"]) == 1 else round(share["cny"] / it["price"])
                raw_twd = share["cny"] * unit["rate"]
                allocations.setdefault(share["owner"], []).append((len(block), raw_twd))
                block.append([state["batch"], it["no"], it["name"], it.get("spec", ""), qty, share["cny"], who(share["owner"]),
                              math.floor(raw_twd), state["payer"], status, unit.get("order_no", ""), updated])
    # 先保留 Discord 已公布的每人小計，再把整數台幣分配到該人的商品明細。
    for owner, entries in allocations.items():
        leftover = t["twd"][owner] - sum(block[index][7] for index, _ in entries)
        by_remainder = sorted(entries, key=lambda entry: entry[1] - math.floor(entry[1]), reverse=True)
        for index, _ in by_remainder[:leftover]:
            block[index][7] += 1
    for owner in sorted(t["twd"], key=lambda o: (o == UNCLASSIFIED, o)):
        block.append([state["batch"], "", "【小計】", "", "", t["cny"][owner], who(owner), t["twd"][owner],
                      state["payer"], status, "", updated])
    return block


def has_unclassified(state: dict) -> bool:
    return any(is_unclassified(it) for u in state["units"] for it in u["items"])


def close(state: dict) -> list[int]:
    """結束認領：還沒人認領的全部算給下單人（可能是幫家人買的）。回傳被算給下單人的編號。"""
    leftover = []
    for unit in state["units"]:
        for it in unit["items"]:
            if is_unclassified(it):
                it["shares"] = [{"owner": state["payer"], "cny": it["line"]}]
                it["hint"] = ""
                leftover.append(it["no"])
    state["closed"] = True
    state["leftover"] = leftover
    return leftover


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
    poster = state.get("poster") or ""
    head = f"🧾 **結算 {state['batch']}**｜請轉帳給 {who(payer)}" if final else f"🧾 **結算草稿**｜下單人 {payer}"
    if poster:
        head += f"（{poster} 代傳）"
    paid = f"{n} 筆訂單，實付合計" if n > 1 else "實付"
    lines = [head, f"{paid} **TWD {t['total_twd']:,}**｜商品合計 ¥{t['total_cny']:.2f}｜1 CNY ≈ {t['rate']:.3f} TWD", ""]

    def item_lines(owner):
        if owner != UNCLASSIFIED:
            return buyer_lines(state, owner)
        out = []
        for unit in state["units"]:
            for it in unit["items"]:
                for share in it["shares"]:
                    if share["owner"] != owner:
                        continue
                    line = f"　#{it['no']} {_short(it['name'])} ×{it['qty']}　¥{share['cny']:.2f}"
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
        if lines[-1]:
            lines.append("")
        lines += [f"❓ **{UNCLASSIFIED}　TWD {t['twd'][UNCLASSIFIED]:,}**（¥{t['cny'][UNCLASSIFIED]:.2f}）沒對到登記的人"]
        lines += item_lines(UNCLASSIFIED)
        if final:
            lines.append("　→ 是你的就回覆 `認領 編號`；幫別人認領用 `認領 編號 @某人`")
            closer = f"{payer} 或 {poster}" if poster else payer
            lines.append(f"　→ {closer} 按 🏁 結束認領，到時還沒人認領的算 {payer} 自己的")

    notes = list(state.get("notes") or []) + rate_notes(state["units"], state.get("market_rate"))
    if notes:
        lines += ["", "⚠️ 請確認："] + [f"　{x}" for x in notes]

    if not final:
        who_confirms = f"{payer} 或 {poster} " if poster else "下單人"
        lines += ["", f"{who_confirms}按 ✅ 確認並寫入表格，按 ❌ 取消（10 分鐘內有效）",
                  "還有截圖的話直接再傳（不用打「結算」），會併進這張草稿，編號接著排"]
    return "\n".join(lines)


def buyer_lines(state: dict, owner: str) -> list[str]:
    """一個人名下的商品明細。"""
    out = []
    for unit in state["units"]:
        for it in unit["items"]:
            for share in it["shares"]:
                if share["owner"] != owner:
                    continue
                out.append(f"　#{it['no']} {_short(it['name'])} ×{it['qty']}　¥{share['cny']:.2f}")
    return out


def render_closed(state: dict, mentions: dict | None = None) -> str:
    """結束認領後的最終金額。每個人下面列出他買的商品。"""
    mentions = mentions or {}
    t = totals(state)
    payer = state["payer"]
    who = lambda name: mentions.get(name, name)
    lines = [f"🏁 **結算 {state['batch']} 結束認領**｜請轉帳給 {who(payer)}", ""]
    owe = sorted(o for o in t["twd"] if o != payer)
    if not owe:
        lines.append("沒有人需要轉帳。")
    for owner in owe:
        lines.append(f"**{who(owner)}　TWD {t['twd'][owner]:,}**")
        lines += buyer_lines(state, owner)
    left = state.get("leftover") or []
    if payer in t["twd"]:
        note = f"，含沒人認領的 {'、'.join(f'#{n}' for n in left)}" if left else ""
        lines += ["", f"下單人自己的 TWD {t['twd'][payer]:,} 不用轉{note}"]
        lines += buyer_lines(state, payer)
    return "\n".join(lines)


def render_totals(state: dict, mentions: dict | None = None) -> str:
    mentions = mentions or {}
    t = totals(state)
    lines = [f"{mentions.get(o, o)}　TWD {t['twd'][o]:,}" for o in sorted(t["twd"]) if o != UNCLASSIFIED]
    if UNCLASSIFIED in t["twd"]:
        left = [f"#{it['no']}" for u in state["units"] for it in u["items"] if is_unclassified(it)]
        lines.append(f"❓ 還沒人認領　TWD {t['twd'][UNCLASSIFIED]:,}（{'、'.join(left)}）")
    return "\n".join(lines)


def changed_owners(before: dict, after: dict) -> set[str]:
    old, new = totals(before)["twd"], totals(after)["twd"]
    return {owner for owner in old.keys() | new.keys()
            if owner != UNCLASSIFIED and old.get(owner, 0) != new.get(owner, 0)}


def _append_item_lines(added_units: list[dict], mentions: dict | None = None) -> list[str]:
    mentions = mentions or {}
    lines = []
    for unit in added_units:
        for item in unit["items"]:
            owners = "、".join(dict.fromkeys(mentions.get(s["owner"], s["owner"]) for s in item["shares"]))
            lines.append(f"#{item['no']} {_short(item['name'])} ×{item['qty']}　¥{item['line']:.2f}（{owners}）")
    return lines


def _append_paid_line(before: dict, after: dict) -> str:
    old, new = totals(before)["total_twd"], totals(after)["total_twd"]
    return f"實付合計 TWD {old:,} → TWD {new:,}（{new - old:+,}）"


def render_append_draft(base: dict, combined: dict, added_units: list[dict], notes: list[str], market=None) -> str:
    payer, poster = base["payer"], base.get("poster") or ""
    head = f"🧾 **追加結算草稿**｜結算 {base['batch']}｜下單人 {payer}"
    if poster:
        head += f"（{poster} 代傳）"
    lines = [head, _append_paid_line(base, combined), "", "**這次追加的商品**"]
    lines += _append_item_lines(added_units)
    old, new = totals(base)["twd"], totals(combined)["twd"]
    changes = [owner for owner in sorted(old.keys() | new.keys()) if old.get(owner, 0) != new.get(owner, 0)]
    lines += ["", "**金額變化**"]
    for owner in changes:
        label = "❓ 還沒人認領" if owner == UNCLASSIFIED else owner
        previous, current = old.get(owner, 0), new.get(owner, 0)
        lines.append(f"{label}　TWD {previous:,} → TWD {current:,}（{current - previous:+,}）")
    if not changes:
        lines.append("每人應付台幣金額維持原值")
    warnings = list(notes) + rate_notes(added_units, market)
    if warnings:
        lines += ["", "⚠️ 請確認"] + [f"　{note}" for note in warnings]
    confirmer = f"{payer} 或 {poster}" if poster else payer
    lines += ["", f"{confirmer} 按 ✅ 確認追加並寫入原結算批次，按 ❌ 取消（10 分鐘內有效）",
              "還有漏掉的截圖可以直接再傳，會併進這張追加草稿"]
    return "\n".join(lines)


def render_append_result(before: dict, after: dict, added_units: list[dict]) -> str:
    mentions = after.get("mentions") or {}
    payer, poster = after["payer"], after.get("poster") or ""
    who = lambda owner: mentions.get(owner, owner)
    lines = [f"🧾 **結算 {after['batch']} 已追加**｜請轉帳給 {who(payer)}",
             _append_paid_line(before, after), "", "**這次追加的商品**"]
    lines += _append_item_lines(added_units, mentions)
    lines += ["", "**更新後的每人金額**", render_totals(after, mentions)]
    if has_unclassified(after):
        lines += ["", "是你的商品就回覆 `認領 編號`，幫別人認領用 `認領 編號 @某人`"]
    closer = f"{payer} 或 {poster}" if poster else payer
    lines += [f"{closer} 按 🏁 結束認領，公布最終金額"]
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
