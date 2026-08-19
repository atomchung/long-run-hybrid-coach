# Long Run Hybrid Coach

Long Run Hybrid Coach 是一個非官方、Intervals-first、device-agnostic 的個人化 hybrid
coach。它把 Intervals.icu 彙整的訓練／wellness evidence、目前計畫、實際完成狀態與
生活限制，持續維護成同一份 28 天方向與本週跑步＋重訓課表。

Garmin 是目前 dogfood 的主要裝置，也是第一個做過實機 delivery 驗證的裝置；它不是
使用這個產品的必要條件，也不是核心 domain model。只要運動員的裝置或 app 能把足夠的
訓練 evidence 帶進 Intervals.icu，Coach 就應該在同一條產品路徑上工作；不同裝置的差異
主要落在 evidence richness 與 Intervals 之後的最後一哩課表交付。

教練判斷由模型做，資料、對帳、驗證與交付由 deterministic code 做。完整的職責
邊界見 [AGENTS.md](AGENTS.md)。

Long Run Hybrid Coach 是一個獨立專案，與 Garmin、Intervals.icu、Apple 或其他裝置／
平台供應商沒有隸屬、背書或贊助關係。Garmin 與 Intervals.icu 是各自所有者的商標。

程式碼以 MIT License 釋出，見 [LICENSE](LICENSE)。

> **這份 README 是完整的使用流程**：從連線、第一次對話、每天每週怎麼用，到匯出、
> 撤銷、刪除與搬遷，不需要拼湊其他檔案。`docs/` 與 `entrypoints/` 底下是操作員細節
> 與逐平台設定，**這些延伸文件都是英文的**，本檔只連過去、不重抄一份。

---

## 這個產品做什麼，以及 v1 的資料邊界

一句話：**維護同一份「現在該練什麼」**——28 天方向、本週可執行的跑步與重訓課表，
以及確認後送進日曆的課。它不是訓練日誌、不是體能分析儀表板、不做醫療判斷。

資料從哪裡來，v1 只有三條，其他都還沒有：

| 來源 | 帶進什麼 | 邊界 |
| --- | --- | --- |
| **Intervals.icu**（OAuth 連線） | 完成的活動、可取得的 wellness 摘要、日曆上的計畫課表 | 唯一的 provider actual 來源，也是課表寫出去的路徑 |
| **運動員自己講的**（對話中） | 時區與語言、可練的日子、實際做的重訓組數重量、體重／體脂、裝置沒錄到的一場訓練 | 明確標記為 athlete-reported，**永遠不會被當成 provider actual** |
| **本機 health database**（只有 local reader 能開） | `strength_execution`（實際舉了什麼）與 `recovery_signals`（readiness／HRV status／acute load／Body Battery／stress） | local build 可用 `--health-db`；連 hosted 的 local Agent 只傳整理後的 values，永遠不傳路徑、credential 或 raw DB |

**Canonical 是講計畫，不是講資料最多。** hosted coach 持有唯一的當前計畫，也永遠讀不到
任何人機器上的 health database。`strength_execution` 由運動員自己講的
`recordStrengthExecution` 補上；`recovery_signals` 則可由有本機資料權限的 Coding Agent
先整理成七日 typed values，再隨同一次 `startCoachSession` 傳入。Gateway 只驗證並放進本次
CoachContext，不保存原始 upload；下一次對話要用就再傳一次。沒有 local reader 的入口仍是
`null`，而且不會阻塞一般 coaching。歷史的那一半仍然開著（issue #101）。逐欄位的能與不能見
[docs/data-sources.md](docs/data-sources.md)。

Intervals.icu 是目前的 interoperability hub，不是 Coach 的 source of truth。Coach 的
長期記憶與計畫仍由 PlanState、DecisionEvent、athlete evidence 等產品狀態持有；Intervals
負責把外部訓練 evidence 帶進來，並承接產品確認後的 calendar/workout delivery。

裝置相容性分成兩個方向，不混為一談：

- **讀取（device/app → Intervals → Coach）**：只要 Intervals 已經有足夠的 activity／wellness
  evidence，Coach 就用同一條路徑判斷，不需要為每個手錶品牌建立一套 Coach adapter。
- **交付（Coach → Intervals → device/app）**：產品只保證自己已經寫入並 read-back 驗證
  Intervals event；Intervals 後面如何同步、最終裝置能呈現哪些 structured workout 能力，
  是逐裝置驗證的 compatibility 問題。

Garmin 的實機觀察是目前第一份 compatibility evidence，不應被提升成全產品契約。Apple
Watch 等平台本身也有 structured workout 能力，但只有在實際確認 Intervals 後段路徑後，
產品才聲稱那條 device delivery 可用。

---

## 一個 athlete 一份計畫：hosted 是 canonical

ChatGPT、claude.ai、跑在自己機器上的 MCP client，連的是同一個 hosted gateway 的
`/mcp`；同一個 Intervals athlete 永遠解析到同一個 owner 與同一份 current PlanState，
重新授權不會生出第二份。多個 client 可以同時存在，連上新的不會把舊的踢掉。

**一個 athlete 只有一個 writer。** 本機那份可寫的 store 因此只剩兩個正當用途：完全不接
hosted 的 local 執行，或開發／排練用的另一個 store。設定
`GARMIN_COACH_LOOP_GATEWAY_URL=https://<gateway-domain>` 之後，這台機器就宣告了
「計畫在 hosted」——所有會寫本機 store 的指令（apply-decision、publish-delivery、
record-profile…）都必須明講 `--offline` 才會執行：

~~~text
$ python3 -m garmin_coach_loop.cli record-profile --timezone Asia/Taipei
{
  "status": "blocked",
  "error": "record-profile writes a local store, and this machine's plan lives on the
   hosted coach at https://mcp.paceandstaystrong.com. Do it there, or pass --offline to
   work on a local store that is deliberately not the athlete's current plan."
}
~~~

要讓整個 shell 都當 offline（開發機、排練環境）則設 `GARMIN_COACH_LOOP_MODE=offline`，
效果與每次帶 `--offline` 相同。沒設 gateway 變數的機器行為完全不變。

