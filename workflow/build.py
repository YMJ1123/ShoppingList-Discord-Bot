#!/usr/bin/env python3
"""從 workflow/*.js 生成 ShoppingListBot.json。

Code 節點的程式碼以 workflow/parse_request.js 與 workflow/plan.js 為唯一真相
（JSON 裡的字串很難 review／測試），改完邏輯跑這支就好：

    python3 workflow/build.py

現有 workflow 的 documentId、sheetName、credentials、webhookId 會沿用，
不會動到你在 n8n 裡設好的憑證。
"""
import argparse
import json
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_JSON = REPO / "ShoppingListBot.json"
JS_DIR = REPO / "workflow"

# Google Sheet 欄位（順序即表頭順序）
COLUMNS = [
    "預購買商品",
    "商品型號規格",
    "商品網址",
    "商品價格",
    "商品數量",
    "預估價格",
    "實際價格",
    "購買人",
    "狀態",
    "批次",
    "下單時間",
    "下單人",
]

# 更新一列時會寫入的欄位（其餘欄位不碰，Plan 會把原值填回來）
UPDATE_COLUMNS = ["商品價格", "預估價格", "實際價格", "狀態", "批次", "下單時間", "下單人"]

TARGETS = ["TB_APPEND", "JD_APPEND", "TB_UPDATE", "JD_UPDATE", "TB_DELETE", "JD_DELETE"]


