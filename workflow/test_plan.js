// workflow/plan.js 的單元測試（不碰 n8n、不碰 Google Sheet）
//   跑法：node workflow/test_plan.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

// 取 N8N_ENTRY_BELOW 之前的部分（純邏輯），把 plan() 拉出來測
const src = fs
  .readFileSync(path.join(__dirname, 'plan.js'), 'utf8')
  .split('// ==== N8N_ENTRY_BELOW ====')[0];
const plan = new Function(src + '\nreturn plan;')();

let passed = 0;
const check = (name, fn) => {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
};

// ---------- 測試資料 ----------
const row = (o) => ({
  row_number: o.row,
  預購買商品: o.item,
  商品型號規格: o.spec || '',
  商品網址: o.url || '',
  商品價格: o.unit ?? '',
  商品數量: o.qty ?? 1,
  預估價格: o.est ?? '',
  實際價格: o.actual ?? '',
  購買人: o.buyer,
  狀態: o.status ?? '',
  批次: o.batch || '',
  下單時間: o.orderedAt || '',
  下單人: o.orderedBy || '',
  _sheet: o.sheet || 'TB',
});

const req = (o) => ({
  action: 'add',
  platform: '淘寶',
  url: '',
  itemName: '',
  modelSpec: '',
  quantity: 1,
  unitPrice: 0,
  totalPrice: 0,
  batch: '',
  senderName: 'amy',
  localDate: '20260910',
  localTime: '2026-09-10 14:30',
  ...o,
});

const only = (out) => {
  assert.strictEqual(out.length, 1, `預期 1 個 item，實際 ${out.length}`);
  return out[0].json;
};

// ============================================================
console.log('\nadd');
check('淘寶商品進 TB，狀態預設待下單', () => {
  const j = only(
    plan(req({ action: 'add', itemName: '小米風扇', modelSpec: '白色', quantity: 2, url: 'https://e.tb.cn/h.a' }), [])
  );
  assert.strictEqual(j._target, 'TB_APPEND');
  assert.strictEqual(j['預購買商品'], '小米風扇');
  assert.strictEqual(j['商品數量'], 2);
  assert.strictEqual(j['狀態'], '待下單');
  assert.strictEqual(j['購買人'], 'amy');
  assert.strictEqual(j['批次'], '');
  assert.match(j._response.message, /2 件 白色 「小米風扇」/);
});

check('京東商品進 JD', () => {
  const j = only(plan(req({ action: 'add', itemName: '耳機', platform: '京東' }), []));
  assert.strictEqual(j._target, 'JD_APPEND');
});

check('沒有商品名稱 → 不寫表格', () => {
  const j = only(plan(req({ action: 'add', itemName: '' }), []));
  assert.strictEqual(j._target, 'READ');
  assert.strictEqual(j._response.status, 'bad_request');
});

// ============================================================
console.log('\ndelete');
const deleteRows = [
  row({ row: 2, item: '電動牙刷', url: 'https://e.tb.cn/h.a', buyer: 'amy', status: '待下單' }),
  row({ row: 3, item: '小米風扇', buyer: 'amy', status: '已下單', batch: '20260901-1' }),
  row({ row: 4, item: '保溫瓶', buyer: 'bob', status: '待下單' }),
];

check('刪自己待下單的 → 產生 DELETE', () => {
  const j = only(plan(req({ action: 'delete', itemName: '電動牙刷' }), deleteRows));
  assert.strictEqual(j._target, 'TB_DELETE');
  assert.strictEqual(j.row_number, 2);
  assert.strictEqual(j._response.status, 'deleted');
});

check('用網址刪也找得到', () => {
  const j = only(plan(req({ action: 'delete', url: 'https://e.tb.cn/h.a' }), deleteRows));
  assert.strictEqual(j.row_number, 2);
});

check('已下單的不給刪', () => {
  const j = only(plan(req({ action: 'delete', itemName: '小米風扇' }), deleteRows));
  assert.strictEqual(j._target, 'READ');
  assert.strictEqual(j._response.status, 'already_ordered');
  assert.match(j._response.message, /20260901-1/);
});