搬過去之後，本機那份會被封存（`hosted-handoff.json`），連 `--offline` 都寫不進去——
見〈已經有本機 state 的人怎麼搬過去〉。

---

## 連線設定

**整條流程沒有任何一步會問你、也不接受你提供 owner ID。** 誰的資料由 credential 決定：
`exportOwnerData`、`prepareOwnerDeletion`、`applyOwnerDeletion`、`getCoachState` 的
input schema 都是空的，連一個可以填進別人帳號的欄位都不存在。

生產環境的 gateway 是 `https://mcp.paceandstaystrong.com`，MCP endpoint 是它的 `/mcp`。

| 入口 | 怎麼連 |
| --- | --- |
| claude.ai / Claude Desktop | Settings → Connectors → *Add custom connector*，貼上 `https://mcp.paceandstaystrong.com/mcp` |
| ChatGPT / OpenAI plugin（OpenAPI） | 用 `entrypoints/custom-gpt/openapi.yaml` 這份契約；合約有維護、有測試 |
| ChatGPT MCP connector | 同一個 `/mcp` URL |
| Claude Code / Agent SDK Skill | 安裝 canonical Skill |
| OpenClaw 等 agent CLI | remote MCP JSON config 指向同一個 URL |

逐平台的設定步驟在 [entrypoints/](entrypoints/README.md)。**哪一條真的跑過真人 OAuth
＋ 真的交付、哪一條只是封裝好等驗證，以那張表為準**——這裡不重抄一份狀態，兩份會走鐘。
protocol 層（tool surface、orchestration prompt、OAuth 全流程）也只寫一次，在
[entrypoints/mcp/README.md](entrypoints/mcp/README.md)。

授權是**被發現的，不是被設定的**。client 不需要任何預先配置：一個沒帶 token 的請求會
拿到 `401` 加上 `WWW-Authenticate` 指向 `/.well-known/oauth-protected-resource`，client
從那裡讀到 authorization server metadata，用 dynamic client registration 自己領一個
`client_id`，再走 PKCE（`S256`）授權碼流程。運動員只會看到 Intervals 的同意畫面，
輸入的是 Intervals 帳密，不是這個產品的。

~~~text
$ curl -s -D - -o /dev/null -X POST https://mcp.paceandstaystrong.com/mcp
HTTP/2 401
www-authenticate: Bearer resource_metadata="https://mcp.paceandstaystrong.com/.well-known/oauth-protected-resource"
~~~

> **手工組 Custom GPT 那條路不再維護。** OpenAPI 那份契約留著——plugin 型的整合需要它，
> 而且每次 commit 都會拿它跟 gateway 真實的 route 與回應對一遍。不再維護的是「把 Action
> schema 和一段 instructions 貼進 GPT Builder」那個流程本身：那是同一份契約的第四份副本，
> 而要讓它跟本體保持同步，得養一整套發版儀式（Vercel 反向代理、Builder 對帳、一台狀態機）。
> 那套儀式連同它的 script 和 operator Skill 一起移除了。已經建好的 GPT 不會壞——gateway 的
> route 一個都沒動——只是這個 repo 不再替它寫建置步驟、也不再測它的發版路徑。
>
> 它跟 hosted 服務的關係也要講清楚：走 OpenAPI 那條需要**你自己的** OAuth application
> credential，所以那份 runbook 帶你架你自己的 gateway，不是接上這個共用的 hosted 服務。

### 從自己機器上讀那份 canonical 計畫

CLI 也是同一個 endpoint 的 client，不是例外。純讀不寫、不呼叫 Intervals，用
`hosted-status`（走 `getCoachState`）：

~~~bash
python3 -m garmin_coach_loop.cli hosted-status --gateway https://mcp.paceandstaystrong.com
~~~

`hosted-session` 走的是 `startCoachSession`：會重新讀 provider evidence 並套用
reconciliation，**可能把 PlanState 推進一個新版本**；輸出一律在
`reconciliation_statement` 講清楚這次有沒有寫：

~~~bash
python3 -m garmin_coach_loop.cli hosted-session --gateway https://mcp.paceandstaystrong.com
~~~

一般 MCP local Coding Agent 呼叫同一個 `startCoachSession` 時，若它已在本機讀到恢復資料，
可加上 `recovery_signals: {source, days}`。每個 day 只有 typed recovery 欄位；Gateway 自己
決定本次七日 window，並把 provenance 標成 `client-uploaded:<source>`。不能傳 DB path、
Garmin credential、raw payload 或模型推算值；遠端 ChatGPT／Claude 沒有本機 reader 時就省略。

兩者都跑完整的 OAuth（dynamic registration + PKCE + 瀏覽器同意）與完整的 MCP
2025-06-18 lifecycle（`initialize` → `notifications/initialized` → 才 `tools/call`），
token 只活在這次 process 裡——不落盤、不進 log、不印出來。

---

## 第一次對話：從 OAuth 到 28 天預覽與一次確認

不會有問卷。第一句話直接問你要問的事就行。

**1. 你問。** 「我想在三個月內把半馬跑進 1:45，同時不要掉重訓，幫我看一下。」

**2. Coach 發現還沒連線。** 第一個 tool call 拿到 `401`，client 開始 OAuth，瀏覽器
跳出 Intervals 的同意畫面。同意完就回到對話——這一步不會問你 owner ID、athlete ID
或任何識別碼。

**3. Coach 讀已經有的東西，不是重新問一遍。** `startCoachSession` 回
`status: "no_plan_state"`，但同時帶 `pre_plan_observations`——Intervals 上已經躺著的
訓練紀錄、local Agent 本次帶來的 `recovery_signals`，加上你在有計畫之前就講過的任何事。
所以它只會問**真的會改變答案的缺口**：
通常是目標、能練的日子，以及裝置量不到的 baseline（例如目前的重訓重量）。

