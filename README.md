
# Shopping List Discord Bot

簡易的 Discord 購物清單 bot，會驗證 `~要買` 指令並把資料送到 n8n Webhook，再由 n8n 寫入 Google 試算表。  
A lightweight Discord bot that validates `~要買` commands and forwards them to an n8n webhook, which appends rows to a Google Sheet.

---

## Features / 功能簡介

- 在指定的 Discord 頻道監聽指令（不用 @ bot，直接打指令即可）：
  - `~要買~3~"白色"~【淘宝】... https://link 「商品名稱」`
  - `~要買~2~"25H续航+AI通话 降噪 雅灰色"~【京东】https://link 「商品名稱」`
- 驗證格式：
  - 欄位是否用 `~` 分隔
  - 是否以 `~要買` 開頭
  - `<數量>` 是否為整數
  - 型號規格是否以雙引號 `"` 包起來（內容可以包含空白）
  - 內容是否包含 `http://` 或 `https://` 開頭的連結
- 自動解析出：
  - **預購買商品**：從最後一組 `「……」` 內抓商品名稱
  - **商品型號規格**：從 `"<型號規格 可以有空格>"` 中抓整段
  - **商品網址**：抓文字中第一個 URL
  - **商品數量**
  - **購買人**：Discord username
  - **平台**：依分享文字／網址自動辨識「淘寶 / 京東」
- 透過 n8n：
  - 使用 `Code` 節點解析 payload
  - 使用 `Switch` 節點依 `platform` 分流
  - 將 **淘寶訂單寫入 Sheet「淘寶」**、**京東訂單寫入 Sheet「京東」**

---

## Requirements / 環境需求

- Python 3.10+
- n8n（本機或遠端皆可）
- Google 帳號（用來建立試算表與 API 憑證）
- 一個 Discord Bot：
  - 已建立於 Discord Developer Portal
  - 在 Bot 設定中啟用 **MESSAGE CONTENT INTENT**

Python 套件依 `requirements.txt` 安裝：

```bash
python -m pip install -r requirements.txt
````

---

## Environment Setup / 環境變數設定

本專案使用 `.env` 管理私密資訊。請 **不要** 將 `.env` commit 到 GitHub。

### 1. 建立 `.env`

在專案根目錄下執行：

```bash
cp env.example .env
```

或自行建立 `.env`，內容範例：

```env
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
N8N_WEBHOOK_URL=http://localhost:5678/webhook/discord-shopping
ALLOWED_CHANNEL_IDS=123456789012345678,987654321098765432
```

* `DISCORD_TOKEN`

  * 從 Discord Developer Portal → 你的應用 → Bot → Reset Token 後取得的新 Token
  * **不要** 直接寫死在 `bot.py` 裡
* `N8N_WEBHOOK_URL`

  * n8n Workflow 的 **Production Webhook URL**
  * 形如 `http://<your-n8n-host>/webhook/discord-shopping`
  * 注意：**不是** `/webhook-test/...`
* `ALLOWED_CHANNEL_IDS`

  * 允許 bot 處理的頻道 ID，逗號分隔
  * 可在 Discord 中開啟 Developer Mode 後，右鍵頻道 → Copy Channel ID

### 2. 安裝相依套件

```bash
python -m pip install -r requirements.txt
```

### 3. 啟動 bot

```bash
python bot.py
```

---

## Command Format / 指令格式

### 正確格式

```text
~要買~<數量>~"<型號規格 可以有空格>"~<平台分享文字（含網址與商品名稱）>
~要買~<數量>~<平台分享文字（含網址與商品名稱）>   ← 無規格可省略整個 <"...">
```

範例：

```text
~要買~3~"白色"~【淘宝】7天无理由退货 https://e.tb.cn/h.xxx 「電動牙刷」
~要買~2~"25H续航+AI通话 降噪 雅灰色"~【京东】https://3.cn/2v-s8bsu?... 「漫步者X3Pro真无线降噪蓝牙耳机」
```

Bot 檢查：

* 指令各欄位以 `~` 分隔，避免被多餘空白吞掉
* 以 `~要買` 開頭
* `<數量>` 為整數（`int`）
* 型號規格以 `"` 包住（`"..."`，內容可含空白）
* 文字中至少有一個 `http://` 或 `https://` 開頭的 URL

若使用者打了 `~要買` 但格式不正確，bot 會回覆提示，例如：

```text
格式怪怪的 QQ
請用下面這種格式：
~要買~3~"型號規格 可以有空格"~【淘宝/京东】... https://link 「商品」
```

---

