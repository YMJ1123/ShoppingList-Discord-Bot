#!/usr/bin/env python3
"""把 delete_target.js 裝進 ShoppingListBot.json 的 Find Delete Target。"""
import json
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
src = (HERE / "delete_target.js").read_text(encoding="utf-8")
src = src.split("module.exports", 1)[0].rstrip() + "\n"

glue = """
const req = $('Code').first().json;

const collect = (nodeName, sheet) => {
  let items = [];
  try {
    items = $(nodeName).all();
  } catch (e) {
    return [];
  }
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const r = it.json || {};
    if (r.row_number === undefined || r.row_number === null || r.row_number === '') continue;
    if (seen.has(r.row_number)) continue;
    seen.add(r.row_number);
    out.push({
      sheet,
      row_number: r.row_number,
      name: r['預購買商品'] || '',
      spec: r['商品型號規格'] || '',
      qty: r['商品數量'] ?? '',
      url: r['商品網址'] || '',
    });
  }
  return out;
};

const rows = [
  ...collect('Get rows in sheet TB', 'TB'),
  ...collect('Get rows in sheet JD', 'JD'),
];

return [{ json: findDeleteTarget(req, rows) }];
"""

wf_path = ROOT / "ShoppingListBot.json"
wf = json.loads(wf_path.read_text(encoding="utf-8"))
found = False
for node in wf["nodes"]:
    if node.get("name") == "Find Delete Target":
        node["parameters"]["jsCode"] = src + glue
        found = True
    elif node.get("name") == "Code":
        code = node["parameters"]["jsCode"]
        old = "        itemName: (payload.itemName || '').toString().trim(),\n        senderName,"
        new = (
            "        itemName: (payload.itemName || '').toString().trim(),\n"
            "        rowNumber: payload.rowNumber ?? '',\n"
            "        sheet: payload.sheet || '',\n"
            "        senderName,"
        )
        if "rowNumber: payload.rowNumber" not in code:
            if old not in code:
                raise SystemExit("Code 節點裡找不到刪除欄位，沒有寫入 rowNumber")
            code = code.replace(old, new, 1)
        if "expect: payload.expect" not in code:
            anchor = "        sheet: payload.sheet || '',\n"
            if anchor not in code:
                raise SystemExit("Code 節點裡找不到 sheet 欄位，沒有寫入 expect")
            code = code.replace(anchor, anchor + "        expect: payload.expect || null,\n", 1)
        node["parameters"]["jsCode"] = code
    elif node.get("name") == "Respond(404)":
        node["parameters"]["responseBody"] = (
            "={{ JSON.stringify($json.status ? $json : {status: 'not_found'}) }}"
        )
if not found:
    raise SystemExit("找不到 Find Delete Target")
wf_path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("updated ShoppingListBot.json")
