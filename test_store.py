#!/usr/bin/env python3
"""sheets.Store 和 shopping.py 的測試。Google Sheets 換成記憶體版，不連網路。

    python3 test_store.py
"""
import re
import sys
from copy import deepcopy
from types import SimpleNamespace

import shopping
import sheets
from sheets import Store, LEDGER, STATE

PASSED = 0


def check(name, got, expect):
    global PASSED
    if got != expect:
        print(f"  ✗ {name}\n      實際: {got!r}\n      預期: {expect!r}")
        sys.exit(1)
    PASSED += 1
    print(f"  ✓ {name}")


class FakeSheets:
    """記憶體裡的試算表，行為照 Google Sheets API：讀出來的列尾端空格會被省略。"""

    def __init__(self, tabs):
        self.tabs = {k: [list(r) for r in v] for k, v in tabs.items()}
        self.hidden, self.calls = [], []

    @staticmethod
    def _parse(rng):
        m = re.fullmatch(r"'(.+)'!([A-Z]+)(\d+)?(?::([A-Z]+)(\d+)?)?", rng)
        col = lambda s: sum((ord(c) - 64) * 26 ** i for i, c in enumerate(reversed(s))) - 1
        tab, c1, r1, c2, r2 = m.groups()
        return tab, col(c1), int(r1 or 1) - 1, (col(c2) if c2 else col(c1)), (int(r2) - 1 if r2 else None)

    def meta(self):
        return {name: i * 100 for i, name in enumerate(self.tabs)}

    def get(self, ranges):
        self.calls.append("get")
        out = []
        for rng in ranges:
            tab, c1, r1, c2, r2 = self._parse(rng)
            grid = self.tabs[tab]
            rows = [list(r[c1:c2 + 1]) for r in grid[r1:(r2 + 1 if r2 is not None else None)]]
            for r in rows:
                while r and r[-1] in ("", None):
                    r.pop()
            while rows and not rows[-1]:
                rows.pop()
            out.append(rows)
        return out

    def _set(self, tab, r, c, v):
        grid = self.tabs[tab]
        while len(grid) <= r:
            grid.append([])
        while len(grid[r]) <= c:
            grid[r].append("")
        grid[r][c] = v

    def update(self, data):
        self.calls.append("update")
        for d in data:
            tab, c1, r1, _, _ = self._parse(d["range"])
            for i, row in enumerate(d["values"]):
                for j, v in enumerate(row):
                    self._set(tab, r1 + i, c1 + j, v)

    def append(self, tab, row):
        self.calls.append("append")
        grid = self.tabs[tab]
        last = max((i for i, r in enumerate(grid) if any(c not in ("", None) for c in r)), default=-1)
        del grid[last + 1:]
        grid.append(list(row))

    def clear(self, ranges):
        for rng in ranges:
            tab, c1, r1, c2, r2 = self._parse(rng)
            for r in range(r1, (r2 if r2 is not None else len(self.tabs[tab]) - 1) + 1):
                for c in range(c1, c2 + 1):
                    if r < len(self.tabs[tab]) and c < len(self.tabs[tab][r]):
                        self.tabs[tab][r][c] = ""

    def batch(self, reqs):
        gids = {v: k for k, v in self.meta().items()}
        for q in reqs:
            if "addSheet" in q:
                self.tabs[q["addSheet"]["properties"]["title"]] = []
                if q["addSheet"]["properties"].get("hidden"):
                    self.hidden.append(q["addSheet"]["properties"]["title"])
            elif "deleteDimension" in q:
                rng = q["deleteDimension"]["range"]
                del self.tabs[gids[rng["sheetId"]]][rng["startIndex"]:rng["endIndex"]]
            elif "updateDimensionProperties" in q:
                rng = q["updateDimensionProperties"]["range"]
                self.hidden.append((gids[rng["sheetId"]], rng["startIndex"]))


HEAD = sheets.BASE_HEADER


