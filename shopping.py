"""新增／刪除的判斷邏輯（純函式，不碰 Discord 和表格）。"""
import re

from opencc import OpenCC

_t2s = OpenCC("t2s").convert


def url_key(raw) -> str:
    """從商品網址取出穩定的比對用鍵。

    淘寶／天貓／京東的網址帶一堆會變的追蹤參數（spm / utparam / mi_id…），同一個商品每次複製都不一樣，
    所以只認商品 ID；抓不到 ID（e.tb.cn / 3.cn 短連結）才退回去掉 query string 的網址。
    """
    u = str(raw or "").strip()
    if not u:
        return ""
    m = re.search(r"[?&]id=(\d{6,})", u) or re.search(r"item\.jd\.com/(\d{6,})", u)
    if m:
        return "item:" + m.group(1)
    return u.split("?")[0].lower().rstrip("/")


def platform(text: str, url: str) -> str:
    """京東的連結或分享文字記到「京東」分頁，其他（含天貓）記到「淘寶」。"""
    return "京東" if ("京东" in (text or "") or "jd.com" in (url or "") or "3.cn" in (url or "")) else "淘寶"


def _norm(s) -> str:
    return _t2s(str(s or "")).strip().lower()


def find_delete_target(url: str, name: str, rows: list[dict]) -> tuple[str, list[dict]]:
    """在「這個人自己登記的列」裡找要刪的。回傳 (狀態, 列)：

    "one"    → 唯一一筆，可以直接刪
    "choose" → 同一層對到好幾筆，要讓他選，不能默默拿第一筆
    "none"   → 找不到
    比對順序：商品 ID → 名稱完全相同 → 名稱互相包含。
    """
    def result(hits):
        return ("one" if len(hits) == 1 else "choose", hits) if hits else None

    key = url_key(url)
    found = result([r for r in rows if url_key(r["url"]) == key]) if key else None
    q = _norm(name)
    if not found and q:
        found = (result([r for r in rows if _norm(r["name"]) == q])
                 or result([r for r in rows if _norm(r["name"]) and (q in _norm(r["name"]) or _norm(r["name"]) in q)]))
    return found or ("none", [])


def find_duplicates(url: str, rows: list[dict]) -> list[dict]:
    """這個人還沒結算的列裡，和這個網址是同一件商品的（給「你已經登記過了」的提醒用）。"""
    key = url_key(url)
    return [r for r in rows if key and not r.get("batch") and url_key(r["url"]) == key]
