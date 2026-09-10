
# Shopping List Discord Bot

團購用的 Discord 購物清單 bot，用 **AI（Groq）** 解析自然語言指令——加商品、刪商品、結單、回填價格、分帳——再透過 n8n Webhook 寫入 Google 試算表。  
A Discord bot for group buying that uses **AI-powered natural language processing (Groq)** to handle adding, removing, order closing, price reporting and bill splitting, forwarding structured data to an n8n webhook that reads and writes a Google Sheet.

---

## Features / 功能簡介

- 在指定的 Discord 頻道監聽指令（不用 @ bot，直接打指令即可）
- **AI 意圖判斷**：一句自然語言就判斷你想做什麼，不需要記指令格式

  | 意圖 | 例子 |
  |------|------|
  | 加入清單 | `買 兩台白色的小米風扇 https://...` |
  | 刪除 | `刪除電動牙刷`、`風扇不用買了` |
  | 結單 | `結單`、`可以下單了` |
  | 回填價格 | `價格 電動牙刷 120`、`牙刷總共 240` |
  | 看清單 | `清單`、`目前有什麼要買的` |
  | 分帳 | `分帳`、`大家要付多少` |

- AI 自動從訊息中提取：
  - **商品名稱**：從「...」格式或自然語言中辨識
  - **商品型號規格**：顏色、型號、規格等描述
  - **商品數量**：支援中文數字（兩台 → 2、三個 → 3）
  - **價格**：分得出「單價」和「總價」（牙刷一個 120 / 牙刷總共 240）
  - **商品網址**：`https://` 開頭的購物連結
  - **平台**：依分享文字／網址自動辨識「淘寶 / 京東」
- **結單流程**：把清單上所有待下單的項目整批凍結、蓋上批次號（`YYYYMMDD-N`）、記下下單人與時間
  - 結單前會先列出這批的完整內容，要按 ✅ 二次確認才會執行（不可逆的批次操作）
  - 結單後那批就不能再自己刪除，帳才不會對不起來
  - 選填 `ORDER_MANAGER_IDS` 可以限制只有下單負責人能結單
- **分帳**：依購買人加總實際價格，並明確列出還沒填價格的項目（否則金額看起來對、其實不完整）
- 只有「買」開頭或命中意圖關鍵字的訊息才會送 LLM，其他訊息完全不理，省 API 額度
- 429 rate limit 自動重試機制（最多 3 次）

---

## Requirements / 環境需求