def legacy():
    """上線中表格的樣子：淘寶已經有「結算批次」，京東還沒有；都沒有 ID。"""
    return FakeSheets({
        "淘寶": [HEAD + ["結算批次"],
                 ["小米風扇", "白色", "https://item.taobao.com/item.htm?id=111111111111&spm=a", "", 2, "", "", "amy", "done"],
                 [],
                 ["USB風扇", "", "https://e.tb.cn/h.abc?tk=1", "", "", "", "", "bob"],
                 ["小米風扇", "黑色", "https://item.taobao.com/item.htm?id=111111111111&utparam=z", "", 1, "", "", "amy"]],
        "京東": [HEAD,
                 ["電動牙刷", "", "https://item.jd.com/333333333333.html", "", 1, "", 19.9, "amy"]],
    })


print("\ncol_letter")
check("0 → A、8 → I、26 → AA", [sheets.col_letter(i) for i in (0, 8, 26)], ["A", "I", "AA"])

print("\nensure_layout：把現有表格補成新版需要的樣子")
fake = legacy()
store = Store(fake)
done = store.ensure_layout()
check("回報做了什麼", done, ["淘寶 加上「ID」欄", "淘寶 替 3 列補上 ID", "京東 加上「結算批次」欄", "京東 加上「ID」欄", "京東 替 1 列補上 ID",
                         "建立分頁「結算紀錄」", "建立分頁「_bot」"])
check("淘寶標題：ID 接在結算批次後面", fake.tabs["淘寶"][0][8:], ["結算批次", "ID"])
check("京東標題：補上結算批次和 ID", fake.tabs["京東"][0][8:], ["結算批次", "ID"])
ids = [r["id"] for r in store.rows()]
check("每一列都有 ID，而且不重複", (all(re.fullmatch(r"[0-9a-f]{8}", i) for i in ids), len(set(ids))), (True, 4))
check("ID 欄和 _bot 分頁設成隱藏", fake.hidden, ["_bot", ("淘寶", 9), ("京東", 9)])
check("結算紀錄有標題列", fake.tabs[LEDGER][0], sheets.LEDGER_HEADER)
check("再跑一次什麼都不用做", store.ensure_layout(), [])
check("再跑一次 ID 不會變", [r["id"] for r in store.rows()], ids)

stray = legacy()
stray.tabs["淘寶"][3] += ["", "雜項"]          # J 欄有沒標題的資料
Store(stray).ensure_layout()
check("新欄位避開沒標題的雜項資料", stray.tabs["淘寶"][0][8:], ["結算批次", "", "ID"])

print("\nrows")
rows = store.rows()
check("跳過空列、列號正確、數量空白當 1", [(r["tab"], r["row"], r["name"], r["qty"], r["buyer"], r["batch"]) for r in rows],
      [("淘寶", 2, "小米風扇", 2, "amy", "done"), ("淘寶", 4, "USB風扇", 1, "bob", ""), ("淘寶", 5, "小米風扇", 1, "amy", ""), ("京東", 2, "電動牙刷", 1, "amy", "")])

print("\nadd")
new = store.add("京東", {"name": "藍牙耳機", "spec": "灰", "url": "https://3.cn/x", "qty": 3, "buyer": "carol"})
last = fake.tabs["京東"][-1]
check("寫在正確的欄位，並帶著新的 ID", (last[0], last[1], last[2], last[4], last[7], last[9] == new["id"], len(new["id"])),
      ("藍牙耳機", "灰", "https://3.cn/x", 3, "carol", True, 8))
check("新增後讀得到", [r["name"] for r in store.rows() if r["tab"] == "京東"], ["電動牙刷", "藍牙耳機"])

print("\ndelete：認 ID，不認列號")
black = next(r for r in store.rows() if r["spec"] == "黑色")
usb = next(r for r in store.rows() if r["name"] == "USB風扇")
check("先刪掉上面的列（USB風扇）", store.delete(usb["id"])["name"], "USB風扇")
check("黑色風扇的列號因此往上移了", next(r for r in store.rows() if r["spec"] == "黑色")["row"], black["row"] - 1)
check("用原本的 ID 刪，刪到的還是黑色風扇", store.delete(black["id"])["spec"], "黑色")
check("其他列都還在", [(r["name"], r["spec"]) for r in store.rows() if r["tab"] == "淘寶"], [("小米風扇", "白色")])
check("ID 不存在 → 不刪任何東西", (store.delete("deadbeef"), len(store.rows())), (None, 3))
check("空的 ID 不會誤中沒有 ID 的列", store.delete(""), None)

