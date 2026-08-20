# Long Run Hybrid Coach

**繁體中文** · [English](README.en.md) · [简体中文](README.zh-Hans.md)

Long Run Hybrid Coach 是一個非官方、Intervals-first、device-agnostic 的個人化 hybrid training coach。它維護同一份 28 天方向與本週跑步＋重訓課表，讀取可信的實際完成 evidence 持續複盤，並可在你確認後把課表送到 Intervals.icu 日曆。

**Garmin 不是使用前提。** Garmin 是目前第一條做過實機 dogfood 的下游裝置路徑；Apple Watch、COROS、Polar、Suunto、Wahoo、其他 app／手錶，甚至沒有手錶，都可以走同一個 Coach。差異在於有多少可信 evidence 能進到訓練迴圈，以及 Intervals 後面的裝置同步路徑是否已驗證。

> **一般使用者優先選 Hosted MCP：** `https://mcp.paceandstaystrong.com/mcp`。你需要一個 Intervals.icu 帳號，但不需要自己建立 Intervals OAuth App，也不需要自己維運 gateway。

---

## Quick Start：Hosted MCP

### 使用前需要什麼？

1. 一個 **Intervals.icu 帳號**。
2. 一個能連 remote MCP、而且能提供本產品需要動作的 AI client。
3. 選配：已經會把活動同步進 Intervals.icu 的手錶或訓練 app。

沒有 Garmin 也可以使用。沒有自動 recovery evidence 時，Coach 會把缺少的資料視為 unknown，而不是 0。

### 1. 連上 Hosted Coach

MCP endpoint：

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai / Claude Desktop**：Settings → Connectors → Add custom connector → 貼上 endpoint。這條路徑已做過 production OAuth、coaching turn 與 Intervals delivery 的完整驗證。
- **ChatGPT**：完整 MCP（包含 write／modify actions）目前依 OpenAI 官方說明提供給 ChatGPT Business、Enterprise 與 Edu 的網頁版 beta；Pro 的 custom MCP 目前只有 read/fetch，不能完成本 Coach 的 plan write／delivery 全流程。若你的 workspace 支援完整 MCP，在 Apps／developer mode 建立 custom app 並指向上面的 remote endpoint。最新方案限制請以 [OpenAI 官方說明](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt) 為準。
- **OpenClaw**：用 `openclaw mcp add` 指到同一個 endpoint，並加上 `--auth oauth`；一個 instance 若不只一個人用，要把 OAuth identity 設成 per-requester，否則所有人會連到同一個 Intervals 帳號。設定見 [entrypoints/openclaw/](entrypoints/openclaw/README.md)。
- **其他 MCP client**：把同一個 URL 設成 remote Streamable HTTP MCP server；實際是否能跑完整流程取決於該 client 是否支援本產品需要的 MCP/OAuth 行為。

逐入口「已完整實機驗證」或「已封裝、等待真實連線驗證」的狀態，以 [entrypoints/](entrypoints/README.md) 為準。

### 2. 授權 Intervals.icu

第一次連線時，瀏覽器會開 Intervals.icu 的同意頁。登入**你自己的 Intervals.icu 帳號**並授權 Coach 需要的能力：

- `ACTIVITY:READ`：讀已完成訓練。
- `WELLNESS:READ`：讀 Intervals 可提供的 wellness evidence。
- `CALENDAR:WRITE`：讀／寫訓練日曆，讓確認過的課表可以交付並 read-back 驗證。
- `SETTINGS:WRITE`：讀設定；只有在已確認的 delivery 流程真的需要時，才補上缺少且有 evidence 支持的 running threshold setting。

Intervals 的同意頁會把權限分開。少勾一項時，依賴那項權限的能力會明確失敗；重新連線並補上權限即可。**不要把 Intervals 密碼、API key 或 token 貼進對話。**

### 3. 直接問正常的教練問題

不用先填問卷，例如：

```text
讀我最近的訓練，告訴我這週該怎麼練。
```

或：

```text
我想提升 VO2max，又不想掉力量，幫我排第一個 28 天方向。
```

Coach 會先讀已經存在的 evidence，再只問真正會改變決策的缺口，例如本週可練日、器材、或 provider 不可能知道的重訓 baseline。

### 4. 先看 28 天 preview，再確認計畫

第一次建立計畫時會先看到：

- **本週**：精確、可執行、可交付的 session。
- **後三週**：方向性 outlook，不假裝現在就知道所有細節。

你確認那份 preview 後，計畫才會寫入。之後的每週改動也是同一條體驗：

**before / after preview → 一次確認 → apply**。

### 5. 要送進日曆時，再做 delivery 確認

交付是另一個獨立確認：

**delivery preview → 一次確認 → 寫入 Intervals.icu → read-back 驗證**。

本產品能證明的最遠狀態是 `intervals_accepted`。**Intervals 成功不等於課表已經在 Garmin、Apple Watch 或其他手錶上。** Intervals 後面的同步是外部 hop，要依裝置路徑各自驗證。

