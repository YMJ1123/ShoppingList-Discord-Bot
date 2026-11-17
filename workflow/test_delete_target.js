// node workflow/test_delete_target.js
const assert = require('assert');
const { findDeleteTarget } = require('./delete_target.js');

let passed = 0;
const check = (name, got, expect) => { assert.deepStrictEqual(got, expect, name); passed++; console.log(`  ✓ ${name}`); };

const row = (sheet, n, name, url, spec = '', qty = 1) => ({ sheet, row_number: n, name, url, spec, qty });
const rows = [
  row('TB', 2, '小米風扇', 'https://item.taobao.com/item.htm?id=111111111111&spm=a', '白色'),
  row('TB', 5, 'USB風扇', 'https://item.taobao.com/item.htm?id=222222222222'),
  row('TB', 8, '小米風扇', 'https://item.taobao.com/item.htm?id=111111111111&utparam=zzz', '黑色', 2),
  row('JD', 3, '電動牙刷', 'https://item.jd.com/333333333333.html'),
];

console.log('\n唯一一筆');
check('名稱完全相同就刪那一筆', findDeleteTarget({ itemName: '電動牙刷' }, rows),
  { found: true, sheet: 'JD', row_number: 3, itemName: '電動牙刷' });
check('同一個商品 ID、追蹤參數不同，只有一筆時照刪',
  findDeleteTarget({ url: 'https://item.jd.com/333333333333.html?spm=1', itemName: '' }, rows).row_number, 3);
check('模糊只剩一筆就刪', findDeleteTarget({ itemName: 'USB' }, rows).row_number, 5);
check('完全相同優先於更長的模糊名稱',
  findDeleteTarget({ itemName: '小米風扇' }, [
    row('TB', 2, '小米風扇', ''),
    row('TB', 4, '小米風扇套', ''),
  ]).row_number, 2);

console.log('\n多筆要選，不拿第一筆');
const fans = findDeleteTarget({ itemName: '風扇' }, rows);
check('兩個模糊名稱回 choose', fans.status, 'choose');
check('不把第一筆當成找到', fans.found, false);
check('編號從 1 開始', fans.choices.map((c) => [c.n, c.row_number, c.itemName]),
  [[1, 2, '小米風扇'], [2, 5, 'USB風扇'], [3, 8, '小米風扇']]);
const dup = findDeleteTarget({ url: 'https://item.taobao.com/item.htm?id=111111111111', itemName: '小米風扇' }, rows);
check('同一個商品 ID 有兩列就讓人選', dup.choices.map((c) => c.row_number), [2, 8]);
check('兩個完全相同的名稱也要選',
  findDeleteTarget({ itemName: '小米風扇' }, rows).choices.map((c) => c.row_number), [2, 8]);

console.log('\n指定列');
check('選了編號就只刪那一列', findDeleteTarget({ rowNumber: 5, sheet: 'TB' }, rows).itemName, 'USB風扇');
check('列已經不在就找不到', findDeleteTarget({ rowNumber: 99, sheet: 'TB' }, rows), { found: false });
check('分頁對不上不刪另一頁', findDeleteTarget({ rowNumber: 3, sheet: 'TB' }, rows), { found: false });
check('沒有名稱也沒有網址', findDeleteTarget({ itemName: '', url: '' }, rows), { found: false });

console.log(`\n${passed} passed`);