- Python 3.10+
- n8n（本機或遠端皆可）
- Google 帳號（用來建立試算表與 API 憑證）
- **Groq API Key**（免費，不需信用卡，到 [console.groq.com](https://console.groq.com) 申請）
- 一個 Discord Bot：
  - 已建立於 Discord Developer Portal
  - 在 Bot 設定中啟用 **MESSAGE CONTENT INTENT**

Python 套件依 `requirements.txt` 安裝：

```bash
python -m pip install -r requirements.txt
```

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
GROQ_API_KEY=gsk_your_groq_api_key
# 選填
GROQ_MODEL=openai/gpt-oss-120b
ORDER_MANAGER_IDS=
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
* `GROQ_API_KEY`

  * 到 [console.groq.com](https://console.groq.com) 註冊後，在 API Keys 頁面建立
  * 免費方案：每分鐘 1,000 請求、每日約 14,000 請求，不需信用卡
* `GROQ_MODEL`（選填）

  * 預設 `openai/gpt-oss-120b`。Llama 3.3 70B 已於 2026-08-16 對免費／Developer 方案下線
  * 中文想更強可以試 `qwen/qwen3.6-27b`（預覽版）
* `ORDER_MANAGER_IDS`（選填）

  * 逗號分隔的 Discord **user ID**，設了之後只有這些人能結單
  * 留空＝任何人都能結單，仍然需要按 ✅ 二次確認才會執行

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

### 買（自然語言，AI 解析）

不需要嚴格格式，只要以 `買` 開頭，後面用自然語言描述即可。AI 會自動提取商品名稱、數量、型號規格和網址。

範例：

```text
買 兩台白色的小米風扇 https://e.tb.cn/h.xxx
買 【淘宝】7天无理由退货 https://e.tb.cn/h.xxx 「電動牙刷」
買1個型號:深空灰 的【淘宝】大促价保 https://e.tb.cn/h.xxx 「手機掛繩」
買 三個【京东】https://3.cn/2v-s8bsu 「藍牙耳機」
```

AI 會自動辨識：
* 中文數字 → 阿拉伯數字（兩台 → 2、三個 → 3）
* 顏色/型號/規格描述（白色、深空灰、256GB...）
* 「...」中的商品名稱
* `https://` 開頭的購物連結

唯一的硬性要求：**訊息中必須包含至少一個 `https://` 開頭的購物網址**。

### 刪除（自然語言）

```text
刪除電動牙刷
幫我把小米風扇刪掉
電動牙刷不用買了
取消 https://e.tb.cn/h.xxx
```

- 只能刪**自己加的、而且還沒下單**的項目。
- 有網址就用網址精確比對，沒有就用商品名稱（完全相同優先，其次模糊包含）。
- 已經結單的項目不給刪，bot 會告訴你它在哪個批次。

`~刪除 <URL>` 的舊格式仍然支援，走 regex 快速通道、不經過 AI。

### 結單

```text
結單
可以下單了
```

1. bot 先把這批待下單的完整內容列出來（依平台分組、含網址，方便照著買）。
2. 按 ✅ 才會真的結單，按 ❌ 或 60 秒沒反應就取消。
3. 結單後那批被蓋上批次號 `YYYYMMDD-N`，狀態變 `已下單`，並記下下單人與時間。

### 回填價格

```text
價格 電動牙刷 120      # 單價
牙刷一個 120           # 單價
電動牙刷總共 240       # 總價，會自己反算單價
```

- 單價會寫進「商品價格」，並自動算出「實際價格」＝單價 × 數量。
- 只給總價時反過來推算單價。
- 優先找已下單的項目（價格通常是下單後才知道），找不到才回頭找待下單的。

### 看清單 / 分帳

```text
清單                  # 目前待下單的項目
分帳                  # 最新批次，依人加總
分帳 20260910-1        # 指定批次
```

分帳會列出每個人的小計與總計，並把**還沒填價格的項目單獨標出來**——否則金額看起來對、其實少算。

---

## n8n Workflow Setup / n8n 工作流程設定

整體流程示意：

```text
Discord Bot (AI 判斷意圖) → n8n Webhook → 撈出兩張表 → Plan（決定要做什麼）
                                                        ├─ 寫入／更新／刪除 淘寶 或 京東 Sheet
                                                        └─ 只回話（清單／分帳）
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

   | 預購買商品 | 商品型號規格 | 商品網址 | 商品價格 | 商品數量 | 預估價格 | 實際價格 | 購買人 | 狀態 | 批次 | 下單時間 | 下單人 |
   | ----- | ------ | ---- | ---- | ---- | ---- | ---- | --- | -- | -- | ---- | --- |

各欄位由誰填：

| 欄位 | 誰填 | 說明 |
|------|------|------|
| 預購買商品／商品型號規格／商品網址／商品數量／購買人 | `買` 指令 | AI 從訊息解析 |
| 狀態 | 系統 | `待下單` → 結單後變 `已下單` |
| 批次 | 結單 | `YYYYMMDD-N`，同一天第幾批 |
| 下單時間／下單人 | 結單 | 誰在什麼時候結的 |
| 商品價格（單價）／實際價格 | `價格` 指令 | 實際價格＝單價 × 數量，分帳用這欄 |
| 預估價格 | 手動 | 程式不會動它 |

> **狀態欄留空會被視為 `待下單`**，所以舊資料不用回填也能正常運作。

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

### 2. Workflow 架構 / Workflow architecture

目前的 workflow 共 13 個節點。所有判斷邏輯集中在一個 `Plan` Code 節點，Google Sheets 節點只負責讀寫：

```text
Webhook → Parse Request → Load TB → Load JD → Plan → Switch Target ┬→ Append TB ┐
                                                                   ├→ Append JD │
                                                                   ├→ Update TB │
                                                                   ├→ Update JD ├→ Respond
                                                                   ├→ Delete TB │
                                                                   ├→ Delete JD │
                                                                   └→ (READ) ───┘
```

* **Parse Request**：正規化 webhook payload、判斷平台（淘寶／京東）。
* **Load TB / Load JD**：一次撈出兩張表的所有列，供全部 action 共用。兩個節點都要開
  **Always Output Data**（空表也得往下走，不然流程會斷）與 **Execute Once**（避免被上游 items 重複觸發）。
* **Plan**：唯一有邏輯的地方。依 `action` 決定要回什麼話、要對哪些列做什麼，輸出的每個 item
  帶一個 `_target`（`TB_APPEND` / `JD_UPDATE` / `TB_DELETE` / `READ`…）。
* **Switch Target**：照 `_target` 把 item 分流到對應的 Sheets 節點；fallback 輸出 `READ`
  代表這次只回話、不動表格。
* **Respond**：所有分支都連到這裡，回應固定取 `$('Plan').first().json._response`，
  所以不管哪條路線先到，回覆內容都一樣。

更新一列時要注意：`Plan` 會把**沒有要改的欄位填回原值**再送進 Update 節點，
否則 Google Sheets 會把 mapping 裡的空值寫回去、把原本的資料清掉。

#### 改邏輯的方式 / How to edit the logic

Code 節點的程式碼以 `workflow/` 下的 JS 檔為**唯一真相**，`ShoppingListBot.json` 由 script 生成
（JSON 裡的程式碼字串既不能 review 也不能測試）：

```bash
node workflow/test_plan.js     # Plan 的單元測試
node workflow/test_parse.js    # Parse Request 的單元測試
python3 workflow/build.py      # 重新生成 ShoppingListBot.json
```

`build.py` 會沿用現有 JSON 裡的 documentId、sheetName、credentials 與 webhookId，
所以重新生成不會弄壞你在 n8n 裡設好的憑證；生成後仍需要在 n8n 重新匯入一次。

#### 測試 / Testing

**1. 離線驗證邏輯**（不碰 Google Sheet，也不需要 n8n）：

```bash
node workflow/mock_n8n.js &                                        # 假 n8n，記憶體模擬兩張表
python3 workflow/smoke_test.py --url http://localhost:5999/webhook/test
pkill -f mock_n8n                                                  # 收工
```

`mock_n8n.js` 跑的是真正的 `parse_request.js` + `plan.js`，只把 Google Sheet 換成記憶體陣列
（連刪列後 `row_number` 往前遞補都照樣模擬）。`GET` 同一個位址可以看表格內容。
測不到的是 n8n 節點本身的行為（Sheets 欄位對應、憑證、Switch 分流）。

**2. 對真的 n8n 測**，但寫進測試分頁而不是團員在看的正式分頁：

```bash
# 在同一份試算表複製出「淘寶TEST」「京東TEST」兩個分頁，記下它們的 gid
python3 workflow/build.py --out ShoppingListBot.test.json \
    --tb-gid <淘寶TEST的gid> --jd-gid <京東TEST的gid> \
    --webhook-path discord-shopping-test
# 匯入 ShoppingListBot.test.json（獨立 workflow，不會蓋掉正式版）並 Active
python3 workflow/smoke_test.py --url http://localhost:5678/webhook/discord-shopping-test
```

測試版會自動拿掉 workflow id、換掉 webhookId、名稱加上 `(TEST)`，所以不會跟正式版衝突。
`smoke_test.py` 的 URL 不含 `test` 時會擋下來要求 `--yes`，避免手滑寫進正式表。

**3. Discord 端到端**：`ENV_FILE=.env.test python3 bot.py` 可以用另一份設定跑
（測試頻道 ID ＋ 測試 webhook URL）。`✅` 結單確認流程只有在 Discord 才測得到。

> 舊版架構（Switch Action / Switch Platform / 每個 action 各一組 Get rows）的逐步建置教學
> 已隨改版移除，需要的話可以在 git 歷史裡找回舊的 `README.md`。

---

### 3. 啟用 Workflow / Activate

匯入後在 n8n 右上角把 workflow 切成 **Active**，並確認 Webhook 的 Production URL 與 `.env` 的
`N8N_WEBHOOK_URL` 一致（要是 `/webhook/...`，不是 `/webhook-test/...`）。


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
  * 確認訊息是否以 `買` 開頭
* **AI 解析失敗（回覆「AI 解析服務暫時忙碌」）**

  * 確認 `GROQ_API_KEY` 是否正確設定在 `.env` 中
  * 到 [console.groq.com/settings/limits](https://console.groq.com/settings/limits) 確認 API 額度
  * Bot 有內建 429 rate limit 自動重試（最多 3 次），正常使用不太會撞到限制
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
