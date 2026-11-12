// ShoppingListSettle 的 Code 節點。
// list   → 回傳兩個分頁裡還沒結算的列
// settle → 檢查列沒有被動過，產生要寫回表格的儲存格（實際價格、結算批次）
const TABS = ['淘寶', '京東'];
const COLS = {
  name: '預購買商品',
  spec: '商品型號規格',
  url: '商品網址',
  qty: '商品數量',
  price: '實際價格',
  buyer: '購買人',
  batch: '結算批次',
};

const str = (v) => (v === undefined || v === null ? '' : String(v)).trim();

function colLetter(index) {
  let s = '';
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) {
    s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  }
  return s;
}

function readTab(valueRange, tab) {
  const values = (valueRange && valueRange.values) || [];
  const header = (values[0] || []).map(str);
  const col = {};
  for (const [key, title] of Object.entries(COLS)) col[key] = header.indexOf(title);

  let needHeader = false;
  if (col.batch < 0) {
    // 放在所有資料右邊的第一個空欄，才不會蓋到沒有標題的雜項資料
    col.batch = values.reduce((width, r) => Math.max(width, r.length), 0);
    needHeader = true;
  }

  const rows = [];
  for (let r = 1; r < values.length; r++) {
    const v = values[r] || [];
    const name = str(v[col.name]);
    if (!name) continue;
    const qty = Number(v[col.qty]);
    rows.push({
      tab,
      row: r + 1,
      name,
      spec: str(v[col.spec]),
      url: str(v[col.url]),
      qty: qty > 0 ? qty : 1,
      buyer: str(v[col.buyer]),
      batch: str(v[col.batch]),
    });
  }
  return { tab, col, needHeader, rows };
}

function plan(body, valueRanges) {
  const tabs = {};
  TABS.forEach((tab, i) => { tabs[tab] = readTab(valueRanges[i], tab); });

  for (const t of Object.values(tabs)) {
    for (const key of ['name', 'qty', 'price', 'buyer']) {
      if (t.col[key] < 0) {
        return { _status: 500, _write: null, _response: { status: 'error', message: `分頁「${t.tab}」找不到欄位「${COLS[key]}」` } };
      }
    }
  }

  if (body.action === 'list') {
    const all = Object.values(tabs).flatMap((t) => t.rows);
    const open = all.filter((r) => !r.batch).map(({ batch, ...rest }) => rest);
    return { _status: 200, _write: null, _response: { status: 'ok', rows: open, settled: all.length - open.length } };
  }

  if (body.action === 'settle') {
    const batch = str(body.batch);
    const wanted = Array.isArray(body.rows) ? body.rows : [];
    if (!batch || wanted.length === 0) {
      return { _status: 400, _write: null, _response: { status: 'error', message: '缺少 batch 或 rows' } };
    }

    const problems = [];
    const data = [];
    const headerTabs = new Set();
    const seen = new Set();
    for (const w of wanted) {
      const t = tabs[w.tab];
      const cur = t && t.rows.find((r) => r.row === Number(w.row));
      const id = `${w.tab}${w.row}`;
      let reason = '';
      if (!t) reason = '沒有這個分頁';
      else if (seen.has(id)) reason = '同一列重複出現';
      else if (!cur) reason = '這一列已經不存在';
      else if (cur.name !== str(w.name) || cur.buyer !== str(w.buyer)) reason = '這一列的內容已經變了（可能有人新增或刪除過商品）';
      else if (cur.batch) reason = `已經在批次 ${cur.batch} 結算過`;
      seen.add(id);
      if (reason) { problems.push({ tab: w.tab, row: w.row, reason }); continue; }

      const price = w.price === null || w.price === undefined || w.price === '' ? null : Number(w.price);
      if (price !== null && !(price >= 0)) { problems.push({ tab: w.tab, row: w.row, reason: '價格不是數字' }); continue; }
      if (price !== null) data.push({ range: `'${w.tab}'!${colLetter(t.col.price)}${cur.row}`, values: [[price]] });
      data.push({ range: `'${w.tab}'!${colLetter(t.col.batch)}${cur.row}`, values: [[batch]] });
      if (t.needHeader) headerTabs.add(w.tab);
    }

    if (problems.length) {
      return { _status: 409, _write: null, _response: { status: 'conflict', problems } };
    }
    for (const tab of headerTabs) {
      data.unshift({ range: `'${tab}'!${colLetter(tabs[tab].col.batch)}1`, values: [[COLS.batch]] });
    }
    return { _status: 200, _write: { valueInputOption: 'RAW', data }, _response: { status: 'settled', batch, count: wanted.length } };
  }

  return { _status: 400, _write: null, _response: { status: 'error', message: `不認得的 action：${str(body.action)}` } };
}

if (typeof module !== 'undefined' && module.exports) module.exports = { plan, colLetter, readTab };
