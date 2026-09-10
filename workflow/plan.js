// ============================================================
//  n8n Code 節點：Plan
//  購物清單的全部決策邏輯都集中在這裡。
//
//  輸入：Parse Request 的請求 + Load TB / Load JD 撈出來的所有列
//  輸出：每個 item 帶 _target，由 Switch Target 分流到對應的 Sheets 節點
//        TB_APPEND / JD_APPEND / TB_UPDATE / JD_UPDATE / TB_DELETE / JD_DELETE / READ
//        每個 item 都帶同一份 _response，Respond 節點固定取 first()。
//
//  ⚠️ 這個檔案是唯一真相：改完請跑 `python3 workflow/build.py` 重新生成
//     ShoppingListBot.json，不要直接編輯 JSON 裡的程式碼。
// ============================================================

const STATUS_PENDING = '待下單';
const STATUS_ORDERED = '已下單';

const COL = {
  item: '預購買商品',
  spec: '商品型號規格',
  url: '商品網址',
  unit: '商品價格',
  qty: '商品數量',
  est: '預估價格',
  actual: '實際價格',
  buyer: '購買人',
  status: '狀態',
  batch: '批次',
  orderedAt: '下單時間',
  orderedBy: '下單人',
};

const SHEETS = [
  ['TB', '淘寶'],
  ['JD', '京東'],
];

const text = (v) => (v ?? '').toString().trim();
const norm = (v) => text(v).toLowerCase();
const num = (v) => {
  const n = parseFloat(text(v).replace(/[^\d.-]/g, ''));
  return Number.isFinite(n) ? n : 0;
};
const money = (n) => (Math.round(n * 100) / 100).toString();

// 舊資料的狀態欄是空的 → 一律視為待下單，既有資料才能無痛接軌
const isPending = (r) => {
  const s = text(r[COL.status]);
  return s === '' || s === STATUS_PENDING;
};

const qtyOf = (r) => {
  const q = Math.round(num(r[COL.qty]));
  return q > 0 ? q : 1;
};

// 從商品網址取出一個穩定的比對用鍵。
//
// 淘寶／天貓／京東的網址帶一大堆會變的追蹤參數（spm / utparam / mi_id / scm /
// pvid / abbucket…），同一個商品從搜尋頁複製和從首頁推薦複製就完全不一樣，
// 所以整串網址不能拿來比對，只能認商品 ID。
// 這也順便救了已經存成壞網址的舊資料——ID 還在裡面。
function urlKey(raw) {
  const u = text(raw);
  if (!u) return '';

  // 淘寶／天貓：?id= 或 &id=（注意不能誤中 skuId= / mi_id=）
  const tb = u.match(/[?&]id=(\d{6,})/);
  if (tb) return 'item:' + tb[1];

  // 京東：item.jd.com/<商品ID>.html
  const jd = u.match(/item\.jd\.com\/(\d{6,})/);
  if (jd) return 'item:' + jd[1];

  // 抓不到 ID（例如 e.tb.cn / 3.cn 短連結）就退回去掉 query string 的網址
  return u.split('?')[0].toLowerCase().replace(/\/+$/, '');
}