check('別人的商品刪不到', () => {
  const j = only(plan(req({ action: 'delete', itemName: '保溫瓶' }), deleteRows));
  assert.strictEqual(j._response.status, 'not_found');
});

check('JD 的列產生 JD_DELETE', () => {
  const rows = [row({ row: 5, item: '耳機', buyer: 'amy', status: '待下單', sheet: 'JD' })];
  const j = only(plan(req({ action: 'delete', itemName: '耳機' }), rows));
  assert.strictEqual(j._target, 'JD_DELETE');
});

// ============================================================
console.log('\nclose');
const closeRows = [
  row({ row: 2, item: '電動牙刷', buyer: 'amy', qty: 1, unit: '99', est: '99', status: '待下單' }),
  row({ row: 3, item: '保溫瓶', buyer: 'bob', qty: 2, status: '待下單' }),
  row({ row: 4, item: '耳機', buyer: 'amy', qty: 1, status: '待下單', sheet: 'JD' }),
  row({ row: 5, item: '舊貨', buyer: 'bob', status: '已下單', batch: '20260901-1' }),
];

check('把所有待下單的列更新，已下單的不動', () => {
  const out = plan(req({ action: 'close', senderName: 'leader' }), closeRows);
  assert.strictEqual(out.length, 3, '應該只更新 3 筆待下單');
  const targets = out.map((o) => o.json._target);
  assert.deepStrictEqual(targets, ['TB_UPDATE', 'TB_UPDATE', 'JD_UPDATE']);
  for (const o of out) {
    assert.strictEqual(o.json['狀態'], '已下單');
    assert.strictEqual(o.json['批次'], '20260910-1');
    assert.strictEqual(o.json['下單人'], 'leader');
    assert.strictEqual(o.json['下單時間'], '2026-09-10 14:30');
  }
  assert.ok(!out.some((o) => o.json.row_number === 5), '已下單的列不該被更新');
});

check('更新時保留原有價格欄（不會被清空）', () => {
  const out = plan(req({ action: 'close' }), closeRows);
  const brush = out.find((o) => o.json.row_number === 2).json;
  assert.strictEqual(brush['商品價格'], '99');
  assert.strictEqual(brush['預估價格'], '99');
});

check('同一天第二次結單，流水號遞增', () => {
  const rows = [
    row({ row: 2, item: 'A', buyer: 'amy', status: '已下單', batch: '20260910-1' }),
    row({ row: 3, item: 'B', buyer: 'amy', status: '已下單', batch: '20260910-2' }),
    row({ row: 4, item: 'C', buyer: 'amy', status: '待下單' }),
  ];
  const out = plan(req({ action: 'close' }), rows);
  assert.strictEqual(out[0].json['批次'], '20260910-3');
});

check('別的日期的批次號不影響今天的流水號', () => {
  const rows = [
    row({ row: 2, item: 'A', buyer: 'amy', status: '已下單', batch: '20260901-7' }),
    row({ row: 3, item: 'C', buyer: 'amy', status: '待下單' }),
  ];
  const out = plan(req({ action: 'close' }), rows);
  assert.strictEqual(out[0].json['批次'], '20260910-1');
});

check('沒有待下單的東西 → 不動表格', () => {
  const rows = [row({ row: 2, item: 'A', buyer: 'amy', status: '已下單', batch: '20260901-1' })];
  const j = only(plan(req({ action: 'close' }), rows));
  assert.strictEqual(j._target, 'READ');
  assert.strictEqual(j._response.status, 'empty');
});