print("\nsettle / unsettle")
brush = next(r for r in store.rows() if r["name"] == "電動牙刷")
ear = next(r for r in store.rows() if r["name"] == "藍牙耳機")
white = next(r for r in store.rows() if r["spec"] == "白色")
w = lambda r, price: {"id": r["id"], "tab": r["tab"], "row": r["row"], "name": r["name"], "price": price}
check("已經在別的批次結算過 → 有問題就完全不寫", store.settle("B1", [w(ear, 30), w(white, 5)]),
      [{"tab": "淘寶", "row": 2, "name": "小米風扇", "reason": "已經在批次 done 結算過"}])
check("…耳機沒有被寫到", next(r for r in store.rows() if r["name"] == "藍牙耳機")["batch"], "")
check("列已經被刪掉", store.settle("B1", [dict(w(ear, 30), id="deadbeef")])[0]["reason"], "這一列已經不存在（可能被刪掉了）")
before_preflight = deepcopy(fake.tabs)
preflight_updates = fake.calls.count("update")
check("結算預檢通過", store.validate_settlement("B1", [w(ear, 30.5), w(brush, None)]), [])
check("結算預檢仍會核對商品快照", store.validate_settlement("B1", [dict(w(ear, 30.5), name="已被換掉的商品")])[0]["reason"],
      "這一列的商品或購買人已經變了，請重新確認結算草稿")
check("結算預檢會回報已結算的衝突", store.validate_settlement("B1", [w(white, 5)])[0]["reason"], "已經在批次 done 結算過")
check("預檢成功或失敗都保留原表格", (fake.tabs, fake.calls.count("update")), (before_preflight, preflight_updates))
check("正常寫入", store.settle("B1", [w(ear, 30.5), w(brush, None)]), [])
got = {r["name"]: (r["price"], r["batch"]) for r in store.rows() if r["tab"] == "京東"}
check("寫了價格和批次；price=None 只寫批次、不動原本的價格", got, {"電動牙刷": (19.9, "B1"), "藍牙耳機": (30.5, "B1")})
check("同批次、同價格再送一次（逾時重試）→ 接受", store.settle("B1", [w(ear, 30.5)]), [])
check("同批次但價格不同 → 拒絕", store.settle("B1", [w(ear, 99)])[0]["reason"], "這一列同批次的實際價格已經變了")
check("別的批次 → 拒絕", store.settle("B2", [w(ear, 30.5)])[0]["reason"], "已經在批次 B1 結算過")
check("同一列出現兩次 → 拒絕", store.settle("B1", [w(ear, 30.5), w(ear, 30.5)])[0]["reason"], "同一列重複出現")
check("unsettle 只清這個批次寫的", (store.unsettle("B9", [ear["id"]]), store.unsettle("B1", [ear["id"]])), (0, 1))
check("清掉後價格和批次都空了", [(r["price"], r["batch"]) for r in store.rows() if r["name"] == "藍牙耳機"], [("", "")])

print("\n結算紀錄分頁")
row = lambda b, n: [b, n, f"商品{n}", "", 1, 10.0, "amy", 50, "bob", "認領中", "", "t"]
store.write_ledger("B1", [row("B1", 1), row("B1", 2)])
store.write_ledger("B2", [row("B2", 3)])
check("兩個批次依序接在後面", [(r[0], r[1]) for r in fake.tabs[LEDGER][1:]], [("B1", 1), ("B1", 2), ("B2", 3)])
store.write_ledger("B1", [row("B1", 1), row("B1", 2), row("B1", 9)])
check("批次變長：原地換掉，後面的批次往後移", [(r[0], r[1]) for r in fake.tabs[LEDGER][1:] if r and r[0]], [("B1", 1), ("B1", 2), ("B1", 9), ("B2", 3)])
store.write_ledger("B1", [row("B1", 1)])
check("批次變短：多出來的列清乾淨", [(r[0], r[1]) for r in fake.tabs[LEDGER][1:] if r and r[0]], [("B1", 1), ("B2", 3)])