function plan(req, rows) {
  // 只回話、不動表格
  const reply = (status, message) => [
    { json: { _target: 'READ', _response: { status, message } } },
  ];

  // 要寫表格：每個 item 都帶同一份回應，Respond 取哪個都一樣
  const writes = (list, status, message) =>
    list.map((w) => ({ json: { ...w, _response: { status, message } } }));

  // 找一列：網址優先，再用商品名稱（完全相同 → 互相包含）
  const findRow = (scope) => {
    const key = urlKey(req.url);
    if (key) {
      const hit = scope.find((r) => urlKey(r[COL.url]) === key);
      if (hit) return hit;
    }
    const q = norm(req.itemName);
    if (!q) return null;
    return (
      scope.find((r) => norm(r[COL.item]) === q) ||
      scope.find((r) => norm(r[COL.item]).includes(q)) ||
      scope.find((r) => {
        const n = norm(r[COL.item]);
        return n && q.includes(n);
      }) ||
      null
    );
  };

  // 更新一列時，沒有要改的欄位一定要填回原值，否則 Sheets 會把它清空
  const carry = (r, changes) => ({
    _target: r._sheet + '_UPDATE',
    row_number: r.row_number,
    [COL.unit]: text(r[COL.unit]),
    [COL.est]: text(r[COL.est]),
    [COL.actual]: text(r[COL.actual]),
    [COL.status]: text(r[COL.status]) || STATUS_PENDING,
    [COL.batch]: text(r[COL.batch]),
    [COL.orderedAt]: text(r[COL.orderedAt]),
    [COL.orderedBy]: text(r[COL.orderedBy]),
    ...changes,
  });

  const describe = (r) => {
    const spec = text(r[COL.spec]);
    return `${text(r[COL.item])}${spec ? `（${spec}）` : ''} ×${qtyOf(r)} — ${text(r[COL.buyer])}`;
  };

  // ---------------- add：加進清單 ----------------
  if (req.action === 'add') {
    const itemName = text(req.itemName);
    if (!itemName) return reply('bad_request', '沒有商品名稱，沒辦法加入清單 QQ');

    const qty = qtyOf({ [COL.qty]: req.quantity });
    const spec = text(req.modelSpec);
    const row = {
      _target: req.platform === '京東' ? 'JD_APPEND' : 'TB_APPEND',
      [COL.item]: itemName,
      [COL.spec]: spec,
      [COL.url]: text(req.url),
      [COL.unit]: '',
      [COL.qty]: qty,
      [COL.est]: '',
      [COL.actual]: '',
      [COL.buyer]: text(req.senderName),
      [COL.status]: STATUS_PENDING,
      [COL.batch]: '',
      [COL.orderedAt]: '',
      [COL.orderedBy]: '',
    };

    const parts = [`${qty} 件`];
    if (spec) parts.push(spec);
    parts.push(`「${itemName}」`);
    return writes([row], 'added', `已把 ${parts.join(' ')} 加入購物清單 🧾`);
  }

  // ---------------- delete：只能刪自己的、且還沒下單的 ----------------
  if (req.action === 'delete') {
    const mine = rows.filter((r) => norm(r[COL.buyer]) === norm(req.senderName));

    const hit = findRow(mine.filter(isPending));
    if (hit) {
      return writes(
        [{ _target: hit._sheet + '_DELETE', row_number: hit.row_number }],
        'deleted',
        `已把「${text(hit[COL.item])}」從購物清單刪掉 🗑️`
      );
    }

    // 已經下單的不給刪，否則帳會對不起來
    const ordered = findRow(mine.filter((r) => !isPending(r)));
    if (ordered) {
      const b = text(ordered[COL.batch]);
      return reply(
        'already_ordered',
        `「${text(ordered[COL.item])}」已經在批次 ${b || '(未標記)'} 下單了，不能刪掉 🙅\n` +
          '要取消請直接跟結單的人說。'
      );
    }

    return reply(
      'not_found',
      '在你的待下單清單裡找不到這筆商品 🙅\n（只能刪自己加的，名稱也要對得上喔）'
    );
  }

  // ---------------- list：看目前待下單的 ----------------
  if (req.action === 'list') {
    const pending = rows.filter(isPending);
    if (!pending.length) return reply('empty', '目前待下單清單是空的 🧺');

    const lines = [`🧺 待下單清單（共 ${pending.length} 項）`];
    for (const [sheet, label] of SHEETS) {
      const g = pending.filter((r) => r._sheet === sheet);
      if (!g.length) continue;
      lines.push('', `**${label}**（${g.length} 項）`);
      for (const r of g) lines.push(`• ${describe(r)}`);
    }
    return reply('list', lines.join('\n'));
  }

  // ---------------- close：結單，把待下單的整批凍結 ----------------
  if (req.action === 'close') {
    const pending = rows.filter(isPending);
    if (!pending.length) return reply('empty', '目前沒有待下單的商品，不用結單 👌');

    // 批次號 YYYYMMDD-N：日期由 bot 端給（時區才會對），N 是當天第幾批
    const day = text(req.localDate) || '00000000';
    let maxSeq = 0;
    for (const r of rows) {
      const m = text(r[COL.batch]).match(/^(\d{8})-(\d+)$/);
      if (m && m[1] === day) maxSeq = Math.max(maxSeq, parseInt(m[2], 10));
    }
    const batch = `${day}-${maxSeq + 1}`;

    const updates = pending.map((r) =>
      carry(r, {
        [COL.status]: STATUS_ORDERED,
        [COL.batch]: batch,
        [COL.orderedAt]: text(req.localTime),
        [COL.orderedBy]: text(req.senderName),
      })
    );

    const buyers = [...new Set(pending.map((r) => text(r[COL.buyer])).filter(Boolean))];
    const lines = [
      `✅ 結單完成，批次 **${batch}**`,
      `共 ${pending.length} 項、${buyers.length} 人，由 ${text(req.senderName)} 結單`,
    ];
    for (const [sheet, label] of SHEETS) {
      const g = pending.filter((r) => r._sheet === sheet);
      if (!g.length) continue;
      lines.push('', `**${label}**（${g.length} 項）`);
      for (const r of g) {
        lines.push(`• ${describe(r)}`);
        const u = text(r[COL.url]);
        if (u) lines.push(`  ${u}`);
      }
    }
    lines.push(
      '',
      '下單後請回填價格，例如 `價格 電動牙刷 120`（單價）或 `電動牙刷總共 240`，之後打 `分帳` 就能算錢。'
    );
    return writes(updates, 'closed', lines.join('\n'));
  }

  // ---------------- price：回填實際價格 ----------------
  if (req.action === 'price') {
    const unit = num(req.unitPrice);
    const total = num(req.totalPrice);
    if (unit <= 0 && total <= 0) {
      return reply(
        'bad_price',
        '我看不出價格是多少 QQ\n請試試：`價格 電動牙刷 120`（單價）或 `電動牙刷總共 240`。'
      );
    }

    // 價格通常是下單後才知道，所以先在已下單的裡面找，找不到再退回待下單
    const hit =
      findRow(rows.filter((r) => !isPending(r))) || findRow(rows.filter(isPending));
    if (!hit) {
      return reply('not_found', '清單裡找不到這個商品 QQ\n可以先打 `清單` 看看目前有什麼。');
    }

    const qty = qtyOf(hit);
    const unitPrice = unit > 0 ? unit : total / qty;
    const actual = unit > 0 ? unit * qty : total;

    const update = carry(hit, {
      [COL.unit]: money(unitPrice),
      [COL.actual]: money(actual),
    });

    return writes(
      [update],
      'price_updated',
      `已記錄「${text(hit[COL.item])}」單價 ${money(unitPrice)} × ${qty} = **${money(actual)}**` +
        `（${text(hit[COL.buyer])}）`
    );
  }

  // ---------------- bill：依人分帳 ----------------
  if (req.action === 'bill') {
    const ordered = rows.filter((r) => !isPending(r));
    if (!ordered.length) return reply('empty', '還沒有任何已下單的商品，結單後再來對帳吧 👌');

    const batches = [
      ...new Set(ordered.map((r) => text(r[COL.batch])).filter(Boolean)),
    ].sort();
    const wanted = text(req.batch) || batches[batches.length - 1] || '';
    const scope = wanted ? ordered.filter((r) => text(r[COL.batch]) === wanted) : ordered;

    if (!scope.length) {
      return reply(
        'not_found',
        `找不到批次 ${wanted} 的紀錄 QQ\n目前有的批次：${batches.join('、') || '(無)'}`
      );
    }

    const byBuyer = new Map();
    const missing = [];
    for (const r of scope) {
      const buyer = text(r[COL.buyer]) || '(未填)';
      const amount = num(r[COL.actual]);
      if (amount <= 0) missing.push(`${text(r[COL.item])}（${buyer}）`);
      byBuyer.set(buyer, (byBuyer.get(buyer) || 0) + amount);
    }

    const lines = [`💰 批次 **${wanted || '全部'}** 分帳（${scope.length} 項）`, ''];
    let grand = 0;
    for (const [buyer, amt] of [...byBuyer].sort((a, b) => b[1] - a[1])) {
      grand += amt;
      lines.push(`• **${buyer}**：${money(amt)}`);
    }
    lines.push('', `合計：**${money(grand)}**`);

    // 沒填價格的一定要講，不然分帳金額是錯的卻看不出來
    if (missing.length) {
      lines.push('', `⚠️ 還有 ${missing.length} 項沒有價格，上面的金額不完整：`);
      for (const m of missing) lines.push(`• ${m}`);
      lines.push('補價格請用：`價格 <商品> <金額>`');
    }
    return reply('bill', lines.join('\n'));
  }

  return reply('unknown_action', `我不知道要做什麼 QQ（action=${text(req.action) || '空'}）`);
}

// ==== N8N_ENTRY_BELOW ====

function loadRows(nodeName, sheet) {
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
    if (seen.has(r.row_number)) continue; // executeOnce 的保險：去掉重複列
    seen.add(r.row_number);
    out.push({ ...r, _sheet: sheet });
  }
  return out;
}

const req = $('Parse Request').first().json;
const rows = [...loadRows('Load TB', 'TB'), ...loadRows('Load JD', 'JD')];
return plan(req, rows);
