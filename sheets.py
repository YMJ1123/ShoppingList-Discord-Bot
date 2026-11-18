"""直接讀寫 Google 試算表（取代 n8n）。

Sheets  ：最底層，一個方法對一個 Google Sheets API 呼叫。測試時換成記憶體版。
Store   ：購物清單的操作。刪除和結算先依固定 ID 重新定位商品，
          再呼叫 Google Sheets API 處理商品目前所在的列。
"""
import json
import math
import secrets
import time
from datetime import datetime

import requests

TABS = ("淘寶", "京東")
BASE_HEADER = ["預購買商品", "商品型號規格", "商品網址", "商品價格", "商品數量", "預估價格", "實際價格", "購買人"]
COL = {"name": "預購買商品", "spec": "商品型號規格", "url": "商品網址", "qty": "商品數量",
       "price": "實際價格", "buyer": "購買人", "batch": "結算批次", "id": "ID"}
LEDGER, STATE = "結算紀錄", "_bot"
LEDGER_HEADER = ["批次", "編號", "商品", "規格", "數量", "人民幣", "歸屬", "應付台幣", "下單人", "狀態", "訂單號", "更新時間"]
STATE_HEADER = ["batch", "updated", "state"]
CELL_LIMIT = 40000  # Google 一格最多 50,000 字


def col_letter(index: int) -> str:
    s, n = "", index + 1
    while n > 0:
        s, n = chr(65 + (n - 1) % 26) + s, (n - 1) // 26
    return s


class SheetsError(Exception):
    pass


