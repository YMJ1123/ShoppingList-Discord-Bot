
# Shopping List Discord Bot

Discord 購物清單 bot，使用 **AI（Groq，預設 `openai/gpt-oss-120b`）** 自然語言解析 `買` 指令，將商品資訊送到 n8n Webhook，再由 n8n 寫入 Google 試算表。
下單的人付款後傳淘寶 App 截圖，bot 會算出每個人應付的台幣。
A Discord bot that uses **AI-powered natural language processing (Groq, `openai/gpt-oss-120b` by default)** to parse `買` commands and forward structured data to an n8n webhook, which appends rows to a Google Sheet. It can also read order screenshots and split the bill.

---

## Features / 功能簡介

- 在指定的 Discord 頻道監聽指令（不用 @ bot，直接打指令即可）
- **AI 自然語言解析**（透過 Groq API，預設 `openai/gpt-oss-120b`，可用 `GROQ_MODEL` 更換）：
  - 不需要嚴格格式，用自然語言即可
  - `買 兩台白色的小米風扇 https://...`
  - `買 【淘宝】... https://e.tb.cn/h.xxx 「電動牙刷」`
  - `買1個型號:深空灰 的【淘宝】... https://e.tb.cn/h.xxx 「手機掛繩」`
- AI 自動從訊息中提取：
  - **商品名稱**：從「...」格式或自然語言中辨識
  - **商品型號規格**：顏色、型號、規格等描述
  - **商品網址**：`https://` 開頭的購物連結
  - **商品數量**：支援中文數字（兩台 → 2、三個 → 3）
  - **購買人**：Discord username
  - **平台**：依分享文字／網址自動辨識「淘寶 / 京東」
- 透過 n8n：
  - 使用 `Code` 節點解析 payload（優先使用 AI 預解析的欄位）
  - 使用 `Switch` 節點依 `platform` 分流
  - 將 **淘寶訂單寫入 Sheet「淘寶」**、**京東訂單寫入 Sheet「京東」**
- **刪除**：自然語言（`刪除電動牙刷`、`小米風扇不用買了`）或 `~刪除 <網址>`。只能刪自己加的；
  網址用**商品 ID** 比對，所以追蹤參數不同的連結也認得出是同一件
- 429 rate limit 自動重試機制
- **結算**（詳見 [USAGE.md](USAGE.md) 的「結算」一節）：
  - 下單的人把淘寶 App 截圖和「結算」一起傳上來，bot 用視覺模型讀出品項和價格，對到清單上的登記人
  - 用「實付台幣 ÷ 商品人民幣合計」回算匯率，算出每個人應付的台幣，加總剛好等於實付金額
  - 先貼草稿，下單人按 ✅ 才寫回表格（「實際價格」「結算批次」）並公布；按 ✅ 前追加的截圖會併進同一張草稿
  - 對不到人的商品列為 unclassified，用 `認領 N` 認領；下單人按 🏁 結束認領，剩下的算下單人自己的
  - `結算 @某人` 可以幫付錢的人代傳；同一件商品之前結算過時，草稿會提醒可能重複結算

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
  * 免費方案不需信用卡，但有每分鐘的 token 上限；多人同時使用或結算截圖較多時會撞到 429，bot 會自動等待重試

以下都是選填：

| 變數 | 預設 | 用途 |
|------|------|------|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | 解析訊息、結算時比對商品用的文字模型 |
| `VISION_MODEL` | `qwen/qwen3.8-27b` | 結算時讀截圖用的 Groq 視覺模型（沒設本地模型時使用，或本地失敗時的後備） |
| `LOCAL_VISION_URL` | （不用本地） | 本地視覺模型的 OpenAI 相容網址，例如 Ollama：`http://host.docker.internal:11434/v1`。有設就先用本地讀截圖，截圖不會離開自己的機器 |
| `LOCAL_VISION_MODEL` | `qwen3-vl:8b-instruct` | 本地視覺模型的名稱 |
| `LOCAL_VISION_TIMEOUT` | `45` | 本地讀一張圖最多等幾秒，超過就退回 Groq |
| `N8N_SETTLE_WEBHOOK_URL` | `N8N_WEBHOOK_URL` 加上 `-settle` | 結算用 workflow 的 webhook |
| `STATE_DIR` | （不存檔） | 存結算紀錄的資料夾。有設的話 bot 重啟後還能 `認領` |