check('摘要含批次、項數、人數與網址', () => {
  const rows = [
    row({ row: 2, item: '電動牙刷', buyer: 'amy', url: 'https://e.tb.cn/h.a', status: '待下單' }),
    row({ row: 3, item: '保溫瓶', buyer: 'bob', status: '待下單' }),
  ];
  const msg = plan(req({ action: 'close', senderName: 'leader' }), rows)[0].json._response.message;
  assert.match(msg, /批次 \*\*20260910-1\*\*/);
  assert.match(msg, /共 2 項、2 人/);
  assert.match(msg, /由 leader 結單/);
  assert.match(msg, /https:\/\/e\.tb\.cn\/h\.a/);
});

// ============================================================
console.log('\nprice');
const priceRows = [
  row({ row: 2, item: '電動牙刷', buyer: 'amy', qty: 2, status: '已下單', batch: '20260910-1' }),
  row({ row: 3, item: '保溫瓶', buyer: 'bob', qty: 3, status: '待下單' }),
];

check('給單價 → 實際價格 = 單價 × 數量', () => {
  const j = only(plan(req({ action: 'price', itemName: '電動牙刷', unitPrice: 120 }), priceRows));
  assert.strictEqual(j._target, 'TB_UPDATE');
  assert.strictEqual(j.row_number, 2);
  assert.strictEqual(j['商品價格'], '120');
  assert.strictEqual(j['實際價格'], '240');
  assert.strictEqual(j['狀態'], '已下單', '改價格不該動到狀態');
  assert.strictEqual(j['批次'], '20260910-1', '改價格不該動到批次');
});

check('只給總價 → 反算單價', () => {
  const j = only(plan(req({ action: 'price', itemName: '保溫瓶', totalPrice: 300 }), priceRows));
  assert.strictEqual(j['商品價格'], '100');
  assert.strictEqual(j['實際價格'], '300');
  assert.strictEqual(j['狀態'], '待下單', '還沒結單的維持待下單');
});

check('單價無法整除也算得出來', () => {
  const j = only(plan(req({ action: 'price', itemName: '保溫瓶', totalPrice: 100 }), priceRows));
  assert.strictEqual(j['商品價格'], '33.33');
  assert.strictEqual(j['實際價格'], '100');
});

check('沒給價格 → 提示格式', () => {
  const j = only(plan(req({ action: 'price', itemName: '電動牙刷' }), priceRows));
  assert.strictEqual(j._response.status, 'bad_price');
});

check('找不到商品 → not_found', () => {
  const j = only(plan(req({ action: 'price', itemName: '不存在的東西', unitPrice: 50 }), priceRows));
  assert.strictEqual(j._response.status, 'not_found');
});

check('已下單的優先於待下單的同名商品', () => {
  const rows = [
    row({ row: 2, item: '牙刷', buyer: 'amy', qty: 1, status: '待下單' }),
    row({ row: 3, item: '牙刷', buyer: 'bob', qty: 1, status: '已下單', batch: '20260910-1' }),
  ];
  const j = only(plan(req({ action: 'price', itemName: '牙刷', unitPrice: 10 }), rows));
  assert.strictEqual(j.row_number, 3);
});

// ============================================================
console.log('\nbill');
check('依人加總最新批次，並列出缺價格的項目', () => {
  const rows = [
    row({ row: 2, item: 'A', buyer: 'amy', actual: '240', status: '已下單', batch: '20260910-1' }),
    row({ row: 3, item: 'B', buyer: 'amy', actual: '60', status: '已下單', batch: '20260910-1' }),
    row({ row: 4, item: 'C', buyer: 'bob', actual: '', status: '已下單', batch: '20260910-1' }),
    row({ row: 5, item: '舊的', buyer: 'bob', actual: '999', status: '已下單', batch: '20260901-1' }),
    row({ row: 6, item: '還沒下單', buyer: 'amy', status: '待下單' }),
  ];
  const j = only(plan(req({ action: 'bill' }), rows));
  assert.strictEqual(j._target, 'READ');
  const msg = j._response.message;
  assert.match(msg, /20260910-1/);
  assert.match(msg, /\*\*amy\*\*：300/);
  assert.match(msg, /\*\*bob\*\*：0/);
  assert.match(msg, /合計：\*\*300\*\*/);
  assert.match(msg, /還有 1 項沒有價格/);
  assert.match(msg, /• C（bob）/);
  assert.ok(!msg.includes('999'), '不該把舊批次算進來');
  assert.ok(!msg.includes('還沒下單'), '待下單的不該進分帳');
});

