// ============================================================
//  n8n Code 節點：Parse Request
//  把 Discord bot 送來的 webhook payload 正規化，並判斷平台。
//  bot 端已用 LLM 解析好 action / itemName / quantity / 價格，這裡只做整理。
//
//  ⚠️ 改完請跑 `python3 workflow/build.py` 重新生成 ShoppingListBot.json。
// ============================================================

const text = (v) => (v ?? '').toString().trim();
const urlRegex = /https?:\/\/\S+/;

const out = [];

for (const item of $input.all()) {
  // Webhook 來的 JSON 可能在 item.json 或 item.json.body
  const p = item.json.body || item.json || {};

  const shareText = text(p.shareText || p.fullText);

  let url = text(p.url);
  if (!url) {
    const m = shareText.match(urlRegex);
    if (m) url = m[0];
  }

  // 平台預設淘寶；文字或網址看得到京東特徵才算京東
  let platform = '淘寶';
  if (/京东|京東/.test(shareText) || (url && (url.includes('jd.com') || url.includes('3.cn')))) {
    platform = '京東';
  }

  out.push({
    json: {
      action: text(p.action || 'add').toLowerCase(),
      platform,
      url,
      itemName: text(p.itemName),
      modelSpec: text(p.modelSpec),
      quantity: p.quantity,
      unitPrice: p.unitPrice,
      totalPrice: p.totalPrice,
      batch: text(p.batch),
      senderId: text(p.senderId),
      senderName: text(p.senderName),
      channelId: text(p.channelId),
      // 日期／時間由 bot 端給，時區才會對（n8n 容器時區不一定是本地）
      localDate: text(p.localDate),
      localTime: text(p.localTime),
      createdAt: p.createdAt || new Date().toISOString(),
    },
  });
}

return out;