### 2. 安裝相依套件

```bash
python -m pip install -r requirements.txt
```

### 3. 啟動 bot

```bash
python bot.py
```

### 部署（Docker Compose）

正式環境把 n8n 和 bot 放在同一個 compose 專案，開機自動啟動，bot 用 compose 網路裡的 `n8n` 主機名連 webhook。
compose 檔不在 repo 裡（放在 repo 的上一層），以下是目前使用的內容：

```yaml
name: shoppinglist
services:
  n8n:
    image: n8nio/n8n:2.16.0          # 鎖版本，避免自動升級觸發資料庫遷移
    restart: unless-stopped
    ports: ["127.0.0.1:5678:5678"]
    volumes: [n8n_storage:/home/node/.n8n]
  bot:
    build:
      context: ./ShoppingList-Discord-Bot
      dockerfile_inline: |
        FROM python:3.11-slim
        RUN pip install --no-cache-dir discord.py==2.6.4 openai==1.57.4 requests==2.31.0 python-dotenv==0.21.0 opencc-python-reimplemented==0.1.7
    image: shoppinglist-bot
    working_dir: /app
    command: python -u bot.py
    restart: unless-stopped
    volumes:
      - ./ShoppingList-Discord-Bot:/app:ro   # 程式碼唯讀掛載：git pull 後 restart 就生效
      - bot_state:/data                      # 結算紀錄
    env_file: ./ShoppingList-Discord-Bot/.env
    environment:
      N8N_WEBHOOK_URL: http://n8n:5678/webhook/discord-shopping
      LOCAL_VISION_URL: http://host.docker.internal:11434/v1   # 主機上的 Ollama；連不上時 bot 會自動退回 Groq
      PYTHONDONTWRITEBYTECODE: "1"
      STATE_DIR: /data
      TZ: Asia/Taipei
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on: [n8n]
volumes:
  n8n_storage:
  bot_state:
```

更新程式碼：在 repo 裡 `git pull`，再 `docker compose restart bot`。
在容器裡跑測試：`docker compose run --rm -T --no-deps bot python test_bot.py`。

讀截圖的順序：本地模型（`LOCAL_VISION_URL`）→ 連不上、超過 `LOCAL_VISION_TIMEOUT` 秒、或讀不完整 → Groq。
本地連不上或逾時之後的 5 分鐘內會直接用 Groq，不會每張圖都等到逾時。bot 的 log 會寫每張圖是哪一邊讀的（`讀圖: 本地 …` / `讀圖: Groq …`）。

> ⚠️ 同一個 `DISCORD_TOKEN` 只能有一個 bot 在跑，兩邊同時跑的話每則訊息都會被處理兩次。

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

### 刪除

自然語言（AI 判斷刪除意圖）：

```text
刪除電動牙刷
幫我把小米風扇刪掉
電動牙刷不用買了
取消 https://e.tb.cn/h.xxx
```

或舊格式（不經過 AI，直接用訊息裡第一個網址）：

```text
~刪除 https://e.tb.cn/h.xxx
~刪除 【淘宝】7天无理由退货 https://e.tb.cn/h.xxx 「電動牙刷」
```

- 只會在「購買人是自己」的列裡找。
- 有網址就先用**商品 ID** 比對（淘寶／天貓的 `id=`、京東的 `item.jd.com/<ID>`），抓不到 ID 的短連結比去掉參數的網址；
  沒有網址或網址對不到時，再比商品名稱（完全相同優先，其次互相包含）。