check('可以指定批次', () => {
  const rows = [
    row({ row: 2, item: 'A', buyer: 'amy', actual: '240', status: '已下單', batch: '20260910-1' }),
    row({ row: 3, item: '舊的', buyer: 'bob', actual: '999', status: '已下單', batch: '20260901-1' }),
  ];
  const msg = only(plan(req({ action: 'bill', batch: '20260901-1' }), rows))._response.message;
  assert.match(msg, /\*\*bob\*\*：999/);
  assert.ok(!msg.includes('240'));
});

check('指定不存在的批次 → 列出有哪些批次', () => {
  const rows = [row({ row: 2, item: 'A', buyer: 'amy', actual: '1', status: '已下單', batch: '20260910-1' })];
  const j = only(plan(req({ action: 'bill', batch: '19990101-1' }), rows));
  assert.strictEqual(j._response.status, 'not_found');
  assert.match(j._response.message, /20260910-1/);
});

check('都還沒下單 → empty', () => {
  const rows = [row({ row: 2, item: 'A', buyer: 'amy', status: '待下單' })];
  const j = only(plan(req({ action: 'bill' }), rows));
  assert.strictEqual(j._response.status, 'empty');
});

// ============================================================
console.log('\nlist / 其他');
check('列出待下單，依平台分組', () => {
  const rows = [
    row({ row: 2, item: '電動牙刷', spec: '白色', buyer: 'amy', qty: 2, status: '待下單' }),
    row({ row: 3, item: '耳機', buyer: 'bob', status: '待下單', sheet: 'JD' }),
    row({ row: 4, item: '舊貨', buyer: 'bob', status: '已下單', batch: '20260901-1' }),
  ];
  const msg = only(plan(req({ action: 'list' }), rows))._response.message;
  assert.match(msg, /共 2 項/);
  assert.match(msg, /\*\*淘寶\*\*（1 項）/);
  assert.match(msg, /\*\*京東\*\*（1 項）/);
  assert.match(msg, /電動牙刷（白色） ×2 — amy/);
  assert.ok(!msg.includes('舊貨'), '已下單的不該出現在待下單清單');
});

check('清單空的', () => {
  const j = only(plan(req({ action: 'list' }), []));
  assert.strictEqual(j._response.status, 'empty');
});

check('狀態欄空白的舊資料視為待下單', () => {
  const rows = [row({ row: 2, item: '舊資料', buyer: 'amy', status: '' })];
  const listMsg = only(plan(req({ action: 'list' }), rows))._response.message;
  assert.match(listMsg, /舊資料/);
  const closeOut = plan(req({ action: 'close' }), rows);
  assert.strictEqual(closeOut[0].json['狀態'], '已下單');
  const del = only(plan(req({ action: 'delete', itemName: '舊資料' }), rows));
  assert.strictEqual(del._target, 'TB_DELETE');
});

check('未知 action 不會寫表格', () => {
  const j = only(plan(req({ action: 'wat' }), []));
  assert.strictEqual(j._target, 'READ');
  assert.strictEqual(j._response.status, 'unknown_action');
});

check('每個要寫的 item 都帶同一份回應（Respond 取 first 才安全）', () => {
  const rows = [
    row({ row: 2, item: 'A', buyer: 'amy', status: '待下單' }),
    row({ row: 3, item: 'B', buyer: 'bob', status: '待下單', sheet: 'JD' }),
  ];
  const out = plan(req({ action: 'close' }), rows);
  const first = JSON.stringify(out[0].json._response);
  for (const o of out) assert.strictEqual(JSON.stringify(o.json._response), first);
});

console.log(`\n全部 ${passed} 項測試通過 ✓\n`);