## n8n Workflow Setup / n8n 工作流程設定

整體流程示意：

```text
Discord Bot → n8n Webhook → Code → Switch(platform)
                                       ├─ 淘寶 → Google Sheets (Sheet: 淘寶)
                                       └─ 京東 → Google Sheets (Sheet: 京東)
```

### ⚡ Quick Start：從 repo 匯入現成 workflow / Import the pre-built workflow from the repo

你不需要手動從零建立每個節點，可以直接匯入本 repo 裡的 `ShoppingListBot.json`：

1. 打開 n8n UI。
2. 左上角「Workflows」→ **Import from File**（或右上角三點選單 → Import）。
3. 選取 repo 中的 `ShoppingListBot.json` 檔案。
4. 匯入後：

   * 打開這個 workflow。
   * 在 Webhook node 確認 Path / URL 與你的 `.env` 中 `N8N_WEBHOOK_URL` 一致。
   * 在 Google Sheets nodes 中改成你自己的 Spreadsheet / Sheet 名稱。
   * 在 Google 憑證處選用你建立好的 Credential（或依下文建立）。

如果你想了解每一個節點在做什麼，以下是手動建立的詳細說明。

---

### 0. 準備 Google 試算表與 Sheet

1. 建立一份 Google Spreadsheet，例如：`ShoppingList`。
2. 在裡面建立兩個 sheet：

   * `淘寶`
   * `京東`
3. 每個 sheet 建立欄位（標題列）：

   | 預購買商品 | 商品型號規格 | 商品網址 | 商品價格 | 商品數量 | 預估價格 | 實際價格 | 購買人 |
   | ----- | ------ | ---- | ---- | ---- | ---- | ---- | --- |

> 註：目前範例程式會自動填：預購買商品、商品型號規格、商品網址、商品數量、購買人，其餘價格欄位先留白。

---

### 1. 建立 Webhook Node

1. 在 n8n 內建立一個新的 workflow。

2. 新增節點：`Webhook`。

3. 設定：

   * **HTTP Method**：`POST`
   * **Path**：`discord-shopping`（可自訂，但要與 `.env` 中的 `N8N_WEBHOOK_URL` 尾段一致）
   * **Authentication**：`None`
   * 其他保持預設值

4. 存檔後，在 Webhook node 右側面板中確認 **Production URL** 格式類似：

   ```text
   http://localhost:5678/webhook/discord-shopping
   ```

   將這個 URL 填入 `.env` 的 `N8N_WEBHOOK_URL`。

---

### 2. 建立 Code Node（解析 Discord payload）

1. 新增節點：`Code`。

2. 將 `Webhook → Code` 接起來。

3. 設定：

   * **Mode**：`Run Once for All Items`
   * **Language**：`JavaScript`

4. 填入類似下面的程式（實際以 repo 中版本為準）：

   ```js
   const incomingItems = $input.all();
   const out = [];

   for (const item of incomingItems) {
     // Webhook 預設會把 body 放在 json.body 裡
     const payload = item.json.body || item.json;

     const shareText = payload.shareText || payload.fullText || '';
     const quantityRaw = payload.quantity ?? 1;
     const quantity = parseInt(quantityRaw, 10) || 1;

     const senderName = payload.senderName || '';
     const createdAt = payload.createdAt || new Date().toISOString();

     // 抓第一個網址
     const urlMatch = shareText.match(/https?:\/\/\S+/);
     const url = urlMatch ? urlMatch[0] : '';

     // 判斷平台：預設淘寶，若有「京东」或 jd.com / 3.cn 則視為京東
     let platform = '淘寶';
     if (/京东/.test(shareText) || (url && (url.includes('jd.com') || url.includes('3.cn')))) {
       platform = '京東';
     }

     // 商品名稱：優先抓最後一組「……」中的內容
     let itemName = '';
     const titleMatch = shareText.match(/「([^」]+)」/);
     if (titleMatch) {
       itemName = titleMatch[1].trim();
     } else if (urlMatch) {
       const afterUrl = shareText.slice(urlMatch.index + urlMatch[0].length).trim();
       itemName = afterUrl || shareText;
     } else {
       itemName = shareText;
     }

     // 型號規格：由 bot 解析後傳入
     const modelSpec = payload.modelSpec || '';

     const unitPrice = '';
     const estimatedPrice = '';
     const actualPrice = '';

     out.push({
       json: {
         platform,        // 淘寶 / 京東
         itemName,        // 預購買商品
         modelSpec,       // 商品型號規格
         url,             // 商品網址
         unitPrice,       // 商品價格（暫空）
         quantity,        // 商品數量
         estimatedPrice,  // 預估價格（暫空）
         actualPrice,     // 實際價格（暫空）
         buyer: senderName,
         createdAt,
       },
     });
   }

   return out;
   ```

