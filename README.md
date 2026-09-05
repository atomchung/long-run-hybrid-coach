# Long Run Hybrid Coach

**繁體中文** · [English](README.en.md) · [简体中文](README.zh-Hans.md)

官網 [paceandstaystrong.com](https://paceandstaystrong.com/zh/) ｜ 遇到問題看[支援頁](https://paceandstaystrong.com/zh/support.html)

把一個網址貼進 Claude 或 ChatGPT，你就有一個讀得到你真實訓練的教練。

它讀你 Intervals.icu 帳號裡的活動與恢復數據，維持同一份 28 天的跑步＋重訓方向與本週課表，拿計畫去對你實際做了什麼，並在你同意之後，把每一堂課排進你的日曆。**免費使用，沒有付費方案。**

**Garmin 不是使用前提。** Garmin 只是目前第一條做過實機驗證的下游裝置路徑；Apple Watch、COROS、Polar、Suunto、Wahoo、其他 app／手錶，甚至沒有手錶，都可以用同一個教練。差別在於有多少可信的訓練紀錄能進到教練手上，以及 Intervals 後面那段裝置同步是否已經被驗證過。

> **一般使用者直接用託管版：** `https://mcp.paceandstaystrong.com/mcp`。你需要一個 Intervals.icu 帳號，但不需要自己向 Intervals 申請 OAuth App，也不需要自己維運伺服器。

---

## 連接：兩步

官網把同一條流程拆成四個點擊步驟走一次：[開始使用](https://paceandstaystrong.com/zh/#setup)。

### 你需要什麼

1. **一個 Intervals.icu 帳號。** 免費，用 Google 登入大約 30 秒就能開好。
2. **Claude 或 ChatGPT。**
   - **Claude**（claude.ai／Claude Desktop）：免費方案就可以，免費帳號限一個自訂連接器。這條路徑已經做過完整實機驗證——production OAuth、教練對話、以及把課表送進 Intervals。
   - **ChatGPT**：目前需要 Business、Enterprise 或 Edu 的網頁版 workspace，在 Apps／developer mode 建立 custom app。個人方案的 custom MCP 只有 read/fetch，跑不完本產品「寫計畫＋交付課表」的流程；個人方案請先等這個教練在 ChatGPT 目錄上架。最新限制以 [OpenAI 官方說明](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt) 為準。
   - **其他 MCP client**：OpenClaw 與自架 client 見[下面一節](#其他-mcp-client)。
3. **選配**：已經會把活動同步進 Intervals.icu 的手錶或訓練 app。沒有手錶也可以用——教練會把拿不到的欄位當成「不知道」，不會當成 0。

### 第一步：把網址貼進你的 AI

在 AI 的連接器設定裡新增一個 remote MCP server，網址是：

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai／Claude Desktop**：Settings → Connectors → Add custom connector → 貼上網址。
- **ChatGPT**：Apps／developer mode → 建立 custom app → 指到同一個網址。

沒有別的欄位要填，也沒有金鑰要貼。

### 第二步：授權 Intervals.icu

瀏覽器會開 Intervals.icu 的同意頁。登入**你自己的 Intervals.icu 帳號**，四個權限都勾起來：

| 權限 | 教練拿它做什麼 |
| --- | --- |
| `ACTIVITY:READ` | 讀你已完成的訓練。 |
| `WELLNESS:READ` | 讀 Intervals 手上的恢復數據。 |
| `CALENDAR:WRITE` | 讀寫訓練日曆，讓你確認過的課表能送進去，並讀回來核對。 |
| `SETTINGS:WRITE` | 讀你的門檻配速。它唯一會寫進去的，是你**還沒有**的門檻配速，發生在你已經確認過的課表交付當下；已經設好的絕不覆寫。 |

Intervals 也有只讀的設定權限；這裡要寫入，是因為上面那個補值動作真的會寫。它之所以需要，是因為 Intervals 這邊沒有門檻配速時，它照樣收下有配速的課表，但往手錶送的時候會把配速目標拿掉——你會收到距離正確、卻沒有任何目標的一堂課。少勾任何一個權限，需要它的功能就會壞掉，而且不容易看出原因；重新連線補上即可。

**不要把 Intervals 密碼、API key 或 token 貼進對話。**

### Intervals 帳號是新的或空的？

教練讀的是 Intervals.icu 裡已經有的東西。帳號是新的話，有兩條路把歷史補進去：

- **接上你原本就在用的裝置或 app。** 在 Intervals.icu 自己的 Settings 裡連 Garmin 或你的手錶／訓練 app，過去的活動會自動補上。資料到了之後，再請教練重新讀一次。
- **把匯出檔直接交給教練。** 在對話裡給它 CSV、Apple Health 匯出檔或 `.fit` 檔，請它匯入。同檔與同一場活動會自動去重，判斷不了才回頭問你。（檔案是給教練的，不會進 Intervals.icu。）

裝置量不到的東西也可以直接在對話裡講：重訓的實際組數重量次數、本週能練的時間與器材、體重體脂、沒帶錶的那一場、「最近很累」「睡不好」，以及你從手錶上實際看到的睡眠、HRV、靜止心率、readiness 數字。教練不會把一句「我很累」偷偷翻成一個假的 readiness 分數。

---

## 連上之後會發生什麼

### 直接問正常的教練問題

不用先填問卷：

```text
讀我最近的訓練，告訴我這週該怎麼練。
```

或：

```text
我想提升 VO2max，又不想掉力量，幫我排第一個 28 天方向。
```

教練會先讀已經有的資料，再只問真正會改變決策的缺口——例如本週可練日、器材，或裝置不可能知道的重訓基準。

### 第一次會先看 28 天預覽，你同意才寫進去

- **本週**：精確、可執行、可以直接送進日曆的課。
- **後三週**：大方向，不假裝現在就知道所有細節。

之後每一次改動也是同一條體驗：**改動前後對照 → 你同意一次 → 才寫入**。

### 要送進日曆時，再確認一次

交付是另一個獨立確認：**課表預覽 → 你同意一次 → 寫進 Intervals.icu → 讀回來核對**。

本產品能證明的最遠一步是 Intervals.icu 收下了。**Intervals 成功不等於課表已經在 Garmin、Apple Watch 或其他手錶上**——Intervals 後面那段同步是外部路徑，要各自驗證。

---

## Intervals.icu 在這個產品裡做什麼？

Intervals.icu 是目前的**中轉站**：它幫教練接住不同裝置／app 的活動與恢復數據，也承接教練確認後的日曆課表。它**不是計畫本身的存放處**。

```text
手錶 / 訓練 app
      │
      ▼
 Intervals.icu ───── 已完成活動 + 恢復數據 ─────► 教練
      ▲                                          │
      │                                          │
      └────────── 你確認過的日曆課表 ◄───────────┘
      │
      ▼
Garmin / Apple Watch / 其他下游同步
```

責任分工：

- **Intervals.icu**：整合外部訓練資料，並持有日曆。
- **Long Run Hybrid Coach**：持有唯一的當前計畫、決策歷史、你自己回報的紀錄、每一次確認的綁定，以及整條教練流程。
- **你的手錶／app**：可以把活動帶進 Intervals，也可能接收 Intervals 往下送的課表；但最後一哩是否成功，要獨立驗證。

只有**帳號本身**是必要條件。Intervals 裡已經有活動與恢復數據的話，教練自動拿得到的資料會比較完整；沒有的欄位保持「不知道」，不會被當成 0，也不會因為少一個選配數值就把一般教練對話擋掉。

---

## 託管版 vs 自架

實際會感覺到的差別只有一個：**託管版你在手機上就能直接用；自架只有在跑伺服器的那台電腦上能用。**下面每一行都是這個差別的成本。

| | 託管版（推薦） | 自架 |
| --- | --- | --- |
| 手機上能用嗎 | 能——連一個有手機 App 的 client 就好 | 不能，除非你自己把伺服器對外開放並處理 TLS |
| 網址 | `https://mcp.paceandstaystrong.com/mcp` | 你自己的伺服器，例如 `http://127.0.0.1:8422/mcp` |
| 維運 | 不用自己管 | 自己啟動、更新、備份與維運 |
| Intervals OAuth App | **不需要** | **需要**自己的 OAuth application 憑證 |
| 計畫存哪 | 託管端、以每位使用者為範圍 | 你自己的伺服器 state root |
| 適合誰 | 一般使用者、多個 client 共用同一份計畫 | 開發者、需要完全自管環境／資料的人 |

託管版會自己處理 dynamic client registration、PKCE、token 與使用者對應；一般使用者不需要任何 id、API key、client secret 或環境變數。

### 其他 MCP client

- **OpenClaw**：用 `openclaw mcp add` 指到同一個網址，加上 `--auth oauth`。一個 instance 若不只一個人用，要把 OAuth identity 設成 per-requester，否則所有人會連到同一個 Intervals 帳號。設定見 [entrypoints/openclaw/](entrypoints/openclaw/README.md)。
- **其他**：把同一個網址設成 remote Streamable HTTP MCP server。跑在你自己機器上的 client（OAuth callback 落在 loopback）可以直接連；跑在雲端主機、用自己網域接 callback 的 client，註冊會被拒絕，需要先把該 origin 加進部署的信任清單。細節見 [entrypoints/mcp/README.md](entrypoints/mcp/README.md)。

逐入口「已完整實機驗證」或「已封裝、等待真實連線驗證」的狀態，以 [entrypoints/](entrypoints/README.md) 為準。

### 自架怎麼跑？

Repo 使用 Python 3.11，產品本身只用標準函式庫，不需要先安裝一串套件。

1. Clone repo。
2. **向 Intervals.icu 申請建立 OAuth application。** Intervals 目前的公開流程不是在 Settings 自助新增：依官方說明提供 app name、description、website、logo、privacy policy、redirect URI 與你的 Intervals ID；app 建立後才會出現在 Settings，從 **Manage App** 取得 `client_id` / secret。流程見 [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759)。
3. 在 Intervals app 裡註冊 callback：`<gateway-origin>/oauth/callback`。本機 client 可以走 loopback；remote client 需要可達的 HTTPS 或安全通道。
4. 設定必要環境變數：

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

6. 本機 MCP client 指到 `http://127.0.0.1:8422/mcp`。

要正式提供給 remote client 的話，不要把 loopback 範例當 production runbook。Persistent volume、TLS、信任的 client origin、single replica、release identity 與部署驗證見 [docs/deploy-gateway.md](docs/deploy-gateway.md)。

### 一個人只該有一份當前計畫

本機設定 `GARMIN_COACH_LOOP_GATEWAY_URL` 指向託管版時，本機寫入預設會被擋；只有明確加 `--offline` 才代表「我刻意在做另一份本機計畫」。已經有本機資料的人可以搬到託管版，流程見 [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md)。

---

## 現在可以做什麼？

- 維護一份 **28 天方向**：本週精確的課 ＋ 後三週大方向。
- 讀 Intervals 的活動、恢復數據與日曆，把可信的「排了什麼 vs 實際做了什麼」自動對回當前計畫。
- 在同一份週計畫裡同時處理跑步與重訓。
- 記錄你自己回報的東西：個人資料、可練時間、長期目標、訓練習慣、實際重訓、體重體脂、裝置沒錄到的活動，以及主觀狀態。
- 每次對話可以帶入當下的恢復數字。託管版不會、也不需要去讀你電腦上的健康資料庫。
- 匯入歷史資料：支援格式的 CSV、Apple Health XML，以及 FIT 檔。同檔與同一場活動會自動去重，判斷不了才問你。
- 課表可以帶一段教練備註一起進 Intervals，而不是偷偷長出第二套課表寫法。
- 每週複盤「實際練了什麼、有沒有進步的證據、下一步是什麼」，而不是把「課表做完」直接當成體能提升。
- 計畫變更先看對照，同意後才套用。
- 日曆交付先看預覽，同意後才寫入；支援安全重試、取代與撤回本產品自己送出去的課表。
- 在對話裡直接匯出，或分兩段永久刪除本產品持有的資料。

### 重要邊界

- 開始一次教練對話會做自動對帳，**可能寫入新的計畫版本**。只想看目前存了什麼、完全不動到資料的話，用唯讀的那條路徑（`getCoachState`）。
- 你自己回報的活動是佐證資料，不會被偷偷升格成裝置確認的完成紀錄。
- 恢復數字只接受真的觀察值；模型不可以從文字自己猜一個數字出來。
- 本產品不做醫療診斷。
- 交付的證據只到 Intervals 讀回來核對，不會聲稱已經到手錶。

---

## 資料、匯出與刪除

託管端保存維持同一份計畫所必要的東西：計畫的版本鏈、決策與回執、你自己回報的紀錄、身分對應，以及還沒收斂的交付紀錄。

匯出時刻意**不包含**三樣東西：授權憑證的 fingerprint（單向的指紋，只拿來做內部記帳）、供應商的原始 payload 與 GPS 軌跡（原始活動檔應該跟供應商拿），以及內部的 owner id（本產品自己的儲存位置編號）。

刪除也有三個明確邊界，這三件不在本產品能刪的範圍：

- 已經寫進 **Intervals.icu 日曆**的課表；
- 你在 **Intervals.icu Settings** 給出的授權；
- 不含計畫、健康或身分內容的最小化平台**營運紀錄**。

完整生命週期見 [docs/account-lifecycle.md](docs/account-lifecycle.md)，公開隱私政策在 [paceandstaystrong.com/zh/privacy.html](https://paceandstaystrong.com/zh/privacy.html)（英文版為準）。

---

## 目前限制

- 教練不直接登入 Apple Health、Garmin Connect 或其他裝置帳號；主要的自動資料路徑目前仍是 Intervals.icu。
- 託管版會按日期保存你提供的睡眠分數、睡眠時長、昨夜 HRV 與靜息心率，直到你刪除帳號資料。教練每次讀取最近 28 天，這不是保存期限；其他傳入的恢復數字只供當次判斷。恢復紀錄目前不支援單日撤回，詳見 [資料說明](docs/data-sources.md#consequence-for-the-coach)。
- 本產品不觀察 Intervals 之後的每一段裝置同步，因此不會把「Intervals 收下了」說成「已經在手錶上」。
- 自架是給開發者／自管的人；一般使用者應優先用託管版。
- 裝置相容性是逐條路徑的證據，不會因為 Garmin 已驗證就推論其他裝置一定相同。
- Remote client 的 OAuth callback origin 不是開放註冊：loopback 一律可用，claude.ai／claude.com／chatgpt.com 內建信任，其他雲端主機上的 client 要先由維運者加進信任清單。
- 這是一個人的專案，不是公司。不保證服務不中斷，也可能改變。

---

## 遇到問題

- **[支援頁](https://paceandstaystrong.com/zh/support.html)**：匯出、刪除、更正、撤銷授權——大部分事情你在對話裡自己就能做完，而且比等人回覆快。上面也有直接寄給開發者的信箱。
- **[Issue tracker](https://github.com/atomchung/long-run-hybrid-coach/issues)**：bug 與功能建議。它是公開且永久的，**不要貼**健康／訓練／計畫內容、Intervals 的 athlete id、token 或任何憑證。要指認自己的帳號，用你資料匯出檔裡那個不可還原的參照碼就夠了。
- 認為是資安或隱私漏洞的話，不要公開描述，直接寄信。

---

## 技術文件

- 穩定使用者故事：[docs/user-story.md](docs/user-story.md)
- 使用者路徑與對應的呼叫：[docs/user-flows.md](docs/user-flows.md)
- 資料來源與欄位邊界：[docs/data-sources.md](docs/data-sources.md)
- 入口與平台設定：[entrypoints/](entrypoints/README.md)
- MCP protocol、OAuth 與 tool 行為：[entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- 託管部署：[docs/deploy-gateway.md](docs/deploy-gateway.md)
- 帳號生命週期：[docs/account-lifecycle.md](docs/account-lifecycle.md)
- 公開上架／reviewer 材料：[docs/distribution/](docs/distribution/README.md)
- Release inventory：[docs/release-inventory.md](docs/release-inventory.md)
- Repository invariants 與驗證：[AGENTS.md](AGENTS.md)

目前 release 對外有 **22 個 MCP tool**、**2 個 prompt**、**31 個 CLI 指令**、**4 份 JSON Schema contract**、**9 張 identity 表**。這些數量由測試從真實程式碼推導，避免這份文件自己走鐘。

Long Run Hybrid Coach 是獨立專案，與 Garmin、Intervals.icu、Apple 或其他裝置／平台供應商沒有隸屬、背書或贊助關係。程式碼以 [MIT License](LICENSE) 釋出。