print("\nbot 狀態（隱藏分頁）")
big = {"batch": "B1", "blob": "字" * (sheets.CELL_LIMIT + 123), "units": [{"items": [{"no": 1}]}]}
store.write_state("B1", big)
store.write_state("B2", {"batch": "B2"})
check("超過一格上限會切成好幾格", len([c for c in fake.tabs[STATE][1][2:] if c]) >= 2, True)
check("讀回來一模一樣", store.read_states(), {"B1": big, "B2": {"batch": "B2"}})
store.write_state("B1", {"batch": "B1", "closed": True})
check("同一批次再存一次：原地更新，舊的長內容不殘留", store.read_states(), {"B1": {"batch": "B1", "closed": True}, "B2": {"batch": "B2"}})

print("\n狀態與公開帳本一起儲存")
one = {"batch": "B1", "closed": False}
updates_before = fake.calls.count("update")
store.save_settlement("B1", one, [row("B1", 7)])
check("公開帳本與完整狀態只送一次寫入", fake.calls.count("update") - updates_before, 1)
check("完整狀態與帳本對上同一版", (store.read_states()["B1"], [r[1] for r in fake.tabs[LEDGER][1:] if r and r[0] == "B1"]),
      (one, [7]))

huge = {"batch": "B1", "blob": "字" * (sheets.CELL_LIMIT * 25)}
store.write_state("B1", huge)
check("超過 24 格時分成多列，仍然完整還原", store.read_states()["B1"], huge)
check("分成多列後保留其他批次", store.read_states()["B2"], {"batch": "B2"})
store.write_state("B1", {"batch": "B1"})
check("多列狀態縮回一列，舊片段清乾淨", store.read_states(), {"B1": {"batch": "B1"}, "B2": {"batch": "B2"}})

old_state = deepcopy(fake.tabs[STATE])
real_update = fake.update

def failed_update(data):
    raise sheets.SheetsError("temporary write failure")

fake.update = failed_update
try:
    store.write_state("B1", {"batch": "B1", "new": True})
except sheets.SheetsError:
    pass
finally:
    fake.update = real_update
check("狀態寫入失敗仍保留上一次完整資料", fake.tabs[STATE], old_state)
old_tabs = deepcopy(fake.tabs)
fake.update = failed_update
try:
    store.save_settlement("B1", {"batch": "B1", "changed": True}, [row("B1", 12)], [brush["id"], white["id"]])
except sheets.SheetsError:
    pass
finally:
    fake.update = real_update
check("改歸屬寫入失敗時，帳本、完整狀態與原列全部保留", fake.tabs, old_tabs)
before_count = fake.calls.count("update")
store.save_settlement("B1", {"batch": "B1", "changed": True}, [row("B1", 12)], [brush["id"], white["id"]])
check("改歸屬、清除錯誤配對與完整狀態一起寫入", fake.calls.count("update") - before_count, 1)
check("清除只處理所指定批次的商品", {r["id"]: r["batch"] for r in store.rows() if r["id"] in [brush["id"], white["id"]]},
      {brush["id"]: "", white["id"]: "done"})

print("\n商品快照與 ID 驗證")

def error_of(call):
    try:
        call()
    except sheets.SheetsError as error:
        return str(error)
    return ""

guarded = legacy()
guard = Store(guarded)
guard.ensure_layout()
target = next(r for r in guard.rows() if r["name"] == "USB風扇")
guarded.tabs["淘寶"][target["row"] - 1][7] = "carol"
check("買家在讀取後變動，刪除再次核對所有人", guard.delete(target["id"], expected_buyer="bob"), None)
check("原選項內容變動，刪除再次核對快照", guard.delete(target["id"], expected=target), None)
write = {"id": target["id"], "name": target["name"], "buyer": "bob", "price": 30}
check("結算草稿後買家更換，回寫拒絕", guard.settle("B3", [write])[0]["reason"], "這一列的商品或購買人已經變了，請重新確認結算草稿")
guarded.tabs["淘寶"][target["row"] - 1][7] = "bob"
guarded.tabs["淘寶"][target["row"] - 1][0] = "不同商品"
check("結算草稿後名稱更換，回寫拒絕", guard.settle("B3", [write])[0]["reason"], "這一列的商品或購買人已經變了，請重新確認結算草稿")
for invalid in (float("nan"), float("inf"), -1, "價格?", True):
    problems = guard.settle("B3", [dict(write, name="不同商品", price=invalid)])
    check(f"無效價格 {invalid!r} 不寫入", (bool(problems), next(r for r in guard.rows() if r["id"] == target["id"])["batch"]), (True, ""))