- 同一層對到好幾筆時不會刪第一筆：workflow 回 `status: "choose"` 和候選清單，bot 列出編號讓使用者選
  （`刪除 2`，或回覆那則訊息打 `2`），再帶著列號和那一列當時的內容去刪。列號位移時 workflow 會用內容找回同一筆，
  認不出來就回 `changed`、什麼都不刪。邏輯在 `workflow/delete_target.js`，改完跑 `python3 workflow/build_delete.py`。
- 找不到時回覆「在你加入的清單裡找不到這筆商品」。

### 結算／認領／結束認領

```text
結算                ← 附上淘寶 App 截圖（确认订单頁或訂單詳情頁）；按 ✅ 前可以再傳圖追加
結算 @付錢的人       ← 幫別人代傳
認領 3              ← 沒對到人的 #3 是我的
認領 3 5 7 / 3-6    ← 一次認領多件
認領 3 @某人         ← 幫別人認領
```

草稿上按 ✅ 確認、❌ 取消；正式結果上按 🏁 結束認領。完整說明見 [USAGE.md](USAGE.md)。

---

## n8n Workflow Setup / n8n 工作流程設定

整體流程示意：

```text
ShoppingListBot（webhook: discord-shopping）
Discord Bot (AI 解析) → n8n Webhook → Code → Switch(action)
                                              ├─ add    → Switch(platform) → 淘寶／京東 Sheet 新增一列
                                              └─ delete → 讀「購買人＝自己」的列 → Find Delete Target → 刪除該列

ShoppingListSettle（webhook: discord-shopping-settle）
Discord Bot（讀截圖、比對、算金額）→ n8n Webhook → 讀兩個分頁 → Plan → 寫回「實際價格」「結算批次」
```

> 💡 n8n 2.x 在編輯器按 Ctrl+S 只會存草稿，正在跑的是「已發布」的版本。改完一定要按 **Publish**
> （用 CLI 的話是 `n8n publish:workflow --id=…`，然後重啟 n8n）。

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

### 結算用的 workflow：`ShoppingListSettle.json`

結算功能用另一個獨立的 workflow（webhook path：`discord-shopping-settle`），不會動到上面新增／刪除的流程。
它用同一組 Google Sheets 憑證直接呼叫 Sheets API：

```text
Webhook → Read Sheets（讀兩個分頁）→ Plan（Code）→ 需要寫入？ → Write Sheets → Respond
```

- `{"action":"list"}`：回傳兩個分頁裡「結算批次」還是空白的列。
- `{"action":"settle","batch":…,"rows":[…]}`：先檢查每一列的商品名、購買人，以及尚未結算或屬於同批次的相同金額，
  全部通過才一次寫入「實際價格」和「結算批次」；有任何一列不對就回 409、什麼都不寫。
- 已公布結算可回覆附圖建立追加草稿，原下單人或代傳人按 ✅ 後，以原批次寫回新配對列並公布更新金額。
  同付款漏圖會重算分攤，既有編號與認領資料會保留。按 🏁 結束認領後無法追加。
- 同批次、同商品、同購買人與相同實際價格的寫入可重試。Bot 在追加寫入前保存確認紀錄，重啟後可接續完成。
- 表格沒有「結算批次」欄的話，第一次結算時會自動加在最右邊。

`Plan` 節點的程式在 `workflow/settle_plan.js`，改完用 `python3 workflow/build_settle.py` 重新產生 JSON
（spreadsheet ID 和憑證 ID 寫在 `build_settle.py` 裡，換表格要改那裡）。匯入與發布：

```bash
n8n import:workflow --input=ShoppingListSettle.json
n8n publish:workflow --id=SLsettle20261112   # 發布後要重啟 n8n 才會生效
```

測試：`python3 test_bot.py`、`python3 test_settle.py`、`node workflow/test_settle_plan.js`、`node workflow/test_delete_target.js`。

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