def load_js(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


def sheet_schema(include_row_number: bool = False):
    cols = (["row_number"] if include_row_number else []) + COLUMNS
    return [
        {
            "id": c,
            "displayName": c,
            "required": False,
            "defaultMatch": c == "row_number",
            "display": True,
            "type": "string",
            "canBeUsedToMatch": True,
        }
        for c in cols
    ]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="從 workflow/*.js 生成 ShoppingListBot.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
生成測試版（指向測試分頁、獨立的 webhook path，不會覆蓋正式 workflow）：

  python3 workflow/build.py \\
      --out ShoppingListBot.test.json \\
      --tb-gid 123456789 --jd-gid 987654321 \\
      --webhook-path discord-shopping-test

分頁 gid 在 Google Sheet 點開該分頁後，網址 #gid= 後面那串。
""",
    )
    ap.add_argument("--out", help="輸出檔名（預設就地覆寫 ShoppingListBot.json）")
    ap.add_argument("--tb-gid", help="淘寶分頁的 gid（測試用，改指向別的分頁）")
    ap.add_argument("--jd-gid", help="京東分頁的 gid（測試用，改指向別的分頁）")
    ap.add_argument("--webhook-path", help="webhook path（預設沿用 discord-shopping）")
    ap.add_argument("--name", help="workflow 名稱")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    out_path = Path(args.out) if args.out else WORKFLOW_JSON
    if not out_path.is_absolute():
        out_path = REPO / out_path
    # 輸出到別的檔名＝測試版：拿掉 workflow id 並換 webhookId，
    # 免得匯入時覆蓋掉正式的 workflow、或兩個 webhook 撞在一起
    is_variant = out_path != WORKFLOW_JSON

    old = json.loads(WORKFLOW_JSON.read_text(encoding="utf-8"))

    # 沿用既有設定，不重建憑證。用型別＋sheet 名稱來找，不靠節點名稱，
    # 這樣重複跑 build（節點已改名）也還是找得到。
    sheets_nodes = [
        n for n in old["nodes"] if n["type"] == "n8n-nodes-base.googleSheets"
    ]
    if not sheets_nodes:
        raise SystemExit("現有 JSON 裡找不到 Google Sheets 節點，無法沿用憑證設定")

    document_id = sheets_nodes[0]["parameters"]["documentId"]
    creds = sheets_nodes[0]["credentials"]

    def find_sheet(label: str) -> dict:
        for n in sheets_nodes:
            sheet = n["parameters"].get("sheetName") or {}
            if sheet.get("cachedResultName") == label:
                return sheet
        raise SystemExit(f"現有 JSON 裡找不到 sheet「{label}」的設定")

    def with_gid(sheet: dict, gid: str | None) -> dict:
        """測試時把分頁換成指定 gid（用 id 模式，最明確）。"""
        if not gid:
            return sheet
        return {"__rl": True, "mode": "id", "value": str(gid).strip()}

    sheet_tb = with_gid(find_sheet("淘寶"), args.tb_gid)
    sheet_jd = with_gid(find_sheet("京東"), args.jd_gid)

    webhook_old = next(
        n for n in old["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    )

    def sheets_node(name, position, sheet, operation):
        """operation: append / update / delete"""
        params = {
            "documentId": document_id,
            "sheetName": sheet,
        }
        if operation == "append":
            params["operation"] = "append"
            params["columns"] = {
                "mappingMode": "defineBelow",
                "value": {c: f'={{{{ $json["{c}"] }}}}' for c in COLUMNS},
                "matchingColumns": [],
                "schema": sheet_schema(),
                "attemptToConvertTypes": False,
                "convertFieldsToString": True,
            }
            params["options"] = {}
        elif operation == "update":
            params["operation"] = "update"
            value = {"row_number": "={{ $json.row_number }}"}
            value.update({c: f'={{{{ $json["{c}"] }}}}' for c in UPDATE_COLUMNS})
            params["columns"] = {
                "mappingMode": "defineBelow",
                "value": value,
                "matchingColumns": ["row_number"],
                "schema": sheet_schema(include_row_number=True),
                "attemptToConvertTypes": False,
                "convertFieldsToString": True,
            }
            params["options"] = {}
        elif operation == "delete":
            params["operation"] = "delete"
            params["startIndex"] = "={{ $json.row_number }}"
        else:  # read
            params["options"] = {}

        return {
            "parameters": params,
            "type": "n8n-nodes-base.googleSheets",
            "typeVersion": 4.7,
            "position": position,
            "id": str(uuid.uuid4()),
            "name": name,
            "credentials": creds,
        }

    def code_node(name, position, js):
        return {
            "parameters": {"jsCode": js},
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": position,
            "id": str(uuid.uuid4()),
            "name": name,
        }

    def target_rule(target):
        return {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 2,
                },
                "conditions": [
                    {
                        "id": str(uuid.uuid4()),
                        "leftValue": "={{ $json._target }}",
                        "rightValue": target,
                        "operator": {"type": "string", "operation": "equals"},
                    }
                ],
                "combinator": "and",
            },
            "renameOutput": True,
            "outputKey": target,
        }

    # ---------------- 節點 ----------------
    nodes = [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": args.webhook_path or webhook_old["parameters"].get(
                    "path", "discord-shopping"
                ),
                "responseMode": "responseNode",
                "options": {},
            },
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2.1,
            "position": [-880, 220],
            "id": str(uuid.uuid4()) if is_variant else webhook_old["id"],
            "name": "Webhook",
            "webhookId": str(uuid.uuid4()) if is_variant else webhook_old["webhookId"],
        },
        code_node("Parse Request", [-660, 220], load_js("parse_request.js")),
    ]

    # 撈全表：兩個節點串聯，各自 executeOnce 避免被上游 items 重複觸發
    load_tb = sheets_node("Load TB", [-440, 220], sheet_tb, "read")
    load_jd = sheets_node("Load JD", [-220, 220], sheet_jd, "read")
    for n in (load_tb, load_jd):
        n["alwaysOutputData"] = True  # 空表也要往下走，不然流程會斷在這
        n["executeOnce"] = True
    nodes += [load_tb, load_jd]

    nodes.append(code_node("Plan", [0, 220], load_js("plan.js")))

    nodes.append(
        {
            "parameters": {
                "rules": {"values": [target_rule(t) for t in TARGETS]},
                "options": {"fallbackOutput": "extra", "renameFallbackOutput": "READ"},
            },
            "type": "n8n-nodes-base.switch",
            "typeVersion": 3.2,
            "position": [220, 220],
            "id": str(uuid.uuid4()),
            "name": "Switch Target",
        }
    )

    writers = [
        ("Append TB", sheet_tb, "append", [460, -140]),
        ("Append JD", sheet_jd, "append", [460, -20]),
        ("Update TB", sheet_tb, "update", [460, 100]),
        ("Update JD", sheet_jd, "update", [460, 220]),
        ("Delete TB", sheet_tb, "delete", [460, 340]),
        ("Delete JD", sheet_jd, "delete", [460, 460]),
    ]
    for name, sheet, op, pos in writers:
        nodes.append(sheets_node(name, pos, sheet, op))

    nodes.append(
        {
            "parameters": {
                "respondWith": "json",
                # 回應固定取 Plan 的第一個 item，所有分支回同一份訊息
                "responseBody": "={{ JSON.stringify($('Plan').first().json._response) }}",
                "options": {"responseCode": 200},
            },
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.4,
            "position": [720, 220],
            "id": str(uuid.uuid4()),
            "name": "Respond",
        }
    )

    # ---------------- 連線 ----------------
    def link(src, outputs):
        return {
            "main": [
                [{"node": t, "type": "main", "index": 0} for t in outs] for outs in outputs
            ]
        }

    writer_names = [w[0] for w in writers]
    connections = {
        "Webhook": link("Webhook", [["Parse Request"]]),
        "Parse Request": link("Parse Request", [["Load TB"]]),
        "Load TB": link("Load TB", [["Load JD"]]),
        "Load JD": link("Load JD", [["Plan"]]),
        "Plan": link("Plan", [["Switch Target"]]),
        # 6 個 target output 各接一個寫入節點，第 7 個（fallback READ）直接回應
        "Switch Target": link("Switch Target", [[w] for w in writer_names] + [["Respond"]]),
    }
    # 每個寫入節點做完都連到同一個 Respond；回應內容一樣，先到的生效
    for name in writer_names:
        connections[name] = link(name, [["Respond"]])

    default_name = old.get("name", "ShoppingListBot")
    out = {
        "name": args.name or (f"{default_name} (TEST)" if is_variant else default_name),
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False if is_variant else old.get("active", True),
        "settings": old.get("settings", {"executionOrder": "v1"}),
        "versionId": str(uuid.uuid4()),
        "meta": old.get("meta", {}),
        "tags": old.get("tags", []),
    }
    if not is_variant:
        out["id"] = old.get("id")

    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    names = {n["name"] for n in nodes}
    for src, obj in connections.items():
        assert src in names, f"連線來源不存在：{src}"
        for branch in obj["main"]:
            for t in branch:
                assert t["node"] in names, f"連線目標不存在：{t['node']}（來自 {src}）"

    print(f"已生成 {out_path.name}：{len(nodes)} 個節點、{len(connections)} 組連線")
    if is_variant:
        print(f"  名稱：{out['name']}（匯入後是獨立的 workflow，不會蓋掉正式版）")
        print(f"  webhook path：{nodes[0]['parameters']['path']}")
        print(f"  淘寶分頁：{sheet_tb.get('value')}　京東分頁：{sheet_jd.get('value')}")
        print("  匯入後記得手動 Active，並確認 Google 憑證有掛上")


if __name__ == "__main__":
    main()
