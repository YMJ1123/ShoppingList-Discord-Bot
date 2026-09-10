// workflow/parse_request.js 的單元測試
//   跑法：node workflow/test_parse.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const src = fs.readFileSync(path.join(__dirname, 'parse_request.js'), 'utf8');
const runNode = new Function('$input', src);

// 模擬 n8n webhook 進來的一個 item
const run = (payload) => runNode({ all: () => [{ json: { body: payload } }] })[0].json;

let passed = 0;
const check = (name, fn) => {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
};

console.log('\nparse_request');

check('預設平台是淘寶', () => {
  const j = run({ action: 'add', itemName: '風扇', url: 'https://e.tb.cn/h.a' });
  assert.strictEqual(j.platform, '淘寶');
  assert.strictEqual(j.action, 'add');
  assert.strictEqual(j.itemName, '風扇');
});

check('jd.com 判成京東', () => {
  assert.strictEqual(run({ url: 'https://item.jd.com/100012.html' }).platform, '京東');
});

check('3.cn 短連結判成京東', () => {
  assert.strictEqual(run({ url: 'https://3.cn/2v-s8bsu' }).platform, '京東');
});

check('分享文出現「京东」也算京東', () => {
  const j = run({ shareText: '【京东】超值优惠 https://u.jd.com/abc', url: '' });
  assert.strictEqual(j.platform, '京東');
});

check('繁體「京東」也認得', () => {
  assert.strictEqual(run({ shareText: '京東的耳機', url: '' }).platform, '京東');
});

check('沒給 url 時從分享文裡撈', () => {
  const j = run({ shareText: '看這個 https://e.tb.cn/h.zzz 很棒', url: '' });
  assert.strictEqual(j.url, 'https://e.tb.cn/h.zzz');
});

check('action 轉小寫、預設 add', () => {
  assert.strictEqual(run({ action: 'CLOSE' }).action, 'close');
  assert.strictEqual(run({}).action, 'add');
});

check('價格與批次原樣帶過去', () => {
  const j = run({ action: 'price', unitPrice: 120, totalPrice: 0, batch: '20260910-1' });
  assert.strictEqual(j.unitPrice, 120);
  assert.strictEqual(j.totalPrice, 0);
  assert.strictEqual(j.batch, '20260910-1');
});

check('localDate / localTime 由 bot 端提供', () => {
  const j = run({ action: 'close', localDate: '20260910', localTime: '2026-09-10 14:30' });
  assert.strictEqual(j.localDate, '20260910');
  assert.strictEqual(j.localTime, '2026-09-10 14:30');
});

check('欄位前後空白會被清掉', () => {
  const j = run({ itemName: '  電動牙刷  ', senderName: ' amy ' });
  assert.strictEqual(j.itemName, '電動牙刷');
  assert.strictEqual(j.senderName, 'amy');
});

check('payload 直接在 item.json（沒有 body）也能解析', () => {
  const j = runNode({ all: () => [{ json: { action: 'list', senderName: 'bob' } }] })[0].json;
  assert.strictEqual(j.action, 'list');
  assert.strictEqual(j.senderName, 'bob');
});

console.log(`\n全部 ${passed} 項測試通過 ✓\n`);