**4. Coach 給一次 28 天預覽。** `prepareCoachInitialization` 回一份 `preview`，
**四週都在裡面**：第一週是精確的、可交付的 session；第二到第四週是 `cycle.outlook`
的輪廓。連同 `unknowns`（哪些資料還不確定）一起顯示給你。

**5. 你確認一次，計畫才存在。** 你說好，Coach 才呼叫 `initializeCoachPlan`。在那之前
沒有任何東西被寫進 store——`prepareCoachInitialization` 的 annotation 就是
read-only。整個第一次流程只需要**一次**確認。

之後的每一次對話，continuity 都靠 `startCoachSession` 重新讀那份 PlanState，不靠聊天
記憶。換一個 client（從 claude.ai 換到手機上的 ChatGPT）讀到的是同一份。

---

## 每天與每週怎麼用

### 讀計畫

- 「今天練什麼」「這週怎麼排」「我最近有進步嗎」——Coach 呼叫 `startCoachSession`。
  它會順便把能確定配對的 planned → actual 完成度寫回去，所以**它不是 read-only**：
  回來的 plan version 可能比出去時高一版。這是刻意的，也是每次都會在回應裡講清楚的。
- 只想知道「現在是第幾版、這週有幾堂、有沒有卡住的交付」而不想有任何副作用——那是
  `getCoachState`（CLI 的 `hosted-status`）：零寫入、零 Intervals 呼叫、不做
  reconciliation。

### 講出裝置量不到的事

都只需要講「變的那一件事」，而且**都不需要確認步驟**——寫進去之後原樣回讀給你看，
那就是你更正的機會，講一次不對就再講一次。

| 你講的 | 進到哪裡 | 更正方式 |
| --- | --- | --- |
| 「我在柏林」「課表給我英文的」 | `recordAthleteProfile` | 只送你講的那一個，另一個保持原樣 |
| 「這週三不能練」「這週出差，飯店只有啞鈴」 | `recordAthleteAvailability` | 常態是一三五時，只講週三照樣留下一和五；`note` 只管這一週，過了就沒了 |
| 「長期想把 VO2max 拉到 50、體重 80 公斤」 | `recordLongTermGoal` | 一個 metric 一筆，重講就蓋掉；跨 cycle 存活，28 天週期的 goal 是它的里程碑而不是第二份 |
| 「我習慣週五跑品質課」「一週想重訓五次」 | `recordTrainingPreference` | 一個 topic 一筆；**只存你講的**，從紀錄推出來的規律不算 |
| 「臥推 60 公斤做了三組八下」 | `recordStrengthExecution` | 日期預設今天；同一天同一個動作再講一次是**更正**，不是多做一組 |
| 「計畫裡那堂重訓照做完了」 | `confirmPrescribedStrength` | 只講有出入的那幾組，沒提到的照處方記 |
| 「今天早上 72.3 公斤」 | `recordBodyMeasurement` | 一天一筆，重講就蓋掉；體重 20–400 kg、體脂 1–75% 以外直接拒絕而不是存下來 |
| 「今天游了 40 分鐘，手錶沒帶」 | `recordActivitySummary` | 同一天同一個運動一筆，重講就取代 |
| 「其實那天沒練臥推」「那筆體重記錯了」「那個目標不追了」 | `retractAthleteRecord` | **收回**而非更正，不留空紀錄；所有紀錄共用一個工具，`kind` 指定收哪一種 |
| 「這是我 Garmin 匯出的三年紀錄」（丟一個 CSV／Apple Health 匯出／`.fit` 檔） | `importAthleteHistory` | 同一個檔再丟一次不會重複匯入；同一場訓練在別的匯出裡出現也認得出來 |

### 丟一個檔案進來，和講一句話進到同一個地方

入口是對話，所以上傳也是對話的一部分。Strava／Intervals.icu／Garmin Connect 的 CSV
看 header 就認得；認不得的 CSV 由 AI 讀完 header 之後告訴它哪一欄是什麼（**包含單位**）；
Apple Health 的 `export.xml` 太大不可能整份送，送裡面的 `<Workout>` 與體重 `<Record>`
片段，讀出來的結果跟整份一樣；`.fit` 是二進位、模型讀不了，所以 base64 送進來由程式解。

底下**沒有第二個 store，也沒有分格式的資料流**：四種來源都收斂到上面那兩個 group，
同一個檔案、同一份 export、同一次刪除。原始檔解析完就丟掉，留下的只有每一場的摘要
加上來源標記。

- **單位不用猜**：認得的匯出自己帶單位；認不得的由 mapping 指定。Garmin Connect 那個
  沒有單位的 `Distance` 欄直接不讀，並在回應裡講明原因——把英里當公里讀，是教練看不到的錯。
- **去重是決定性的**：同一個檔（digest）、同一場的來源 id、或同一天同一個運動且時間差
  三分鐘內（兩邊都有距離時再看距離差一公里內），都算同一場，靜默略過或合併。
- **只有真的分不出來才問你**：同一天同一個運動、但數字對不上，那可能是更正也可能是第二場，
  程式不猜——回一個問題，你回答之後把同一份 payload 再送一次就好。
- **一天可以有兩場**：講出來的沒辦法分辨「更正」和「第二場」，所以一天一筆；匯出檔有開始
  時間，所以早上跑一次晚上跑一次會是兩筆。收回那天的跑步時，會先問你是哪一場。

**回報的訓練不是 provider actual。** `recordActivitySummary` 寫下的那一場，沒有
activity id、沒有配對信心度、沒有完成狀態：它不會進 `recent_actuals`、不會推動任何
coverage、reconciliation 讀不到它，也**永遠不會把某一堂計畫課標記成完成**。一週如果
同時被算成「你講的」和「Intervals 記的」，那就是一週訓練被讀成兩週。它以
athlete-reported evidence 的身分到達教練那裡，就這樣。

體重也一樣不做任何加工：交過去的是原始數列，沒有趨勢、沒有變化率、沒有跟目標比。
一公斤在一個 hybrid block 裡代表什麼，是教練的判讀，不是 store 先替它算好。