4. 填入下面這段程式（`ShoppingListBot.json` 即採用此邏輯，會同時處理新增 / 刪除）。
   注意：Python 端已透過 AI 預解析好 `itemName` / `quantity` / `modelSpec` / `url`，此節點優先使用這些現成欄位：

   ```js
   // === ShoppingListBot 用 Code 節點 ===
   // 同時處理 action = "add" / "delete"
   // Python 端已透過 AI 解析好 itemName / quantity / modelSpec / url
   // 此節點優先使用這些現成欄位，減少二度 Regex 解析

   const items = $input.all();
   const out = [];
   const urlRegex = /https?:\/\/\S+/;

   for (const item of items) {
     const payload = item.json.body || item.json || {};
     const action = payload.action || 'add';
     const senderId = payload.senderId || '';
     const senderName = payload.senderName || '';
     const createdAt = payload.createdAt || new Date().toISOString();

     // 決定 URL：優先用 payload.url（Python 端已解析好）
     let shareText = payload.shareText || payload.fullText || '';
     let url = payload.url || '';
     if (!url) {
       const m = shareText.match(urlRegex);
       if (m) url = m[0];
     }

     // 預設平台：淘寶
     let platform = '淘寶';
     if (
       /京东/.test(shareText) ||
       (url && (url.includes('jd.com') || url.includes('3.cn')))
     ) {
       platform = '京東';
     }

     if (action === 'add') {
       const quantity = parseInt(payload.quantity, 10) || 1;

       // 優先使用 AI 預解析的 itemName
       let itemName = payload.itemName || '';
       if (!itemName) {
         const titleMatch = shareText.match(/「([^」]+)」/);
         if (titleMatch) {
           itemName = titleMatch[1].trim();
         } else {
           itemName = shareText;
         }
       }

       out.push({
         json: {
           action: 'add',
           platform, itemName,
           modelSpec: payload.modelSpec || '',
           url, unitPrice: '',
           quantity, estimatedPrice: '', actualPrice: '',
           buyer: senderName, senderId, createdAt,
         },
       });
       continue;
     }

     if (action === 'delete') {
       out.push({
         json: { action: 'delete', platform, url, senderName, senderId, createdAt },
       });
       continue;
     }

     out.push({ json: payload });
   }

   return out;
   ```

---

### 3. 建立 Switch Node（依 action 分流）

1. 新增節點：`Switch`（命名為 `Switch Action` 以利辨識）。
2. 將 `Code → Switch Action` 接起來。
3. 設定：

   * **Value / Property to evaluate**：`{{$json["action"]}}`
   * **Data Type**：`String`
   * **Rules**：
     1. Equals `add`
     2. Equals `delete`

這樣就能讓後續流程分成兩條路徑：

* Output 0（`add`）→ 寫入 Google Sheet
* Output 1（`delete`）→ 依 URL + 購買人刪除對應列

---

### 4. 建立 Switch Node（依平台分流：Add Flow）

1. 新增節點：`Switch`（建議命名為 `Switch Platform`）。
2. 將 `Switch Action (0: add)` 接到這個 Switch。
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

輸出結果：

* Output 0 → 淘寶
* Output 1 → 京東

之後要擴充其他平台（例如蝦皮），只要在 Code 中增加 `platform` 判斷，並在這個 Switch 中再加 Rule 即可。

---

### 5. Google Sheets Nodes（寫入試算表：Add Flow）

#### 5.1 淘寶 Sheet

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

#### 5.2 京東 Sheet

1. 複製上一個 node（右鍵 `Append 淘寶` → Duplicate），改名為 `Append 京東`。
2. 接在 Switch Output 1：`Switch (1) → Append 京東`。
3. 修改：

   * **Sheet**：改為 `京東`
   * 其餘 Expression mapping 保持相同

---

### 6. 刪除流程：查詢並刪除指定列