---

### 3. 建立 Switch Node（依平台分流）

1. 新增節點：`Switch`。
2. 將 `Code → Switch` 接起來。
3. 設定：

   * **Value / Property to evaluate**：
     點 **Add Expression**，填入：

     ```js
     {{$json["platform"]}}
     ```

   * **Data Type**：`String`

   * **Rules**：

     1. Equals `淘寶`
     2. Equals `京東`

這樣：

* Switch Output 0 → 淘寶
* Switch Output 1 → 京東

之後要擴充其他平台（例如蝦皮），只要在 Code 中增加 `platform` 判斷，並在 Switch 中再加一條 Rule 即可。

---

### 4. Google Sheets Nodes（寫入試算表）

#### 4.1 淘寶 Sheet

1. 新增 Google Sheets node（例如命名為 `Append 淘寶`）。
2. 接在 Switch Output 0：`Switch (0) → Append 淘寶`。
3. 設定：

   * **Operation**：`Append row`
   * **Spreadsheet**：選擇剛剛建立的購物清單試算表
   * **Sheet**：`淘寶`
   * **Value Input Mode**：User entered 或 RAW 皆可
   * **Map Each Column Manually**：

     * 預購買商品 → Expression：`{{$json["itemName"]}}`
     * 商品型號規格 → `{{$json["modelSpec"]}}`
     * 商品網址 → `{{$json["url"]}}`
     * 商品價格 → `{{$json["unitPrice"]}}`
     * 商品數量 → `{{$json["quantity"]}}`
     * 預估價格 → `{{$json["estimatedPrice"]}}`
     * 實際價格 → `{{$json["actualPrice"]}}`
     * 購買人 → `{{$json["buyer"]}}`

> 注意：一定要用「Expression」模式，否則字串 `{{$json["itemName"]}}` 會被直接寫入表格。

#### 4.2 京東 Sheet

1. 複製上一個 node（右鍵 `Append 淘寶` → Duplicate），改名為 `Append 京東`。
2. 接在 Switch Output 1：`Switch (1) → Append 京東`。
3. 修改：

   * **Sheet**：改為 `京東`
   * 其餘 Expression mapping 保持相同

---

### 5. 啟用 Workflow

確認連線為：

```text
Webhook → Code → Switch(platform)
                       ├─ 0: Append 淘寶 (Sheet: 淘寶)
                       └─ 1: Append 京東 (Sheet: 京東)
```

然後在 n8n 右上角將 workflow 切換為 **Active**。

---

## n8n Google Sheets Credential Setup / n8n Google 憑證設定

要讓 n8n 寫入 Google Sheets，需要在 n8n 裡設定 Google 憑證。
以下以 **OAuth Client** 的方式為例（使用者帳號登入）。

### A. 建立 Google Cloud 專案與啟用 API

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)。
2. 用你的 Google 帳號登入。
3. 建立專案（Project）：

   * 任意命名，如：`n8n-shopping-list`
4. 在左側選單進入：**APIs & Services → Library（程式庫）**。
5. 搜尋並啟用：

   * **Google Sheets API**
   * **Google Drive API**

---

### B. 設定 OAuth Consent Screen（同意畫面）

1. 左側選單 → **APIs & Services → OAuth consent screen**。
2. User type 選擇 **External**。
3. 填寫基本資訊：

   * App name：`n8n` 或任意名稱
   * User support email：你的 Google 帳號
   * Developer contact information：填入你的 email
4. 其他欄位可以先使用預設設定，保存即可（不需要送 Google 審核，上「測試模式」即可）。

### C. 建立 OAuth Client ID（Web Application）

1. 左側選單 → **APIs & Services → Credentials**。
2. 點擊 **Create Credentials → OAuth client ID**。
3. Application type 選 **Web application**。
4. Name 自行命名，例如 `n8n-gsheets`.
5. **Authorized redirect URIs**：

   * 先暫停一下，到 n8n 開啟一個 **Google Sheets** 節點 → 新增 Credential。

   * 在 Credential 編輯視窗底部會看到一個「Redirect URL」（唯讀）。
     格式類似：

     ```text
     http://<your-n8n-host>/rest/oauth2-credential/callback
     ```

   * 把這個 URL 複製回 Google Cloud，貼進 **Authorized redirect URIs**，然後按 **Create**。
6. 之後會跳出一個視窗顯示：

   * **Client ID**
   * **Client Secret**

