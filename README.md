# 1R 助手雲端掃描（電腦關機也會通知）

- 每 15 分鐘掃描一次：幣安成交額前 50 名的山寨幣（不含 BTC、ETH）
- 規則和 TradingView 指標「短線 1R 助手 v3」相同
- 有訊號就傳 Telegram；之後到目標、止損或過了 12 小時，會再傳一則告訴你結果

## 你會收到的訊息

```
【SOL 做多】加分 6/8（歷史勝率約 59–63%）
訊號 K 線收盤：10/07 10:15（台灣）
進場 117.75｜止損 115.33（-2.06%）｜目標 120.17
數量約 2.07 顆（打到止損賠 5.00U）
現價 117.9（+0.13%）→ 可以照計畫進
符合：BTC4h綠、BTC急跌、波動大、4小時急跌、在4h均線上、日線強
```

之後：`【SOL】✓ 到目標`、`【SOL】✗ 止損` 或 `【SOL】⏱ 12 小時未觸及`。

預估數量（全部山寨幣合計）：最低加分 5 分約每天 1 個、6 分約每 2 天 1 個、4 分約每天 2–3 個。行情平靜時會更少。

---

## 設定步驟（約 15 分鐘，只要做一次）

### 1. 建立 Telegram 機器人

1. 在 Telegram 搜尋 **@BotFather** → 傳 `/newbot` → 照指示取名字 → 拿到一串 **token**（像 `123456789:ABCdef...`）。
2. 搜尋你剛建立的機器人 → 按「開始／Start」→ 隨便傳一句話給它。
3. 用瀏覽器打開 `https://api.telegram.org/bot你的token/getUpdates`
   → 找到 `"chat":{"id":一串數字` → 那串數字就是 **chat id**。

> token 等於機器人的密碼，不要貼給別人。

### 2. 建立 GitHub 專案

1. 到 https://github.com 註冊（免費）。
2. 右上角「＋」→ **New repository** → 名稱隨意（例如 `crypto-scan`）→ 選 **Public** → Create。
   - 選 Public 的原因：公開專案的 GitHub Actions **完全免費**。私人專案每月只有 2000 分鐘，每 15 分鐘跑一次會超過。
   - 公開的只有程式碼。token、帳戶資金放在第 3 步的 Secrets 裡，別人看不到。
3. 在新專案頁面按 **uploading an existing file** → 把這個資料夾裡的 `scan.py`、`config.json`、`requirements.txt`、`state.json`、`README.md` 拖進去 → Commit changes。
4. 建立排程檔：按 **Add file → Create new file**
   - 檔名輸入 `.github/workflows/scan.yml`（要打斜線，會自動變成資料夾）
   - 內容：把本機 `cloud_scan\.github\workflows\scan.yml` 用記事本打開，全部複製貼上
   - Commit changes

### 3. 填入秘密設定（Secrets）

專案頁面 → **Settings** → 左邊 **Secrets and variables → Actions** → **New repository secret**，新增：

| Name | Secret |
|---|---|
| `TG_TOKEN` | 第 1 步的 token |
| `TG_CHAT` | 第 1 步的 chat id |
| `EQUITY_USDT` | 你的帳戶資金（例如 `1000`），用來算數量 |

### 4. 測試

1. 專案頁面 → **Actions** 分頁（如果出現提示，按允許啟用 workflows）。
2. 左邊點 **scan** → 右邊 **Run workflow** → 勾選「只傳一則測試訊息」→ Run。
3. 大約 1 分鐘後，Telegram 應該收到「【1R 助手】測試訊息」。
4. 收到之後就不用管了，之後會每 15 分鐘自動掃描。

---

## 之後想改設定

在 GitHub 上點 `config.json` → 右上鉛筆圖示編輯 → Commit：

| 欄位 | 預設 | 意思 |
|---|---|---|
| `min_score` | 5 | 最低加分。改 6 更準但更少，改 4 更多但約 54% |
| `risk_pct` | 0.5 | 每筆風險 %（算數量用） |
| `min_stop_pct` | 1.0 | 止損小於這個 % 不通知 |
| `top_n` | 50 | 掃描前幾名的幣 |

## 注意

- **GitHub 的排程不準時**：常會晚 5～15 分鐘，偶爾會跳過一次。所以訊息裡有「現價」和「可以照計畫進／別追」的提示。
- 資料：先用幣安合約 API；GitHub 主機在美國連不上合約 API 時，會自動改用幣安現貨公開資料。價格和合約差一點點，規則相同。
- `state.json` 會由程式自動更新（記錄追蹤中的訊號和今天的幣池），不要手動改。
- 如果 GitHub 寄信說排程被停用（長時間沒有活動時會這樣），到 Actions 分頁按啟用即可。
- 想先看最近會收到哪些訊號（在本機）：`python scan.py --replay 7`

## 勝率（回測：2020–2026，前 50 名山寨幣，1R）

| 加分 | 勝率（2020–22／2023 後） |
|---|---|
| 4 分 | 54%／54% |
| 5 分 | 56%／60% |
| 6 分 | 59%／63% |
| 7 分 | 66%／70% |

2020 年和 2026 年比較弱。回測假設用限價單進場與止盈。