**長期目標與習慣是你的，不是教練的。** 長期目標活得比 28 天週期久，所以不放在
PlanState 裡——週期結束時它會跟著消失。週期的 `goal` 是教練往長期目標走的**其中一個
里程碑**，不是同一件事的第二份。習慣則是教練的**起點而不是規則**：validator 讀不到它，
沒有任何課表會因為偏離習慣被擋下來；教練覺得有更好的排法時可以偏離，但要把理由講出來。

反過來也是一條線：**推論不會變成你的陳述。** 連續三週只重訓三次、而你講的是五次，那是
一個要拿出來跟你談的落差，不是產品替你把五改成三——習慣要不要改，是你決定的。同理，從
活動紀錄看出「最近常週日長跑」只是教練當下的讀法，不會被寫進任何一筆 preference。

只管這一週的限制走另一條路：出差、器材、臨時工作都掛在 availability 的 `note` 上，
跟著那一週過期，standing preference 原封不動。

### 改計畫、確認、交付

- 一次每週改動走 `prepareCoachDecision` → 顯示實際的 before/after → **一次確認** →
  `applyCoachDecision`。`confirmation_required: false` 代表沒有實質改動，計畫照舊。
- 要送到日曆：`prepareWorkoutDelivery` → 顯示完整 preview → **一次確認** →
  `applyWorkoutDelivery`；同一組工具加 `withdraw: true` 就是撤回被取代的已交付課表，
  方向不同、確認流程相同。code 負責查重、寫入、read-back verification 與狀態更新。
- 交付狀態只回報產品真正能觀察的證據，最遠到 `intervals_accepted`。Intervals 之後轉送
  到 Garmin Connect、Apple Watch bridge 或其他 app 的過程，是本產品無法逐筆觀察的外部
  hop；**不得**從 Intervals 成功推論 workout 已經到了任何一支手錶。

正常使用不需要手工建立或修改 CoachContext、PlanState、DecisionEvent、proposal 或
receipt JSON。

### 時區與語言

「今天」與「下一堂課」一律由 athlete-local 時區決定，不會從伺服器或裝置所在地推測。
時區講一次就存起來，之後每次 build、session、status、withdrawal 都自己帶上。順序是：
這次 request 講的 > 存起來的 > 預設 `Asia/Taipei`。給錯時區名稱照樣直接回報一則明確
錯誤，絕不悄悄退回預設值。

同一份 profile 也記語言（`zh-Hant` 或 `en`）。它只換 prescription 那層自然語言外殼——
結構欄位、數字、以及動作的 `display_name`（本來就是運動員自己講的名字）都不動。那句話
會成為 Intervals event 的 description 與重訓日的標題。已經寫好的課不會因為改語言被
重寫，之後動到的才用新語言重新生成。

---

## 四週視角與目標的 measurement

**四週都看得到，但只有一週是可執行的。** `plan.week` 是這週：精確的 session、有
session_id、可以交付、會被對帳。`cycle.outlook` 是接下來三週的輪廓——每一週只有
`week_start`、`intent`、`key_sessions`（用你認得的講法，例如「一次長跑，大約 90 分鐘」）
與 `relation_to_primary`（build／hold／recover／measure）。

outlined week **在結構上就無法被交付**：它沒有 session id、沒有 execution、沒有
prescription。所以它也不會過期、不會卡住 delivery、不會被 reconciliation 讀成漏掉的
課。每次週複盤把下一週變精確時，新的 `week` 與縮短後的 `outlook` 一起送出，四週視窗
就往前滾一格。

**measurement 是一堂普通的課，不是新的 session 類型。** `goal.measurement` 指三件事：

- `reference_session_id`——這個 cycle 拿哪一堂課當基準讀數（通常在 cycle 早期）；
- `measurement_week_start`——哪一週重跑同一堂；
- `compare`——什麼固定、讀什麼，例如「同樣路線同樣配速，比平均心率」。

到了那一週，Coach 自己把那堂課排上並標記 `measures`，不需要運動員記得。context 裡的
`measurement_evidence` 只回答一件事：**兩個讀數各自進來了沒有**。判讀由教練給，產品
不算分數、不給結論。`goal.measurement` 是 `null` 代表這個 cycle 根本沒排 measurement——
那是一個真實狀態，會照實講，不會被說成「還沒進步」。

---

## 我們存了什麼

「產品持有的」與「provider 持有的」「平台營運紀錄」是三件不同的事，下面這張表只講
第一件。

| 存了什麼 | 存在哪裡 | 存多久 | 匯得出來？ | 刪得掉？ | owner 隔離 |
| --- | --- | --- | --- | --- | --- |
| PlanState append-only 鏈（目前計畫＋每一版歷史） | hosted owner store 的 `store.json` 與 `commits/*/plan.json` | 直到你刪除；沒有到期、沒有閒置清掃 | ✅ `plan_state` ＋ `decision_history` | ✅ | 一個 owner 一個目錄 |
| DecisionEvent 與 receipt（每次改動是什麼、產生了什麼） | 同一條鏈的 `commits/*/event.json`、`receipt.json` | 同上 | ✅ 在 `decision_history` 裡 | ✅ | 同上 |
| 你自己講的事：profile、availability、重訓回報、體重／體脂、裝置沒錄到的訓練 | 同一個 owner 目錄的 `athlete-evidence.json` | 同上 | ✅ `athlete_evidence` | ✅ | 綁 internal owner id，不綁 Intervals athlete id |
| 未收斂的交付紀錄（哪一堂、寫入還是刪除、走到哪一步） | owner 目錄的 `delivery-attempt.json` | 直到重試收斂或手動接手 | 只帶 `attempt_id`（`unresolved_delivery`） | ✅ 隨帳號一起 | 同上 |
| store snapshot（改動前自動留的備份） | owner 目錄**旁邊**的 `<name>.snapshots/` | 直到你刪除 | ❌ 不進 archive、不進 bundle | ✅ 刪除會一併移除並回報 | 同上 |
| identity 對應（owners／provider_identities／token_fingerprints／token_scopes／owner_revocations 五張表） | gateway 的 identity registry | 直到你刪除 | 只匯出**筆數**、撤銷時刻與 scope 名稱，不匯出任何值 | ✅ | 這就是隔離本身 |
| owner maintenance fence／deletion tombstone | owner 目錄旁的 `<name>.maintenance` | fence 是搬遷期間；tombstone **刻意永久保留** | ❌ | 刪除之後刻意留下來（見下） | 同上 |
| 本機 handoff 封條 | 本機 store 的 `hosted-handoff.json` | 你自己刪掉本機目錄為止 | ❌ 不進 bundle | 本機檔案，你自己處理 | 只在本機 |
| 可攜備份 bundle | `export-store` 產生的檔案，0600，原子寫入 | 你自己保管、自己刪 | 它本身就是匯出物 | 你自己刪 | 檔案在你手上 |