duplicate = legacy()
dup_store = Store(duplicate)
dup_store.ensure_layout()
duplicate.tabs["淘寶"][3][9] = duplicate.tabs["淘寶"][1][9]
check("複製列導致重複 ID 時明確回報", "重複" in error_of(dup_store.rows), True)
check("重複 ID 不會挑第一列刪掉", "重複" in error_of(lambda: dup_store.delete(duplicate.tabs["淘寶"][1][9])), True)
broken = legacy()
broken.tabs["京東"][0][7] = "被改掉的標題"
check("缺必要欄位會回報，避免寫進錯誤欄", "購買人" in error_of(Store(broken).ensure_layout), True)

print("\n完整狀態的批次核對")
safe = legacy()
safe_store = Store(safe)
safe_store.ensure_layout()
safe_store.save_settlement("B1", {"batch": "B1", "closed": False}, [row("B1", 1)])
safe_before = deepcopy(safe.tabs)
check("拒絕將別的批次狀態存入目前帳本", "批次" in error_of(lambda: safe_store.save_settlement("B1", {"batch": "B2"}, [row("B1", 2)])), True)
check("批次核對失敗保留帳本與狀態", safe.tabs, safe_before)
safe.tabs[STATE][1][2] = '{"batch":"B2","closed":true}'
check("恢復時拒絕批次鍵與 JSON 內容不符", "狀態讀不回來" in error_of(safe_store.read_states), True)
safe.tabs[STATE][1][2] = '{invalid json'
check("恢復時明確回報損壞狀態", "狀態讀不回來" in error_of(safe_store.read_states), True)

print("\nHTTP 重試不能重複新增或刪列")

def retry_count(operation, statuses):
    original_request, original_sleep = sheets.requests.request, sheets.time.sleep
    calls = []
    api = sheets.Sheets("fake", {})
    api._headers = lambda: {}

    def request(*args, **kwargs):
        status = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(status)
        return SimpleNamespace(status_code=status, ok=status < 400, text="failed", json=lambda: {})

    sheets.requests.request = request
    sheets.time.sleep = lambda _: None
    try:
        operation(api)
    except sheets.SheetsError:
        pass
    finally:
        sheets.requests.request, sheets.time.sleep = original_request, original_sleep
    return len(calls)

check("append 回 500 不盲目重送", retry_count(lambda api: api.append("淘寶", ["商品"]), [500, 200]), 1)
check("刪列回 503 不盲目重送", retry_count(lambda api: api.batch([{"deleteDimension": {}}]), [503, 200]), 1)
check("建立分頁回 500 不盲目重送", retry_count(lambda api: api.batch([{"addSheet": {}}]), [500, 200]), 1)
check("設定絕對儲存格值可重試", retry_count(lambda api: api.update([{"range": "A1", "values": [[1]]}]), [503, 200]), 2)
check("429 代表請求遭拒，新增仍可等待重試", retry_count(lambda api: api.append("淘寶", ["商品"]), [429, 200]), 2)

print("\nshopping.url_key / platform")
check("淘寶／天貓只認商品 ID", [shopping.url_key(u) for u in (
    "https://item.taobao.com/item.htm?abbucket=10&id=652762230399&skuId=4709&spm=a",
    "https://detail.tmall.com/item.htm?id=652762230399&utparam=zzz",
    "https://item.jd.com/100024268712.html?cu=true")], ["item:652762230399", "item:652762230399", "item:100024268712"])