1. 新增第二個 `Switch`（命名為 `Switch Platform (delete)`），把 `Switch Action (1: delete)` 接進來，判斷邏輯與 add 流程相同（`platform === 淘寶` / `京東`）。
2. 針對兩個平台各自放一個 **Google Sheets → Lookup** 節點（在介面裡叫 `Get rows in sheet`）：
   * **Filters**：`商品網址 = {{$json["url"]}}`、`購買人 = {{$json["senderName"]}}`
   * **Options → Return First Match**：啟用
   * 勾選 **Always Output Data**，即便找不到列也會輸出空物件，方便後續判斷。
3. 在每個 lookup 節點後面加一個 `If`：
   * 條件：`{{$json["row_number"]}}` **is not empty**
   * **True** → 代表找到同一個人貼的同一個連結。
   * **False** → 代表連結不存在或不是同一位購買人，直接回覆 403。
4. `If (True)` 連到 `Google Sheets → Delete rows or columns`：
   * **Operation**：`Delete rows`
   * **Start Index**：`{{$json["row_number"]}}`（Google Sheets node 會自動附帶的欄位）
5. 刪除節點後方加 `Respond to Webhook`，回傳：
   ```json
   { "status": "deleted" }
   ```
6. `If (False)` 以及刪除節點拋錯時，改回傳：
   ```json
   { "status": "not_owner_or_not_found" }
   ```
   並把 **Response Code** 設為 `403`，提醒使用者要由原貼文人刪除。

---

### 7. 啟用 Workflow

確認連線為：

```text
Webhook → Code (AI 已預解析) → Switch(action)
                                     ├─ add → Switch(platform) → Append 淘寶/京東 → Respond {"status":"added"}
                                     └─ delete → Switch(platform) → Get rows → If row exists?
                                                                    ├─ delete row → Respond {"status":"deleted"}
                                                                    └─ Respond 403 {"status":"not_owner_or_not_found"}
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
  * 確認訊息是否以 `買` 開頭
* **AI 解析失敗（回覆「AI 解析服務暫時忙碌」）**

  * 確認 `GROQ_API_KEY` 是否正確設定在 `.env` 中
  * 到 [console.groq.com/settings/limits](https://console.groq.com/settings/limits) 確認 API 額度
  * Bot 有內建 429 rate limit 自動重試（最多 3 次），正常使用不太會撞到限制
* **改了 n8n workflow 但沒生效**

  * Ctrl+S 只存草稿，要按 **Publish**；用 CLI 發布的話要重啟 n8n
* **回覆「請求已送出，但 n8n 超過 30 秒沒回應」**

  * Google Sheets 偶爾很慢，這筆通常其實已經寫進去了，先看表格再決定要不要重送
* **結算沒反應或讀不到金額**

  * 截圖要拍到「立即支付 TWD」或「实付款 TWD」那一行；一張圖不能超過 3MB
  * Groq 免費額度撞到 429 時 bot 會等待重試，截圖多的話要等一兩分鐘
* **按了 ✅ 沒反應**

  * 只有下單人（或代傳的人）按的才算，而且要按在**最新**那張草稿上；追加截圖後舊草稿會被刪掉換新的
  * 草稿只存在記憶體，bot 重啟後沒確認的草稿就沒了，要重新傳
* **n8n 沒有任何執行紀錄**

  * 確認 workflow 狀態是 Active
  * 確認 `.env` 中使用的是 `/webhook/discord-shopping` 而不是 `/webhook-test/...`
* **Google Sheets 沒有寫入**

  * 在 n8n → Executions 中查看：

    * Code node 的 OUTPUT 是否有 `itemName` / `modelSpec` / `url` 等欄位
    * Google Sheets node 是否顯示錯誤訊息（例如憑證錯誤或權限不足）
  * 檢查欄位 mapping 是否使用 Expression（`{{$json[...]}}` 灰底 fx）

---

歡迎 fork / PR，如果你增加了更多平台（蝦皮、拼多多…），也可以補充在 README 的 n8n 範例中 😄
