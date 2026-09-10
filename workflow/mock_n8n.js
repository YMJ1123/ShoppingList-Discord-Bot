// 假的 n8n：用記憶體模擬兩張 Google Sheet，跑真正的 parse_request.js + plan.js。
//
// 用途：在完全不碰 Google Sheet 的情況下端到端驗證流程邏輯，
//       也用來確認 smoke_test.py 的期望值是對的。
//
//   node workflow/mock_n8n.js &
//   python3 workflow/smoke_test.py --url http://localhost:5999/webhook/test
//
// 注意：這裡沒有模擬 n8n 節點本身的行為（Sheets 的欄位對應、憑證、
//       Switch 分流），那部分只有真的匯入 n8n 才驗得到。
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = Number(process.env.PORT || 5999);

const planSrc = fs
  .readFileSync(path.join(__dirname, 'plan.js'), 'utf8')
  .split('// ==== N8N_ENTRY_BELOW ====')[0];
const plan = new Function(planSrc + '\nreturn plan;')();

const parseSrc = fs.readFileSync(path.join(__dirname, 'parse_request.js'), 'utf8');
const runParse = new Function('$input', parseSrc);

// 模擬的表格內容（第 1 列是表頭，所以資料從 row_number = 2 開始）
const sheets = { TB: [], JD: [] };

const allRows = () => [
  ...sheets.TB.map((r) => ({ ...r, _sheet: 'TB' })),
  ...sheets.JD.map((r) => ({ ...r, _sheet: 'JD' })),
];

// Google Sheets 刪一列之後，後面的列號會往前遞補——這裡照樣模擬
const renumber = () => {
  for (const key of ['TB', 'JD']) {
    sheets[key].forEach((row, i) => {
      row.row_number = i + 2;
    });
  }
};

function handle(payload) {
  const req = runParse({ all: () => [{ json: { body: payload } }] })[0].json;
  const out = plan(req, allRows());
  const response = out[0].json._response;

  for (const item of out) {
    const write = item.json;
    const target = write._target;
    if (!target || target === 'READ') continue;

    const [sheetKey, op] = target.split('_');
    const sheet = sheets[sheetKey];
    if (!sheet) throw new Error(`未知的 sheet：${target}`);

    if (op === 'APPEND') {
      const row = { ...write };
      delete row._target;
      delete row._response;
      sheet.push(row);
    } else if (op === 'UPDATE') {
      const row = sheet.find((r) => r.row_number === write.row_number);
      if (!row) throw new Error(`UPDATE 找不到列 ${write.row_number}（${sheetKey}）`);
      for (const [k, v] of Object.entries(write)) {
        if (k === '_target' || k === '_response' || k === 'row_number') continue;
        row[k] = v;
      }
    } else if (op === 'DELETE') {
      const idx = sheet.findIndex((r) => r.row_number === write.row_number);
      if (idx < 0) throw new Error(`DELETE 找不到列 ${write.row_number}（${sheetKey}）`);
      sheet.splice(idx, 1);
    } else {
      throw new Error(`未知的操作：${target}`);
    }
  }

  renumber();
  return response;
}

const server = http.createServer((req, res) => {
  // GET /_rows 可以看目前兩張表的內容，方便除錯
  if (req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(sheets, null, 2));
    return;
  }

  let body = '';
  req.on('data', (chunk) => (body += chunk));
  req.on('end', () => {
    let response;
    try {
      response = handle(JSON.parse(body || '{}'));
    } catch (e) {
      console.error('[mock] 錯誤:', e.message);
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'error', message: e.message }));
      return;
    }
    console.log(`[mock] ${JSON.parse(body).action || 'add'} -> ${response.status}`);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(response));
  });
});

server.listen(PORT, () => {
  console.log(`假 n8n 已啟動：http://localhost:${PORT}/webhook/test`);
  console.log(`（GET 同一個位址可以看表格內容，Ctrl+C 結束）`);
});
