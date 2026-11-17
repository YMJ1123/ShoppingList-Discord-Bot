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

console.log('\n指定列：要和選項當時的樣子對得上才刪');
const pick = (n, sheet, r, list = rows) => findDeleteTarget(
  { rowNumber: n, sheet, expect: { itemName: r.name, spec: r.spec, qty: r.qty, url: r.url } }, list);
const usb = rows[1], blackFan = rows[2], brush = rows[3];
check('列號沒變、內容也對 → 刪那一列', pick(5, 'TB', usb), { found: true, sheet: 'TB', row_number: 5, itemName: 'USB風扇' });
check('選項要帶網址，之後才認得出是哪一筆', fans.choices.map((c) => c.url), [rows[0].url, rows[1].url, rows[2].url]);

// 2026-11-17 實測：等使用者選的時候上面有一列被刪，下面的列號全部往上移一格
const shifted = [
  row('TB', 4, 'USB風扇', usb.url),
  row('TB', 7, '小米風扇', blackFan.url, '黑色', 2),
  row('TB', 8, '驗證牙刷', 'https://item.taobao.com/item.htm?id=444444444444'),
];
check('列號位移後，原本的列號現在是別的商品 → 不能刪它，要刪搬到新位置的那一筆',
  pick(8, 'TB', blackFan, shifted), { found: true, sheet: 'TB', row_number: 7, itemName: '小米風扇' });
check('選的那筆已經不在了 → 回 changed，什麼都不刪',
  pick(8, 'TB', blackFan, [shifted[0], shifted[2]]), { found: false, status: 'changed' });
const twins = [row('TB', 6, '小米風扇', blackFan.url, '黑色', 2), row('TB', 7, '小米風扇', blackFan.url, '黑色', 2), shifted[2]];
check('位移後有兩筆一模一樣、分不出是哪一筆 → 回 changed', pick(8, 'TB', blackFan, twins), { found: false, status: 'changed' });
check('兩筆一模一樣但列號沒變 → 刪選的那一列', pick(7, 'TB', blackFan, twins).row_number, 7);
check('列已經不在、也找不到同樣的 → changed', pick(99, 'TB', usb, [rows[0]]), { found: false, status: 'changed' });
check('分頁對不上不刪另一頁', pick(3, 'TB', brush), { found: false, status: 'changed' });
check('沒帶選項內容就不靠列號刪', findDeleteTarget({ rowNumber: 5, sheet: 'TB' }, rows), { found: false, status: 'changed' });
check('沒有名稱也沒有網址', findDeleteTarget({ itemName: '', url: '' }, rows), { found: false });

console.log(`\n${passed} passed`);