請將這兩組資料妥善保存。

---

### D. 在 n8n 建立 Google Sheets Credential

1. 在 n8n 任一 Google Sheets node 中，點選 **Credentials → New**。
2. 選擇「Google」相關的 Credential 類型（例如：**Google Sheets (OAuth2)**，視 n8n 版本而定）。
3. 在 Credential 表單中輸入：

   * **Client ID**：貼上剛剛在 Google Cloud 建立的 OAuth Client ID
   * **Client Secret**：貼上剛剛的 Client Secret
   * 其他 Scope 欄位可以使用預設值（通常會包含 Sheets / Drive 權限）
4. 下方的 **Redirect URL** 應與剛剛在 Google Cloud 設定的 URI 相同。
5. 按 **Connect / Sign in with Google**，會跳出 Google 登入視窗：

   * 選擇你的 Google 帳號
   * 如果看到「尚未通過 Google 驗證」的警告，點「進階 → 前往（不安全）」繼續（因為你只是自用 App）
6. 如果你的 App 是 External 且在「測試模式」，只允許 Test Users 使用：

   * 回到 Google Cloud → OAuth Consent Screen → **Test users**
   * 加入你的 Google 帳號 email
   * 再回 n8n 按一次「Sign in with Google」

完成後，Credential 狀態應顯示為已連線，即可在 Google Sheets nodes 中使用。

> **備註：Service Account 替代方案**
> 若不想使用 OAuth flow，也可以在 Google Cloud 建立 Service Account，下載 JSON Key，並在 n8n 中選擇 Google Service Account 類型的 Credential，將 JSON 內容貼入。
> 此時記得將你的 Spreadsheet 分享給該 Service Account 的 email 地址（像分享給一般人一樣），否則 n8n 無法寫入該試算表。

---

<!-- ## Deploy or Back Up to GitHub / 備份到 GitHub

### 1. 初始化 Git

```bash
git init
```

### 2. 設定 `.gitignore`

確保 `.env` 與敏感資料不會進入版本控制，例如：

```gitignore
.env
.env.*
.n8n/
node_modules/
__pycache__/
```

確認：

```bash
git status
```

看不到 `.env` 或 `.n8n` 就是正確的。

### 3. Token / 密鑰安全

* 若你曾在程式碼中寫死 Token（例如 Discord Bot Token），請：

  1. 到 Discord Developer Portal 重新產生（Reset）該 Token。
  2. 在 `.env` 中更新為新 Token。
  3. 確認程式碼中不再有明文 Token 字串。

### 4. 推送到 GitHub

```bash
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```

推送後，請在 GitHub 網頁上檢查：

* 沒有 `.env`
* 沒有 `.n8n` 或任何憑證 JSON 檔
* 沒有包含 Token 的字串（可用 GitHub 的搜尋功能再確認）

--- -->

## Safety Checklist / 安全檢查

* `.env` **永遠不要** commit 到 repo。
* 發現 Token 曾經公開：

  * 立刻 Reset / Revoke 該 Token（Discord / Google / 其他 API）
  * 更新 `.env` 內的值
* n8n 的 Credential 建議只存在於：

  * 本機 `.n8n` 資料夾
  * 或部署環境的 Secret / 環境變數
* 若將 n8n 部署到公開網路：

  * 請設定基本認證 / 反向代理 / 防火牆，避免 Webhook URL 被濫用

---

## Debug Tips / 除錯提示

* **Bot 完全沒反應**

  * 確認 `DISCORD_TOKEN` 是否正確、Bot 是否有加入伺服器
  * 確認 `ALLOWED_CHANNEL_IDS` 是否包含目前頻道 ID
  * 確認訊息是否以 `~要買` 開頭
* **n8n 沒有任何執行紀錄**

  * 確認 workflow 狀態是 Active
  * 確認 `.env` 中使用的是 `/webhook/discord-shopping` 而不是 `/webhook-test/...`
* **Google Sheets 沒有寫入**

  * 在 n8n → Executions 中查看：

    * Code node 的 OUTPUT 是否有 `itemName` / `modelSpec` / `url` 等欄位
    * Google Sheets node 是否顯示錯誤訊息（例如憑證錯誤或權限不足）
  * 檢查欄位 mapping 是否使用 Expression（`{{$json[...]}}` 灰底 fx）

---

歡迎 fork / PR，如果你增加了更多平台（蝦皮、拼多多…）或自動抓價格邏輯，也可以補充在 README 的 n8n 範例中 😄

```
::contentReference[oaicite:0]{index=0}
```
