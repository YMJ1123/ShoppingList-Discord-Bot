// 在「這個人自己加的列」裡找出要刪的那一筆。
// 唯一一筆才刪。同一層有多筆就回 choose，讓使用者選編號，不默默拿第一筆。

function urlKey(raw) {
  const u = (raw ?? '').toString().trim();
  if (!u) return '';
  const tb = u.match(/[?&]id=(\d{6,})/);
  if (tb) return 'item:' + tb[1];
  const jd = u.match(/item\.jd\.com\/(\d{6,})/);
  if (jd) return 'item:' + jd[1];
  return u.split('?')[0].toLowerCase().replace(/\/+$/, '');
}

function findDeleteTarget(req, rows) {
  const norm = (s) => (s ?? '').toString().trim().toLowerCase();
  const one = (row) => ({
    found: true,
    sheet: row.sheet,
    row_number: row.row_number,
    itemName: row.name || '',
  });
  const choose = (list) => ({
    found: false,
    status: 'choose',
    choices: list.map((row, i) => ({
      n: i + 1,
      sheet: row.sheet,
      row_number: row.row_number,
      itemName: row.name || '',
      spec: row.spec || '',
      qty: row.qty ?? '',
    })),
  });

  const picked = req.rowNumber;
  if (picked !== undefined && picked !== null && String(picked) !== '') {
    const row = rows.find((r) => String(r.row_number) === String(picked)
      && (!req.sheet || r.sheet === req.sheet));
    return row ? one(row) : { found: false };
  }

  const key = urlKey(req.url);
  if (key) {
    const hits = rows.filter((r) => urlKey(r.url) === key);
    if (hits.length === 1) return one(hits[0]);
    if (hits.length > 1) return choose(hits);
  }

  const q = norm(req.itemName);
  if (!q) return { found: false };
  const exact = rows.filter((r) => norm(r.name) === q);
  if (exact.length === 1) return one(exact[0]);
  if (exact.length > 1) return choose(exact);
  const fuzzy = rows.filter((r) => {
    const name = norm(r.name);
    return name && (name.includes(q) || q.includes(name));
  });
  if (fuzzy.length === 1) return one(fuzzy[0]);
  if (fuzzy.length > 1) return choose(fuzzy);
  return { found: false };
}

module.exports = { findDeleteTarget, urlKey };
