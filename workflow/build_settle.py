#!/usr/bin/env python3
"""把 settle_plan.js 包成 n8n workflow：python3 workflow/build_settle.py → ShoppingListSettle.json"""
import json
from pathlib import Path

HERE = Path(__file__).parent
SHEET_ID = "1lHVZo7mhJt_1jA_shrHjrEMjVzFt8JX5NhB3n3Z-jBM"
API = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values"
CRED = {"googleSheetsOAuth2Api": {"id": "TGwFp295v7gk3RIt", "name": "Google Sheets account"}}
AUTH = {"authentication": "predefinedCredentialType", "nodeCredentialType": "googleSheetsOAuth2Api"}

code = (HERE / "settle_plan.js").read_text(encoding="utf-8")
code += "\nreturn [{ json: plan($('Webhook').first().json.body || {}, $input.first().json.valueRanges || []) }];\n"

def node(name, type_, version, x, params, **extra):
    return {"parameters": params, "type": f"n8n-nodes-base.{type_}", "typeVersion": version,
            "position": [x, 0], "id": f"settle-{name.lower().replace(' ', '-')}", "name": name, **extra}

nodes = [
    node("Webhook", "webhook", 2.1, 0,
         {"httpMethod": "POST", "path": "discord-shopping-settle", "responseMode": "responseNode", "options": {}},
         webhookId="5e77c1e0-5e77-4c1e-9d5c-5e771e2026b1"),
    node("Read Sheets", "httpRequest", 4.2, 220,
         {"url": f"={API}:batchGet?ranges={{{{ encodeURIComponent('淘寶!A1:Z') }}}}&ranges={{{{ encodeURIComponent('京東!A1:Z') }}}}"
                 "&valueRenderOption=UNFORMATTED_VALUE", **AUTH, "options": {}},
         credentials=CRED),
    node("Plan", "code", 2, 440, {"jsCode": code}),
    node("If Write", "if", 2.2, 660, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "settle-has-write", "leftValue": "={{ $json._write !== null }}", "rightValue": "",
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}}),
    node("Write Sheets", "httpRequest", 4.2, 880,
         {"method": "POST", "url": f"{API}:batchUpdate", **AUTH, "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify($json._write) }}", "options": {}},
         credentials=CRED),
    node("Respond", "respondToWebhook", 1.4, 1100,
         {"respondWith": "json", "responseBody": "={{ JSON.stringify($('Plan').first().json._response) }}",
          "options": {"responseCode": "={{ $('Plan').first().json._status }}"}}),
]
link = lambda target: [{"node": target, "type": "main", "index": 0}]
workflow = {
    "name": "ShoppingListSettle",
    "nodes": nodes,
    "pinData": {},
    "connections": {
        "Webhook": {"main": [link("Read Sheets")]},
        "Read Sheets": {"main": [link("Plan")]},
        "Plan": {"main": [link("If Write")]},
        "If Write": {"main": [link("Write Sheets"), link("Respond")]},
        "Write Sheets": {"main": [link("Respond")]},
    },
    "active": False,
    "settings": {"executionOrder": "v1"},
    "versionId": "5e771e00-0000-4000-8000-000020261112",
    "id": "SLsettle20261112",
    "tags": [],
}
out = HERE.parent / "ShoppingListSettle.json"
out.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out.name}: {len(nodes)} nodes")
