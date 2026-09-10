#!/usr/bin/env python3
"""直接打 n8n webhook，端到端驗證整條流程（不需要 Discord）。

用法：
    python3 workflow/smoke_test.py --url http://localhost:5678/webhook/discord-shopping-test

⚠️ 這支會真的寫入 Google Sheet，請指向**測試分頁**的 workflow。
   每次執行都用帶時間戳的購買人名稱，所以不會跟上次的殘留資料互相干擾，
   斷言也只看自己這批。測完把測試分頁的列刪掉即可。
"""
import argparse
import json
import sys
from datetime import datetime

import requests

TIMEOUT = 30


class Failure(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="n8n webhook 的 Production URL")
    ap.add_argument("--yes", action="store_true", help="跳過『看起來像正式環境』的確認")
    args = ap.parse_args()

    # 防手滑：打到正式 webhook 會污染團員在看的表
    if "test" not in args.url.lower() and not args.yes:
        print(f"⚠️  這個 URL 看起來不是測試環境：\n    {args.url}")
        print("   打到正式 workflow 會寫進正式的 Google Sheet（而且結單會凍結所有人的項目）。")
        print("   確定要繼續請加 --yes。")
        return 2

    stamp = datetime.now().strftime("%H%M%S")
    sender = f"smoke-{stamp}"
    item_a = f"煙霧測試A-{stamp}"
    item_b = f"煙霧測試B-{stamp}"
    item_c = f"煙霧測試C-{stamp}"
    now = datetime.now()

    def call(payload: dict) -> dict:
        body = {
            "senderId": "000000000000000000",
            "senderName": sender,
            "channelId": "000000000000000000",
            "createdAt": now.isoformat(),
            "localDate": now.strftime("%Y%m%d"),
            "localTime": now.strftime("%Y-%m-%d %H:%M"),
            **payload,
        }
        resp = requests.post(args.url, json=body, timeout=TIMEOUT)
        if not resp.ok:
            raise Failure(f"HTTP {resp.status_code}：{resp.text[:300]}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise Failure(f"回應不是 JSON：{resp.text[:300]}")
        if not isinstance(data, dict) or "status" not in data:
            raise Failure(f"回應格式不對（少了 status）：{data}")
        return data

    step_no = 0

    def step(title: str, payload: dict, expect_status: str, must_contain=(), must_not_contain=()):
        nonlocal step_no
        step_no += 1
        data = call(payload)
        status = data.get("status", "")
        message = data.get("message", "") or ""

        if status != expect_status:
            raise Failure(
                f"步驟 {step_no}（{title}）status 應為 {expect_status!r}，"
                f"實際 {status!r}\n    訊息：{message[:300]}"
            )
        for token in must_contain:
            if token not in message:
                raise Failure(
                    f"步驟 {step_no}（{title}）訊息裡找不到 {token!r}\n    訊息：{message[:400]}"
                )
        for token in must_not_contain:
            if token in message:
                raise Failure(
                    f"步驟 {step_no}（{title}）訊息裡不該出現 {token!r}\n    訊息：{message[:400]}"
                )

        first_line = message.split("\n")[0][:70]
        print(f"  ✓ {step_no:2}. {title:26} [{status}] {first_line}")
        return message

    print(f"\n煙霧測試　購買人={sender}　→ {args.url}\n")

    try:
        step("新增（淘寶，2 件）", {
            "action": "add", "itemName": item_a, "quantity": 2,
            "modelSpec": "白色", "url": f"https://e.tb.cn/h.smoke-a-{stamp}",
            "shareText": f"買 兩個白色的 {item_a}",
        }, "added", must_contain=["2 件", "白色", item_a])

        step("新增（京東，1 件）", {
            "action": "add", "itemName": item_b, "quantity": 1,
            "url": f"https://item.jd.com/smoke-b-{stamp}.html",
            "shareText": f"買 【京东】{item_b}",
        }, "added", must_contain=[item_b])

        step("新增（等下要刪掉的）", {
            "action": "add", "itemName": item_c, "quantity": 1,
            "url": f"https://e.tb.cn/h.smoke-c-{stamp}",
        }, "added", must_contain=[item_c])

        step("看清單", {"action": "list"}, "list",
             must_contain=[item_a, item_b, item_c, "淘寶", "京東"])

        step("刪除待下單的", {"action": "delete", "itemName": item_c},
             "deleted", must_contain=[item_c])

        step("清單已不含刪掉的", {"action": "list"}, "list",
             must_contain=[item_a, item_b], must_not_contain=[item_c])

        batch = f"{now.strftime('%Y%m%d')}-"
        closed = step("結單", {"action": "close"}, "closed",
                      must_contain=[batch, item_a, item_b, sender])

        # 從結單訊息裡取出實際批次號，後面指定批次分帳要用
        batch_no = ""
        for part in closed.replace("*", " ").split():
            if part.startswith(now.strftime("%Y%m%d") + "-"):
                batch_no = part
                break
        if not batch_no:
            raise Failure(f"結單訊息裡抓不到批次號：{closed[:300]}")

        step("回填單價（100 × 2）", {
            "action": "price", "itemName": item_a, "unitPrice": 100, "totalPrice": 0,
        }, "price_updated", must_contain=["100", "200"])

        step("分帳（B 還沒填價格）", {"action": "bill"}, "bill",
             must_contain=[f"{sender}**：200", "沒有價格", item_b])

        step("回填總價（50）", {
            "action": "price", "itemName": item_b, "unitPrice": 0, "totalPrice": 50,
        }, "price_updated", must_contain=["50"])

        step("分帳（價格補齊）", {"action": "bill"}, "bill",
             must_contain=[f"{sender}**：250"], must_not_contain=["沒有價格"])

        step("指定批次分帳", {"action": "bill", "batch": batch_no}, "bill",
             must_contain=[batch_no, f"{sender}**：250"])

        step("已下單的不給刪", {"action": "delete", "itemName": item_a},
             "already_ordered", must_contain=[item_a, batch_no])

        step("刪不存在的東西", {"action": "delete", "itemName": f"不存在-{stamp}"},
             "not_found")

    except Failure as e:
        print(f"\n❌ 失敗：{e}\n")
        return 1
    except requests.RequestException as e:
        print(f"\n❌ 連不上 webhook：{e}")
        print("   確認 workflow 已經 Active，而且 URL 是 /webhook/... 不是 /webhook-test/...\n")
        return 1

    print(f"\n全部 {step_no} 步通過 ✓　批次 {batch_no}")
    print(f"清理：把測試分頁裡購買人為 {sender} 的列刪掉（或整個測試分頁刪掉）\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