class Sheets:
    """Google Sheets REST API。憑證是 OAuth 的 client_id / client_secret / refresh_token。"""

    def __init__(self, spreadsheet_id: str, credentials: dict):
        self.id, self.cred = spreadsheet_id, credentials
        self.token, self.token_until = "", 0.0
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"

    def _headers(self) -> dict:
        if time.time() > self.token_until - 60:
            r = requests.post("https://oauth2.googleapis.com/token", timeout=20,
                              data={**self.cred, "grant_type": "refresh_token"})
            if not r.ok:
                raise SheetsError(f"Google 授權失敗（{r.status_code}）：{r.text[:200]}")
            body = r.json()
            self.token, self.token_until = body["access_token"], time.time() + body.get("expires_in", 3600)
        return {"Authorization": f"Bearer {self.token}"}

    def _call(self, method: str, path: str, retry: bool = True, **kw):
        for wait in (2, 5, 12, None):  # 每分鐘讀寫次數有上限，撞到就等一下再試
            r = requests.request(method, self.base + path, headers=self._headers(), timeout=30, **kw)
            can_retry = r.status_code == 429 or (retry and r.status_code in (500, 502, 503))
            if not can_retry or wait is None:
                break
            time.sleep(wait)
        if not r.ok:
            raise SheetsError(f"Google Sheets 回 {r.status_code}：{r.text[:200]}")
        return r.json()

    def meta(self) -> dict:
        """分頁名稱 → sheetId。"""
        body = self._call("GET", "?fields=sheets.properties(sheetId,title)")
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in body["sheets"]}

    def get(self, ranges: list[str]) -> list[list[list]]:
        body = self._call("GET", "/values:batchGet",
                          params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"})
        return [vr.get("values", []) for vr in body["valueRanges"]]

    def update(self, data: list[dict]):
        if data:
            self._call("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})

    def append(self, tab: str, row: list):
        self._call("POST", f"/values/'{tab}'!A1:append", json={"values": [row]},
                   params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, retry=False)

    def clear(self, ranges: list[str]):
        if ranges:
            self._call("POST", "/values:batchClear", json={"ranges": ranges})

    def batch(self, reqs: list[dict]):
        if reqs:
            retry = all("updateDimensionProperties" in request for request in reqs)
            self._call("POST", ":batchUpdate", json={"requests": reqs}, retry=retry)


class Store:
    def __init__(self, sheets):
        self.sheets = sheets
        self._gids: dict | None = None

    # ------------------------------------------------------------ 讀
    @staticmethod
    def _text(v) -> str:
        return "" if v is None else str(v).strip()

    def _read(self) -> dict:
        out = {}
        for tab, values in zip(TABS, self.sheets.get([f"'{t}'!A1:Z" for t in TABS])):
            header = [self._text(h) for h in (values[0] if values else [])]
            col = {k: (header.index(title) if title in header else -1) for k, title in COL.items()}
            missing = [COL[key] for key in ("name", "spec", "url", "qty", "price", "buyer") if col[key] < 0]
            if missing:
                raise SheetsError(f"分頁「{tab}」缺少欄位：{'、'.join(missing)}")
            out[tab] = {"header": header, "values": values, "col": col,
                        "width": max((len(r) for r in values), default=0)}
        return out

    def _rows(self, data: dict) -> list[dict]:
        rows, seen = [], set()
        for tab, t in data.items():
            col = t["col"]
            cell = lambda r, k: (r[col[k]] if 0 <= col[k] < len(r) else "")
            for i, r in enumerate(t["values"][1:], 2):
                name = self._text(cell(r, "name"))
                if not name:
                    continue
                try:
                    qty = float(cell(r, "qty"))
                    qty = int(qty) if qty > 0 else 1
                except (TypeError, ValueError, OverflowError):
                    qty = 1
                row_id = self._text(cell(r, "id"))
                if row_id and row_id in seen:
                    raise SheetsError(f"商品 ID {row_id} 重複，請清除複製列的 ID 再重新載入表格")
                if row_id:
                    seen.add(row_id)
                rows.append({"tab": tab, "row": i, "id": row_id, "name": name,
                             "spec": self._text(cell(r, "spec")), "url": self._text(cell(r, "url")), "qty": qty,
                             "buyer": self._text(cell(r, "buyer")), "price": cell(r, "price"),
                             "batch": self._text(cell(r, "batch"))})
        return rows

    def rows(self) -> list[dict]:
        """兩個分頁所有有商品名稱的列（含已結算的，由呼叫的人自己篩）。"""
        return self._rows(self._read())

    # ------------------------------------------------------------ 準備表格
    def ensure_layout(self) -> list[str]:
        """補上需要的欄位和分頁，替沒有 ID 的列補 ID。可以重複執行。回傳做了哪些事。"""
        done = []
        gids = self.sheets.meta()
        data = self._read()
        self._rows(data)  # 已有 ID 重複時先回報，避免替其他列寫入一半才失敗
        updates, reqs = [], []
        for tab, t in data.items():
            col, width = t["col"], t["width"]
            for key in ("batch", "id"):
                if col[key] < 0:
                    col[key], width = width, width + 1
                    updates.append({"range": f"'{tab}'!{col_letter(col[key])}1", "values": [[COL[key]]]})
                    done.append(f"{tab} 加上「{COL[key]}」欄")
                    if key == "id":
                        reqs.append({"updateDimensionProperties": {
                            "range": {"sheetId": gids[tab], "dimension": "COLUMNS",
                                      "startIndex": col["id"], "endIndex": col["id"] + 1},
                            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
            missing = 0
            for i, r in enumerate(t["values"][1:], 2):
                has_name = 0 <= col["name"] < len(r) and self._text(r[col["name"]])
                has_id = 0 <= col["id"] < len(r) and self._text(r[col["id"]])
                if has_name and not has_id:
                    updates.append({"range": f"'{tab}'!{col_letter(col['id'])}{i}", "values": [[secrets.token_hex(4)]]})
                    missing += 1
            if missing:
                done.append(f"{tab} 替 {missing} 列補上 ID")
        for title, header, hidden in ((LEDGER, LEDGER_HEADER, False), (STATE, STATE_HEADER, True)):
            if title not in gids:
                props = {"title": title, "gridProperties": {"frozenRowCount": 1}}
                if hidden:
                    props["hidden"] = True
                reqs.insert(0, {"addSheet": {"properties": props}})
                updates.append({"range": f"'{title}'!A1", "values": [header]})
                done.append(f"建立分頁「{title}」")
        self.sheets.batch(reqs)
        self.sheets.update(updates)
        self._gids = None
        return done

    # ------------------------------------------------------------ 新增／刪除
    def add(self, tab: str, item: dict) -> dict:
        t = self._read()[tab]
        col = t["col"]
        if col["id"] < 0 or col["batch"] < 0:
            raise SheetsError(f"分頁「{tab}」還沒有 ID 欄，請先執行 ensure_layout")
        row = [""] * max(len(t["header"]), col["id"] + 1)
        row_id = secrets.token_hex(4)
        for key, value in (("name", item["name"]), ("spec", item.get("spec", "")), ("url", item.get("url", "")),
                           ("qty", item.get("qty", 1)), ("buyer", item["buyer"]), ("id", row_id)):
            row[col[key]] = value
        self.sheets.append(tab, row)
        return {"tab": tab, "id": row_id, **item}

    def delete(self, row_id: str, expected_buyer: str | None = None, expected: dict | None = None) -> dict | None:
        """重新定位 ID 目前所在列，核對所有人與選擇時的商品快照後刪除。"""
        target = next((r for r in self.rows() if r["id"] and r["id"] == row_id), None)
        if target is None:
            return None
        if expected_buyer is not None and target["buyer"] != expected_buyer:
            return None
        if expected and any(target[key] != expected[key] for key in ("name", "spec", "url", "qty", "buyer", "batch") if key in expected):
            return None
        if self._gids is None:
            self._gids = self.sheets.meta()
        self.sheets.batch([{"deleteDimension": {"range": {
            "sheetId": self._gids[target["tab"]], "dimension": "ROWS",
            "startIndex": target["row"] - 1, "endIndex": target["row"]}}}])
        return target

    # ------------------------------------------------------------ 結算
    def _settlement_plan(self, batch: str, writes: list[dict]) -> tuple[list[dict], list[dict]]:
        """核對目前列內容，回傳問題清單及準備好的儲存格更新。"""
        data = self._read()
        by_id = {r["id"]: r for r in self._rows(data) if r["id"]}
        problems, updates, seen = [], [], set()
        for w in writes:
            cur = by_id.get(w.get("id") or "")
            where = {"tab": w.get("tab", ""), "row": w.get("row", ""), "name": w.get("name", "")}
            reason = ""
            if cur is None:
                reason = "這一列已經不存在（可能被刪掉了）"
            elif cur["id"] in seen:
                reason = "同一列重複出現"
            elif any(cur[key] != w[key] for key in ("name", "buyer", "spec", "qty") if key in w):
                reason = "這一列的商品或購買人已經變了，請重新確認結算草稿"
            elif cur["batch"] and cur["batch"] != batch:
                reason = f"已經在批次 {cur['batch']} 結算過"
            price = None
            if not reason and w.get("price") is not None:
                try:
                    price = float(w["price"])
                    if isinstance(w["price"], bool) or not math.isfinite(price) or price < 0:
                        reason = "實際價格必須是有限的非負數字"
                    elif cur["batch"] == batch and (cur["price"] in ("", None) or float(cur["price"]) != price):
                        reason = "這一列同批次的實際價格已經變了"
                except (TypeError, ValueError, OverflowError):
                    reason = "實際價格不是有效數字"
            if reason:
                problems.append({**where, "reason": reason})
                continue
            seen.add(cur["id"])
            col = data[cur["tab"]]["col"]
            if col["price"] < 0 or col["batch"] < 0:
                raise SheetsError(f"分頁「{cur['tab']}」還沒有結算欄位，請先執行 ensure_layout")
            if w.get("price") is not None:
                updates.append({"range": f"'{cur['tab']}'!{col_letter(col['price'])}{cur['row']}", "values": [[price]]})
            updates.append({"range": f"'{cur['tab']}'!{col_letter(col['batch'])}{cur['row']}", "values": [[batch]]})
        return problems, updates

    def validate_settlement(self, batch: str, writes: list[dict]) -> list[dict]:
        """確認草稿目前仍可結算，回傳問題清單。"""
        problems, _ = self._settlement_plan(batch, writes)
        return problems

    def settle(self, batch: str, writes: list[dict]) -> list[dict]:
        """核對指定 ID 的列，整批通過後回寫價格與批次，回傳問題清單。"""
        problems, updates = self._settlement_plan(batch, writes)
        if not problems:
            self.sheets.update(updates)
        return problems

    def unsettle(self, batch: str, ids: list[str]) -> int:
        """把這個批次寫在這些列上的價格和批次清掉（對錯人、改歸屬時用），回傳清了幾列。"""
        updates = self._release_updates(batch, ids)
        self.sheets.update(updates)
        return len(updates) // 2

    def _release_updates(self, batch: str, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        data = self._read()
        updates = []
        for r in self._rows(data):
            if r["id"] in ids and r["batch"] == batch:
                col = data[r["tab"]]["col"]
                updates += [{"range": f"'{r['tab']}'!{col_letter(col['price'])}{r['row']}", "values": [[""]]},
                            {"range": f"'{r['tab']}'!{col_letter(col['batch'])}{r['row']}", "values": [[""]]}]
        return updates

    # ------------------------------------------------------------ 結算紀錄
    def _ledger_updates(self, batch: str, block: list[list], old=None) -> list[dict]:
        if old is None:
            old = self.sheets.get([f"'{LEDGER}'!A2:L"])[0]
        first = next((i for i, r in enumerate(old) if r and str(r[0]) == batch), None)
        rest = [r for r in old if r and str(r[0]) != batch]
        at = len(rest) if first is None else len([r for r in old[:first] if r and str(r[0]) != batch])
        new = rest[:at] + block + rest[at:]
        width = len(LEDGER_HEADER)
        new += [[] for _ in range(max(0, len(old) - len(new)))]
        values = [list(r[:width]) + [""] * max(0, width - len(r)) for r in new]
        return [{"range": f"'{LEDGER}'!A2:L{len(values) + 1}", "values": values}] if values else []

    def write_ledger(self, batch: str, block: list[list]):
        """替換這個批次的紀錄，尾端清空和新內容在同一次寫入完成。"""
        self.sheets.update(self._ledger_updates(batch, block))

    def _state_updates(self, batch: str, state: dict, old=None) -> list[dict]:
        if "batch" in state and str(state["batch"]) != str(batch):
            raise SheetsError("結算狀態的批次和儲存目標不同，請先核對結算資料")
        text = json.dumps(state, ensure_ascii=False)
        chunks = [text[i:i + CELL_LIMIT] for i in range(0, len(text), CELL_LIMIT)]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block = [[batch, stamp] + chunks[i:i + 24] for i in range(0, len(chunks), 24)]
        if old is None:
            old = self.sheets.get([f"'{STATE}'!A2:Z"])[0]
        first = next((i for i, r in enumerate(old) if r and str(r[0]) == batch), None)
        rest = [r for r in old if r and str(r[0]) != batch]
        at = len(rest) if first is None else len([r for r in old[:first] if r and str(r[0]) != batch])
        new = rest[:at] + block + rest[at:]
        new += [[] for _ in range(max(0, len(old) - len(new)))]
        values = [list(r) + [""] * (26 - len(r)) for r in new]
        return [{"range": f"'{STATE}'!A2:Z{len(values) + 1}", "values": values}]

    def write_state(self, batch: str, state: dict):
        """把 bot 的完整結算狀態存在隱藏分頁，主機壞了可以從這裡還原。"""
        self.sheets.update(self._state_updates(batch, state))

    def save_settlement(self, batch: str, state: dict, block: list[list], release_ids=()):
        """以同一次 values.batchUpdate 更新公開帳本與可還原的完整狀態。"""
        old_ledger, old_state = self.sheets.get([f"'{LEDGER}'!A2:L", f"'{STATE}'!A2:Z"])
        updates = self._ledger_updates(batch, block, old_ledger) + self._state_updates(batch, state, old_state)
        updates += self._release_updates(batch, release_ids)
        self.sheets.update(updates)

    def read_states(self) -> dict:
        parts = {}
        for r in self.sheets.get([f"'{STATE}'!A2:Z"])[0]:
            if r and r[0]:
                parts.setdefault(str(r[0]), []).extend(str(c) for c in r[2:])
        out = {}
        for batch, chunks in parts.items():
            try:
                state = json.loads("".join(chunks))
                if not isinstance(state, dict):
                    raise ValueError("state must be an object")
                if "batch" in state and str(state["batch"]) != batch:
                    raise ValueError("state batch does not match its storage key")
                out[batch] = state
            except (json.JSONDecodeError, ValueError) as error:
                raise SheetsError(f"表格裡結算 {batch} 的狀態讀不回來，請先核對隱藏分頁") from error
        return out