**這些從來不落盤，也永遠不在任何匯出裡：**

- **OAuth state 與授權碼**——授權碼 60 秒有效，用完即失效。
- **gateway 自己發的 access token**——伺服器端不存：token 本身就是儲存。所以沒有
  session、沒有 SSE stream、沒有 server-side proposal 資料庫。
- **Intervals 的 provider token**——加密封在那個 token 裡，只有 gateway 持有金鑰。
  identity registry 存的是 HMAC 過的單向 fingerprint，換不回 token。
- **confirmation proposal**——簽章而非儲存，15 分鐘有效，過期就重跑一次 preview。
- **Intervals 的原始 payload、GPS 軌跡、活動檔**——讀進來組 context，讀完就沒了。
  要那些請直接從 Intervals.icu 匯出。

**provider 持有的**（Intervals.icu 的活動、wellness、日曆上的 event、你給的授權）
與**平台營運紀錄**（request path 與拒絕原因，不含任何計畫、健康或身分內容，
見 [docs/ops/security-events.md](docs/ops/security-events.md)）都不在上表，也不在
匯出與刪除的範圍內。

完整的隱私與保存政策在 [公開網站](https://paceandstaystrong.com/privacy.html)；
連線／斷線／匯出／刪除四個動作的逐條差別在 [docs/account-lifecycle.md](docs/account-lifecycle.md)。
匯出與刪除都不用找人，自己在對話裡做；真的需要找人時走
[Support](https://paceandstaystrong.com/support.html)，那頁也寫了
哪些東西不能貼進公開 issue。

---

## 匯出、撤銷、重新連線、兩段式刪除

### 匯出：在對話裡直接要

> 「你手上有我哪些資料？給我一份。」

Coach 呼叫 `exportOwnerData`（不吃任何參數）。回來的 archive 有 `plan_state`、
`decision_history`、`athlete_evidence`、identity 的**筆數**、撤銷時刻（`revoked_after`，
從沒撤銷過就是 `null`）、每個連線的 scope 名稱、還沒收斂的交付 `attempt_id`，以及一份
`excluded` 清單。archive 自己會列出這三條**沒有**放進去的東西，逐字如下：

- OAuth access token 與它們的 keyed fingerprint：這個產品不存任何 provider credential，
  而 fingerprint 是單向 digest，換不回 token。
- Intervals.icu 的原始 payload、GPS 軌跡與活動檔：它們被讀來組 context、從不寫下。
  要那些請從 Intervals.icu 自己匯出。
- 任何其他運動員的資料，以及指向這個帳號儲存位置的 internal owner id。

「這是全部」跟「這是我們會給你看的全部」是兩句不同的話，只有後面那句是真的——所以
archive 自己講。

### 撤銷連線，以及撤銷之後重連

**你自己就能停掉未來的存取**：到 intervals.icu → Settings → 已授權的應用程式，把
Long Run Hybrid Coach 撤掉。那一刻起這個產品讀不到也寫不進你的 Intervals 帳號。這是
你的授權，只有你能收回——**在這裡刪資料不會替你收回它，撤銷它也不會刪掉這裡的資料**。

產品這一側還有一個更窄的動作，`revoke-connections`（operator 執行，不帶 `--confirm`
就只是預覽會移除什麼）：把這個 owner 已記錄的連線移除。每一個入口都靠在 identity
registry 查 fingerprint 來解析請求，所以 gateway 已經發出去的 token 在同一刻全部失效，
不需要維護撤銷清單。

- **PlanState 完全不動。** 重新登入解析到同一個 owner、同一份計畫。
- **撤銷後立刻重連是可以的。** 撤銷時刻與 token 簽發時刻都是整秒；同一秒內重連曾經
  會被誤判成「早於撤銷」而被拒到下一秒。現在 token 會帶上簽發當下讀到的撤銷時刻，
  同秒重連直接可用。
- **它碰不到 Intervals。** provider 自己的授權還是要在 intervals.icu 撤銷。

想「停止未來存取」與想「刪掉已有資料」是兩件事，要哪個做哪個。

### 刪除：預覽 → 一次確認 → 收據

> 「把你們手上關於我的資料全部刪掉。」

**第一段：`prepareOwnerDeletion`**——不吃參數、不寫任何東西，回一份 `removes`：
`plan_id`、`plan_versions`（幾版歷史）、`reported_strength_sessions`、
`body_measurements`、`reported_activities`、`reported_availability`、`stored_profile`、
`long_term_goals`、`training_preferences`、`identity_rows`（五張表各幾筆）、
`stored_snapshots`，加上 `reversible: false`。

同一份預覽也會逐字列出**刪不到的三件事**：

- 這個產品已經寫進你 Intervals.icu 日曆的那些課。它們屬於你的 Intervals.icu 帳號；
  想清掉請在那邊清。
- 你的 Intervals.icu 授權。到 Intervals.icu Settings 的已授權應用程式裡撤銷——在這裡
  刪資料不會收回你在那邊給出的授權。
- 營運紀錄：它們只帶 request path 與拒絕原因，完全不含計畫、健康或身分內容。

**第二段：`applyOwnerDeletion`**——要帶回上一段的 proposal 與 `confirmed: true`。
預覽之後如果帳號又多了一版計畫或多了一筆回報，它會**拒絕執行**而不是照舊刪掉，要求
重新預覽。成功後回一張收據：`receipt_id`（`gcd-` 開頭）、`removed` 的逐項結果、以及
同樣那三條刪不到的清單。收據上沒有 owner id、沒有 fingerprint、沒有任何計畫內容——
一張刪除的稽核紀錄不該變成被刪內容的最後一份副本。

**不可回復，而且刪除之後那個帳號的位置會留一塊墓碑。** 刪除全程握著 owner maintenance
fence，最後一件事是把那面 fence 封成 deletion tombstone 並**永久留著**。原因很實際：
一個在刪除開始前就通過驗證、刪除結束後才抵達的寫入者，會把目錄重新建出來，而那時已經
沒有任何 credential 能再刪它一次。owner id 從不重複使用（重新連線會拿到全新的 owner），
所以永久 fence 擋住的正好只有必須擋住的那些寫入者。`doctor-store` 會在
`deletion_tombstone` 底下如實回報它。

刪除被拒的幾種情況與處理方式（未收斂的交付、預覽後又變動、正在搬遷）在
[docs/ops/privacy-requests.md](docs/ops/privacy-requests.md)。

---

## 已經有本機 state 的人怎麼搬過去

這是一次性的操作員動作，跑在**自己的機器與 gateway 主機**上，不是對話裡的步驟。順序是
**匯出 → 在空的 owner store 打開 → 驗證 → 把本機封存**。每一個會改動東西的指令，
**不帶 `--confirm` 就只是預覽**，把來源、目的地與會發生什麼原樣印出來：

~~~bash
# 1. 匯出整條 append-only 鏈（0600，原子寫入，不動來源）
python3 -m garmin_coach_loop.cli export-store --out ./coach-store-bundle.json

# 2. 在 gateway 主機上先看會發生什麼；--athlete-id 是自己的 Intervals athlete id，
#    用來從 identity registry 解析出目的地目錄——沒有任何指令收 owner id
python3 -m garmin_coach_loop.cli import-store \
  --bundle ./coach-store-bundle.json --athlete-id <intervals-athlete-id>

# 3. 確認後才真的打開
python3 -m garmin_coach_loop.cli import-store \
  --bundle ./coach-store-bundle.json --athlete-id <intervals-athlete-id> --confirm

# 4. 驗證 hosted 那份讀得到、版本對得上
python3 -m garmin_coach_loop.cli hosted-status --gateway https://mcp.paceandstaystrong.com

# 5. 把本機那份封存：讀得到、匯得出，但再也寫不進去
python3 -m garmin_coach_loop.cli seal-local-store \
  --hosted-entry https://mcp.paceandstaystrong.com --confirm
~~~

兩邊都有歷史時 `import-store` **直接 fail closed**，不覆蓋也不合併：

~~~text
{
  "status": "blocked",
  "error": "destination already holds state; importing is not merging. Archive it first
   (archive-store) if the imported history is meant to replace it."
}
~~~

要讓路就用 `archive-store` 把目的地整份移開——同樣是不帶 `--confirm` 先預覽。那是搬走
不是刪掉，移完還打得開，目錄名會帶上時間戳與你給的 `--reason`。

**搬遷全程握著 owner maintenance fence。** `archive-store`、`import-store`、`init-store`、
`adopt-owner-store` 都會取得 owner 目錄旁的 `<name>.maintenance`，而每一個寫入者都在
store 的 `.lock` 底下讀它：已經在鎖裡面的寫入者會讓搬遷在任何東西被移動之前就失敗；
之後才抵達的寫入者在寫任何東西之前被拒絕。fence 是 sibling 檔案，不進 commit 鏈、不進
bundle，舊版 checkout 打開 store 的行為完全不變。

⚠️ **fence 不是停機的替代品。** 它是單一檔案系統上的一次 O_EXCL create，而 Railway 的
`ssh` session 與服務容器是否共用同一個 volume，還沒有端到端驗證過。搬遷期間該做的
stop／drain 仍然照做——完整流程、rollback 與那條 caveat 在
[docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md)。

**搬遷不會帶走本機的 health database。** `--health-db` 是一條本機路徑，gateway 讀不到
任何人的檔案。搬過去之後，重訓執行紀錄由 `recordStrengthExecution`（你自己講）補上；
有本機 reader 的 Agent 可在每次 `startCoachSession` 傳七日 sanitized `recovery_signals`。
這不是搬 DB、也不是保存一份：下一次未重傳就回 `null`。歷史的那一半仍然開著（issue #101）。

---

## 目前做不到的事

直說，不迂迴：

- **不能上傳歷史 FIT 檔或任何原始活動檔。** 也不能匯入 Apple Health export。這是
  evidence 設計的 phase 2（phase 1 是對話裡直接講的體重與訓練摘要，已經上線），
  tracker 是 issue #140。
- **不直接讀 Apple Health / Apple Watch。** 資料要先進 Intervals.icu，Coach 才看得到。
- **沒有完整的歷史遷移。** 進到 provider 之前的訓練史，這個產品目前拿不到
  （issue #101）。
- **hosted 不會自己取得或保存 `recovery_signals`。** 只有本次對話的 client 已在本機讀到並
  傳入 sanitized values 時才有；換到沒有 local reader 的入口，或下一次沒有重傳，就是 unknown。
- **不保留 provider 原始 payload 與 GPS 軌跡。** 讀完就沒了，要那些請從 Intervals.icu
  自己匯出。
- **不做修正。** store 是 append-only 的，所以沒有「改掉某一筆歷史」這個操作；能做的
  是匯出或刪除。運動員自己講的那一層則相反——重講就是更正。
- **不能刪掉 provider 那一側的東西。** 日曆上的課、你給 Intervals 的授權，都要在
  Intervals.icu 自己處理。
- **不做醫療判斷。** 疼痛、生病、胸痛、頭暈或不尋常症狀，需要一個風險更低的人類決定。

---

## 出問題時

**credential 過期或被撤銷** — 下一個 tool call 回 `401` 加上 challenge，conforming 的
client 會自己重跑 OAuth。要診斷連線本身用 `inspectIntervalsPermissions`，它會當場各讀
一次 Settings 與日曆，回 `settings_read` 與 `calendar_read` 分類（`readable` = 200、
`denied` = 403、`invalid_or_expired` = 401）；同時附上的
`scopes_recorded_at_authorization` 是發證當下記下來的字串，不是 provider 現在認不認。
**被撤銷過的人重新連上之後立刻就能用**，不需要等任何冷卻。

**`calendar_read: denied`（讀得到設定，卻交付不了）** — intervals.icu 的同意頁上每一項
權限都是獨立勾選，日曆沒勾的連線讀活動與設定都正常，只有交付會失敗。請運動員重新連線
並勾選日曆，再把**同一份**已確認的配送送一次；步驟見
[docs/ops/restore-calendar-access.md](docs/ops/restore-calendar-access.md)。

**Settings 讀取或更新被拒絕** — 同一張同意頁的 Settings 也是獨立勾選。重新連線並勾選
Settings；若錯誤發生在已確認的 threshold pace 更新，可重送**同一份**配送，寫入路徑會先
讀回目前值，已經正確就不會再寫一次。

**`no_plan_state`（不知道計畫在哪）** — 這個帳號還沒有計畫，不是壞掉。走第一次對話的
初始化路徑；`startCoachSession` 同時會帶 `pre_plan_observations`，所以不會從零問起。
如果你確定應該要有計畫，先確認連的是不是同一個 Intervals 帳號——owner 是由那個帳號
解析的。

**交付中斷** — 產品在打出去之前就先把每個可能改動 Intervals 的動作寫進本地的交付
紀錄：哪一堂、寫入還是刪除、產品自己的 marker、拿到的 event id、走到哪一步。所以
「寫進去了但 read-back 不符」「刪掉了但確認不了」「程序死在請求中間」都留得下來。

- 只要還有未收斂的動作，交付紀錄就不解除：plan 不能改，`snapshot-store`、
  確認過的 `restore-store`、`adopt-owner-store --mode copy` 也一律拒絕。
- **正確做法是重試，不是還原。** 把**同一份**已確認的配送再送一次：已記進 plan 的略過，
  Intervals 可能已經有的先讀回來核對、不符就原地覆蓋，沒送成的才第一次寫入——都用同一個
  marker，不會生出第二則 event。
- 真的無法重試時，先去 Intervals 日曆確認實況，再用 `clearDeliveryAttempt`
  （CLI：`clear-delivery-attempt --confirm`）接手。它需要一次明確確認，**修不了任何東西**，
  只會列出你接手的是哪幾筆。
- 這期間 `startCoachSession` 照樣可讀，reconciliation 會標成 `deferred`：計畫是準確的，
  但某一堂已經練過的課可能暫時還讀作 planned。

**本機 store 被封存** — 寫入會拿到明確的訊息，指向 hosted 入口與封存時間：

~~~text
{
  "status": "blocked",
  "error": "record-profile is refused: this store was handed off to the hosted coach at
   https://mcp.paceandstaystrong.com on 2026-08-17T17:49:34Z, and the plan there is the
   current one. Read it through the hosted entry. To make this local store writable
   again -- knowing it will be a second plan for the same athlete -- run
   seal-local-store --release --confirm."
}
~~~

讀取、`history`、`doctor-store`、`export-store` 與 snapshot 全部照常。解除封存是一個明確
的 operator 動作，而且要知道那會讓同一個運動員有兩份計畫。

**deletion tombstone** — `doctor-store` 在 `deletion_tombstone` 底下回報它，這是資訊而
不是錯誤。它代表那個 owner id 的資料已經被刪除，而且那個位置**永遠**不會再被寫入。
同一個運動員重新連線會拿到全新的 owner id 與全新的計畫，不受影響。

**schema 不相容** — `doctor-store` 會重新驗證整條 commit 歷史，所以新版程式寫過的
store，舊版程式會**完全打不開**。`WRITER_CONTRACT_VERSION` 的守門在任何寫入之前就攔下
版本不符，並在這個 checkout 是較新的一側時先做 snapshot；`doctor-store` 會回報版本，
`restore-store` 是回復路徑。

---

## 使用體驗的前後差別

| | 之前 | 現在 |
| --- | --- | --- |
| 計畫住在哪 | 一台機器上的本機 store，換機器就斷 | hosted owner store 是 canonical；ChatGPT、claude.ai、本機 client 讀到同一份 |
| 換 client | 各自一份，可能互相打架 | 同一個 Intervals athlete 永遠解析到同一個 owner 與同一份計畫 |
| 開始使用 | CLI 設定、環境變數、手動初始化 | 在對話裡問一句，OAuth，看一次四週預覽，確認一次 |
| 看得到多遠 | 只有這一週 | 這一週精確 ＋ 接下來三週輪廓，一起在你確認的那份預覽裡 |
| 進步怎麼判斷 | 各憑印象 | cycle 自己指定一堂普通的課當基準，產品說兩個讀數在不在，教練給判讀 |
| 裝置沒錄到的事 | 沒地方講 | 體重、體脂、一場沒帶錶的訓練，講了就進；重講就更正 |
| 要自己的資料 | 找人 | 在對話裡直接要，`exportOwnerData` 當場給 |
| 要刪掉 | 找人 | 在對話裡直接刪：預覽 → 一次確認 → 收據，並且明講刪不到什麼 |
| 一個 athlete 兩份計畫 | 有可能，而且不會被發現 | 設了 hosted 就擋本機寫入；搬過去的那份會被封存到寫不進去 |
| 交付中斷 | 沒 receipt 就當沒發生 | 打出去之前就先記；未收斂就凍結計畫改動，重試同一份即可收斂 |

---

## 本次發布的資料與行為盤點

對照 issue #132 的初始盤點，重新對**已合併並部署**的內容清點過的結果：

| 分類 | 數量 | 內容 |
| --- | --- | --- |
| 新增的持久化資料形狀 | **8** | 本機 handoff 封條、per-owner 撤銷邊界、`cycle.outlook`、`goal.measurement`、`session.measures`、owner maintenance fence／deletion tombstone、`body_measurements`、`reported_activities` |
| 衍生／僅存在於回應的視圖 | **5** | `measurement_evidence`、owner data export archive、deletion preview、deletion receipt、`getCoachState` 摘要 |
| 可攜複本格式 | **1** | store bundle（`garmin-coach-loop-store-bundle` 1.0，本次改為原子且從第一個 byte 就 0600） |
| 使用者／操作員工作流群組 | **14** | issue #132 列的 12 組，加上「完全不寫的計畫讀取」與「回報體重與裝置沒錄到的訓練」 |

同時清點到的介面規模（都由測試從程式碼推導，不是手寫的數字）：**23 個 MCP tool**、
**1 個 orchestration prompt**、**30 個 CLI 指令**、**3 份 JSON Schema contract**、
**5 張 identity 表**。

逐項的細節——每一個形狀寫在哪個檔案、哪個 issue 帶進來的、匯出與刪除各自怎麼處理它——
在 [docs/release-inventory.md](docs/release-inventory.md)。

---

## Deterministic command surface

這些是 Coach Skill 使用的 implementation detail，不是要求使用者手工串接的工作流：

~~~bash
python3 -m garmin_coach_loop.cli doctor-store
python3 -m garmin_coach_loop.cli status
python3 -m garmin_coach_loop.cli history --help
python3 -m garmin_coach_loop.cli refresh-context --help
python3 -m garmin_coach_loop.cli record-profile --help
python3 -m garmin_coach_loop.cli record-availability --help
python3 scripts/render_plan_preview.py --help
python3 -m garmin_coach_loop.cli prepare-delivery --help
python3 -m garmin_coach_loop.cli approve-delivery --help
python3 -m garmin_coach_loop.cli publish-delivery --help
python3 -m garmin_coach_loop.cli clear-delivery-attempt --help
python3 -m garmin_coach_loop.cli prepare-withdrawal --help
python3 -m garmin_coach_loop.cli approve-withdrawal --help
python3 -m garmin_coach_loop.cli withdraw-delivery --help
python3 -m garmin_coach_loop.cli hosted-status --help
python3 -m garmin_coach_loop.cli hosted-session --help
python3 -m garmin_coach_loop.cli export-store --help
python3 -m garmin_coach_loop.cli import-store --help
python3 -m garmin_coach_loop.cli archive-store --help
python3 -m garmin_coach_loop.cli seal-local-store --help
python3 -m garmin_coach_loop.cli revoke-connections --help
~~~

`refresh-context` 取得最新資料、套用可靠對帳，並回傳與最新 current version 一致的
context。Delivery commands 將 approval 綁定精確 preview，並在同一條 deterministic
boundary 內完成 product-owned event 查重、寫入、read-back 與 state update。

每個 session 都帶一份 `plan`，用執行模型分類（`time_axis`／`movement_list`／
`unstructured`），由 `kind` 而不是 sport 決定怎麼驗證；跑課的 provider-neutral
`time_axis` 只存在於 current PlanState，`prepare-delivery` 只選 session_id，不能再提交
另一份 workout 改寫它。`plan` 是交付時唯一的 executable source；`prescription` 是它的
人類可讀 rendering，由 code 生成、不由 Coach 撰寫，也不授權 provider write。

已交付的課被改掉（移動、縮短、換成別的、換成休息）之後，Intervals 上那則舊 workout
不會自己消失。PlanState 會記下那個 event id，`status` 與 hosted session 都看得到；重新
交付新內容會直接覆蓋同一則 event（移動日期也是同一則，不會兩則），沒有東西可交付時則
用 withdraw 把它撤掉——撤掉要單獨確認一次，只動這個產品自己寫的 event，且不碰已經過去
的日期。

一次配送多堂課時，Intervals 沒有跨 event 的 transaction，產品也不假裝有：配送期間 plan
不能被改寫，中途失敗會保留已被 Intervals 接受並 read-back 通過的那幾堂（記進 current
state），並指名還沒送成的是哪一堂。

結構化交付接受 open target、有個人 baseline 支持的絕對配速，或 easy／recovery／long run
的心率上限。心率只能是上限，下限在 schema 中無法表達。

課表本身用 bpm 記上限，送到 Intervals 時改寫成 `% LTHR`：絕對 bpm 在目前 Garmin 實機
驗證中會變成 1-252 bpm，等於畫了一條不存在的線，所以已經移除。換算用的是 Intervals
帳號裡的 Run threshold HR，preview 會同時顯示百分比與它換算回來的 bpm，換算結果只會等於
或低於課表上限，不會四捨五入成更鬆的一條。帳號沒設 threshold HR 就在 preview 擋下來，
不會默默改成無目標；確認後 threshold HR 被改動也會擋，要求重新 preview。Read-back 核對
provider 自己解析出來的百分比，並換算回 bpm 再比一次上限。

---

## 產品與開發文件

以下皆為英文文件。

- 穩定使用者故事：[docs/user-story.md](docs/user-story.md)
- 資料來源逐欄位能與不能：[docs/data-sources.md](docs/data-sources.md)
- 本次發布的逐項盤點：[docs/release-inventory.md](docs/release-inventory.md)
- 帳號生命週期（連線／斷線／匯出／刪除）：[docs/account-lifecycle.md](docs/account-lifecycle.md)
- 入口與逐平台設定：[entrypoints/](entrypoints/README.md)
- MCP protocol、tool surface 與 OAuth：[entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- 公開上架送審材料與逐平台 checklist：[docs/distribution/](docs/distribution/README.md)
- 教練行為案例：[evals/README.md](evals/README.md)
- 部署與維運 runbook：[docs/ops/](docs/ops/verify-production-status.md)
- Repository invariants 與驗證：[AGENTS.md](AGENTS.md)

真實 credentials、健康資料、context、plan、approval、receipt 與 provider state
不得 commit；repository 只保留匿名 fixtures。