---

## Intervals.icu 在這個產品裡做什麼？

Intervals.icu 是目前的 **interoperability hub**：它幫 Coach 接住不同裝置／app 的活動與 wellness evidence，也承接 Coach 確認後的日曆課表。它**不是 Coach 的 PlanState source of truth**。

```text
手錶 / 訓練 app
      │
      ▼
 Intervals.icu ───── 已完成活動 + wellness evidence ─────► Coach
      ▲                                                   │
      │                                                   │
      └────────── 確認後的 calendar workout ◄────────────┘
      │
      ▼
Garmin / Apple Watch bridge / 其他下游同步
```

責任分工：

- **Intervals.icu**：整合外部訓練 evidence，並持有 provider calendar。
- **Long Run Hybrid Coach**：持有唯一 current PlanState、decision history、athlete-reported evidence、確認 binding 與 coaching workflow。
- **你的手錶／app**：可以把活動帶進 Intervals，也可能接收 Intervals 往下送的 workout；但最後一哩是否成功，是獨立 compatibility evidence。

### Intervals 裡一定要先有資料嗎？

只有**帳號本身**是必要條件。有活動／wellness 已同步進去，Coach 的自動 evidence 會比較完整；沒有的欄位保持 unknown，不會被當成 0，也不會因為少一個選配數值就把一般 coaching 擋掉。

裝置量不到或沒有同步的東西可以直接在對話裡講，例如：

- 重訓實際組數、重量與次數；
- 本週可練時間與器材限制；
- 體重／體脂；
- 沒帶錶的一場活動；
- 「最近很累」「睡不好」這種 subjective state；
- 你從手錶／app 實際看到的 sleep、HRV、resting HR、readiness 等 recovery reading。

Coach 不會把一句「我很累」偷偷翻成一個假的 readiness score。

---

## Hosted MCP vs Local / Self-hosted MCP

實際會感覺到的差別只有一個：**hosted 你在手機上就能直接用；local 只有在跑 gateway 的那台電腦上能用。**下面其他每一行，都是這個差別的成本。

| | Hosted MCP（推薦） | Local / Self-hosted MCP |
| --- | --- | --- |
| 手機上能用嗎 | 能——連一個有手機 App 的 client 就好 | 不能，除非你自己把 gateway 對外開放並處理 TLS |
| MCP URL | `https://mcp.paceandstaystrong.com/mcp` | 你自己的 gateway，例如 `http://127.0.0.1:8422/mcp` |
| 維運 | 不用自己管 server | 自己啟動、更新、備份與維運 |
| Intervals OAuth App | **不需要** | **需要**自己的 OAuth application credential |
| current plan 存哪 | hosted per-athlete owner store | 你自己的 gateway state root |
| 適合誰 | 一般使用者、多 client 共用同一計畫 | 開發者、需要完全自管環境／資料的人 |

### Hosted MCP 最短啟用流程

1. 在支援完整需求的 MCP client 新增 remote MCP app／connector。
2. URL 貼 `https://mcp.paceandstaystrong.com/mcp`。
3. 完成 client 的 OAuth 流程。
4. 瀏覽器到 Intervals.icu 同意授權。
5. 回到聊天後直接問第一個教練問題。

Hosted 服務會自己處理 dynamic client registration、PKCE、gateway token 與 per-athlete owner mapping；一般使用者不需要 owner id、athlete id、API key、Intervals client secret 或 server environment variable。

### Local / Self-hosted MCP 怎麼跑？

Repo 使用 Python 3.11，產品本身是 stdlib-only，不需要先安裝一串 runtime Python package。

1. Clone repo。
2. **向 Intervals.icu 申請建立 OAuth application。** Intervals 目前的公開流程不是在 Settings 自助新增 app：依官方 OAuth 說明提供 app name、description、website、logo、privacy policy、redirect URI 與你的 Intervals ID；app 建立後才會出現在 Settings，從 **Manage App** 取得 `client_id` / secret。流程見 [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759)。
3. 在 Intervals app 裡註冊 gateway provider callback：`<gateway-origin>/oauth/callback`。本機 client 可以走 loopback；remote client 需要可達的 HTTPS／secure tunnel。
4. 設定 gateway 必要環境變數：

```bash
export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$HOME/.local/share/long-run-hybrid-coach-gateway"
export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..."
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
```

5. 啟動：

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

6. 本機 MCP client 指到：

```text
http://127.0.0.1:8422/mcp
```

如果要正式提供給 remote client，不要把 loopback 範例當 production runbook。Persistent volume、TLS、trusted client origin、single replica、release identity 與部署驗證見 [docs/deploy-gateway.md](docs/deploy-gateway.md)。

### Local CLI 不應該默默變成第二份 current plan

一個 athlete 應該只有一個 current writer。當本機設定 `GARMIN_COACH_LOOP_GATEWAY_URL` 指向 hosted coach 時，本機 store 寫入預設會被擋；只有明確加 `--offline` 才代表「我刻意在做另一份 local plan」。

