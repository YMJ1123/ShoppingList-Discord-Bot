// node workflow/test_settle_plan.js
const assert = require('assert');
const { plan, colLetter } = require('./settle_plan.js');

let passed = 0;
const check = (name, got, expect) => { assert.deepStrictEqual(got, expect, name); passed++; console.log(`  ✓ ${name}`); };

const HEAD = ['預購買商品', '商品型號規格', '商品網址', '商品價格', '商品數量', '預估價格', '實際價格', '購買人'];
const tb = (extra = []) => ({ values: [
  [...HEAD, ...extra],
  ['風扇', '白色', 'https://e.tb.cn/a', '', 2, '', '', 'amy'],
  [],                                              // 被刪掉的空列
  ['牙刷', '', 'https://e.tb.cn/b', '', '', '', '', 'bob'],
] });
const jd = { values: [HEAD, ['耳機', '灰', 'https://3.cn/c', '', 1, '', 19.9, 'amy']] };

console.log('\ncolLetter');
check('0 → A', colLetter(0), 'A');
check('8 → I', colLetter(8), 'I');
check('26 → AA', colLetter(26), 'AA');

console.log('\nlist');
let r = plan({ action: 'list' }, [tb(), jd]);
check('回 200', r._status, 200);
check('不寫表格', r._write, null);
check('列號跳過空列、數量空白當 1', r._response.rows.map((x) => [x.tab, x.row, x.name, x.qty, x.buyer]),
  [['淘寶', 2, '風扇', 2, 'amy'], ['淘寶', 4, '牙刷', 1, 'bob'], ['京東', 2, '耳機', 1, 'amy']]);

const marked = tb(['結算批次']); marked.values[1][8] = '20261101-1';
r = plan({ action: 'list' }, [marked, jd]);
check('已結算的列不再列出', r._response.rows.map((x) => x.name), ['牙刷', '耳機']);
check('回報已結算筆數', r._response.settled, 1);

console.log('\nsettle');
const want = [{ tab: '淘寶', row: 2, name: '風扇', buyer: 'amy', price: 52.54 }, { tab: '京東', row: 2, name: '耳機', buyer: 'amy', price: null }];
r = plan({ action: 'settle', batch: 'B1', rows: want }, [tb(), jd]);
check('回 200', r._status, 200);
check('沒有結算批次欄時補標題，寫價格和批次；price=null 只寫批次', r._write, { valueInputOption: 'RAW', data: [
  { range: "'京東'!I1", values: [['結算批次']] },
  { range: "'淘寶'!I1", values: [['結算批次']] },
  { range: "'淘寶'!G2", values: [[52.54]] },
  { range: "'淘寶'!I2", values: [['B1']] },
  { range: "'京東'!I2", values: [['B1']] },
] });

const stray = tb(); stray.values[3][9] = '雜項';   // J 欄有沒標題的資料
r = plan({ action: 'settle', batch: 'B1', rows: [want[0]] }, [stray, jd]);
check('新欄位避開沒標題的雜項資料', r._write.data.map((d) => d.range), ["'淘寶'!K1", "'淘寶'!G2", "'淘寶'!K2"]);

r = plan({ action: 'settle', batch: 'B2', rows: [{ tab: '淘寶', row: 4, name: '牙刷', buyer: 'bob', price: 9 }] }, [marked, jd]);
check('已有結算批次欄就不重寫標題', r._write.data.map((d) => d.range), ["'淘寶'!G4", "'淘寶'!I4"]);

r = plan({ action: 'settle', batch: 'B2', rows: [
  { tab: '淘寶', row: 2, name: '風扇', buyer: 'amy', price: 1 },     // 已結算
  { tab: '淘寶', row: 4, name: '牙刷', buyer: 'amy', price: 1 },     // 購買人不對
  { tab: '淘寶', row: 3, name: 'x', buyer: 'amy', price: 1 },        // 空列
  { tab: '蝦皮', row: 2, name: 'x', buyer: 'amy', price: 1 },        // 沒這個分頁
] }, [marked, jd]);
check('有任何一列不對 → 409', r._status, 409);
check('有問題就完全不寫', r._write, null);
check('逐列說明原因', r._response.problems.map((p) => p.reason.slice(0, 6)), ['已經在批次 ', '這一列的內容', '這一列已經不', '沒有這個分頁']);

check('缺 batch → 400', plan({ action: 'settle', rows: want }, [tb(), jd])._status, 400);
check('不認得的 action → 400', plan({ action: 'nope' }, [tb(), jd])._status, 400);
check('缺必要欄位 → 500', plan({ action: 'list' }, [{ values: [['預購買商品']] }, jd])._status, 500);

console.log(`\n全部 ${passed} 項測試通過 ✓\n`);