check("skuId、mi_id 不會被當成商品 ID", shopping.url_key("https://item.taobao.com/item.htm?skuId=123456789&mi_id=0000123456"), "https://item.taobao.com/item.htm")
check("短連結退回去掉參數的網址", shopping.url_key("https://e.tb.cn/h.ABC/?tk=xyz"), "https://e.tb.cn/h.abc")
check("空的", shopping.url_key(None), "")
check("平台", [shopping.platform(t, u) for t, u in (("買 【京东】耳機", "https://3.cn/x"), ("買 耳機", "https://item.jd.com/1.html"),
                                                   ("買 風扇", "https://e.tb.cn/h.x"), ("買 風扇", "https://detail.tmall.com/item.htm?id=1"))],
      ["京東", "京東", "淘寶", "淘寶"])

print("\nshopping.find_delete_target")
R = lambda i, name, url="", spec="": {"id": i, "name": name, "url": url, "spec": spec}
mine = [R("a", "小米風扇", "https://item.taobao.com/item.htm?id=111111111111&spm=a", "白色"), R("b", "USB風扇", "https://item.taobao.com/item.htm?id=222222222222"),
        R("c", "小米風扇", "https://item.taobao.com/item.htm?id=111111111111&utparam=zzz", "黑色"), R("d", "電動牙刷", "https://item.jd.com/333333333333.html")]
ids_of = lambda res: (res[0], [r["id"] for r in res[1]])
check("名稱完全相同、唯一 → 直接刪", ids_of(shopping.find_delete_target("", "電動牙刷", mine)), ("one", ["d"]))
check("同一個商品 ID、追蹤參數不同、唯一", ids_of(shopping.find_delete_target("https://item.jd.com/333333333333.html?spm=1", "", mine)), ("one", ["d"]))
check("模糊只剩一筆", ids_of(shopping.find_delete_target("", "USB", mine)), ("one", ["b"]))
check("完全相同優先於更長的名稱", ids_of(shopping.find_delete_target("", "小米風扇", [R("a", "小米風扇"), R("b", "小米風扇套")])), ("one", ["a"]))
check("同一個商品 ID 有兩列 → 要選", ids_of(shopping.find_delete_target("https://item.taobao.com/item.htm?id=111111111111", "小米風扇", mine)), ("choose", ["a", "c"]))
check("兩個完全同名 → 要選", ids_of(shopping.find_delete_target("", "小米風扇", mine)), ("choose", ["a", "c"]))
check("模糊對到三筆 → 要選", ids_of(shopping.find_delete_target("", "風扇", mine)), ("choose", ["a", "b", "c"]))
check("網址沒對到 → 再用名稱找", ids_of(shopping.find_delete_target("https://item.taobao.com/item.htm?id=999999999999", "電動牙刷", mine)), ("one", ["d"]))
check("都沒有", ids_of(shopping.find_delete_target("", "不存在", mine)), ("none", []))
check("沒有網址也沒有名稱", ids_of(shopping.find_delete_target("", "", mine)), ("none", []))
check("繁體指令能找到簡體商品", ids_of(shopping.find_delete_target("", "電動牙刷", [R("a", "电动牙刷")])), ("one", ["a"]))
check("簡體指令能找到繁體商品", ids_of(shopping.find_delete_target("", "小米风扇", [R("a", "小米風扇")])), ("one", ["a"]))
check("繁簡同名的多筆仍須選擇", ids_of(shopping.find_delete_target("", "小米風扇", [R("a", "小米風扇"), R("b", "小米风扇")])), ("choose", ["a", "b"]))

print("\nshopping.find_duplicates")
mine2 = [dict(R("a", "小米風扇", "https://item.taobao.com/item.htm?id=111111111111&spm=a"), batch=""),
         dict(R("b", "小米風扇", "https://item.taobao.com/item.htm?id=111111111111"), batch="done"),
         dict(R("c", "牙刷", "https://e.tb.cn/h.x?tk=1"), batch="")]
check("同一個商品 ID 而且還沒結算的才算重複", [r["id"] for r in shopping.find_duplicates("https://detail.tmall.com/item.htm?id=111111111111&x=1", mine2)], ["a"])
check("短連結也認得", [r["id"] for r in shopping.find_duplicates("https://e.tb.cn/h.x?tk=2", mine2)], ["c"])
check("沒登記過", shopping.find_duplicates("https://item.taobao.com/item.htm?id=555555555555", mine2), [])

print(f"\n全部 {PASSED} 項測試通過 ✓\n")