已經有本機 state 的人可以搬到 hosted，完整流程見 [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md)。

---

## 現在可以做什麼？

目前產品能力包括：

- 維護一份 **28 天方向**：本週精確 session ＋ 後三週 outlook。
- 讀 Intervals activity／wellness／calendar evidence，並把可信的 planned → actual 自動 reconciliation 回 current plan。
- 在同一份週計畫裡同時處理跑步與重訓。
- 記錄 athlete-reported profile、availability、long-term goal、training preference、實際重訓、體重／體脂、裝置沒錄到的活動，以及 subjective state。
- `startCoachSession` 接收本次 request 的 recovery readings；Hosted 不需要也不會去讀你的本機 health database。
- 匯入支援的歷史 evidence，包括支援格式的 CSV、Apple Health XML 內容，以及透過 binary import path 處理的 FIT payload；同檔與同活動會做 deterministic 去重，判斷不了才問使用者。
- Session 可帶 `coach_note`，讓教練的重點文字一起進 Intervals event，而不是偷偷長出第二套 workout grammar。
- 每週複盤「實際練了什麼、是否有進步證據、下一步是什麼」，而不是把「課表做完」直接當成 fitness 已提升。
- 計畫變更先 preview，再確認後 apply。
- 日曆交付先 preview，再確認；支援安全 retry、replace 與 withdraw product-owned event。
- 在對話裡直接匯出或兩段式永久刪除本產品持有的 owner data。

### 重要邊界

- `startCoachSession` 會做 deterministic reconciliation，**可能寫入新的 PlanState version**；如果只要完全無 side effect 的 stored state，用 `getCoachState`，它才是 read-only 路徑。
- athlete-reported activity 是 evidence，但不會被偷偷升格成 provider-backed actual completion。
- recovery 數字只接受真的觀察值；模型不可以從文字自己猜一個數字。
- 本產品不做醫療診斷。
- Delivery 證據只到 Intervals read-back，不會聲稱已經到手錶。

---

## 資料、匯出與刪除

Hosted 端保存維持同一個 owner 計畫所必要的產品狀態：PlanState version chain、decision／receipt、athlete-reported evidence、identity mapping，以及未收斂 delivery bookkeeping。

匯出時刻意**不包含**：OAuth credential 的 keyed **fingerprint**、provider raw payload／**GPS** track，以及 internal **owner id**。Fingerprint 是單向 bookkeeping；raw GPS／活動檔應由 provider 提供；owner id 是內部 storage locator。

刪除產品資料也有三個明確邊界，這三件不在本產品能刪的範圍：

- 已經寫進 **Intervals.icu 日曆** 的 workout；
- 你在 **Intervals.icu Settings** 給出的 provider 授權；
- 不含 plan、健康或 identity 內容的最小化平台**營運紀錄**。

完整生命週期見 [docs/account-lifecycle.md](docs/account-lifecycle.md)，公開隱私政策在 [paceandstaystrong.com/privacy.html](https://paceandstaystrong.com/privacy.html)。

---

## 目前限制

- Coach 不直接登入 Apple Health、Garmin Connect 或其他裝置帳號；主要自動 evidence 路徑目前仍是 Intervals.icu。
- Hosted 不會永久保存每次 request 傳入的 raw recovery upload；下一次需要就再傳當下 evidence。
- 本產品不觀察 Intervals 之後的每一個裝置同步 hop，因此不會把 `intervals_accepted` 說成「已經在手錶上」。
- Local self-hosting 是 operator／developer 路徑；一般使用者應優先 Hosted MCP。
- 裝置相容性是逐路徑 evidence，不因 Garmin 已驗證就推論其他裝置一定相同。

---

## 產品 surface 與技術文件

目前 release 對外有 **22 個 MCP tool**、**2 個 prompt**、**30 個 CLI 指令**、**3 份 JSON Schema contract**、**5 張 identity 表**。這些數量由測試從真實程式碼推導，避免 README 自己走鐘。

- 穩定使用者故事：[docs/user-story.md](docs/user-story.md)
- 資料來源與欄位邊界：[docs/data-sources.md](docs/data-sources.md)
- 入口與平台設定：[entrypoints/](entrypoints/README.md)
- MCP protocol、OAuth 與 tool 行為：[entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- Hosted gateway 部署：[docs/deploy-gateway.md](docs/deploy-gateway.md)
- 帳號生命週期：[docs/account-lifecycle.md](docs/account-lifecycle.md)
- 公開上架／reviewer 材料：[docs/distribution/](docs/distribution/README.md)
- Release inventory：[docs/release-inventory.md](docs/release-inventory.md)
- Repository invariants 與驗證：[AGENTS.md](AGENTS.md)

Long Run Hybrid Coach 是獨立專案，與 Garmin、Intervals.icu、Apple 或其他裝置／平台供應商沒有隸屬、背書或贊助關係。程式碼以 [MIT License](LICENSE) 釋出。
