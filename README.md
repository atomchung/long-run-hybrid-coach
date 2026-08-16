# Long Run Hybrid Coach

Long Run Hybrid Coach 是一個非官方、Garmin-first 的個人化 hybrid coach。它把最新
Intervals.icu／Garmin 訓練資料、目前計畫、實際完成狀態與生活限制，持續維護成
同一份 28 天方向與本週跑步＋重訓課表。

教練判斷由模型做，資料、對帳、驗證與交付由 deterministic code 做。完整的職責
邊界見 [AGENTS.md](AGENTS.md)。

它不是 Garmin 官方產品，也與 Garmin Ltd. 沒有隸屬或授權關係。

## 下一次怎麼開始

在 repository 中直接對 Codex 說：

> 根據最新資料重新評估我的目標與課表。

Canonical 入口只有
[Long Run Hybrid Coach Skill](.agents/skills/garmin-coach-loop/SKILL.md)；
行動端的 Custom GPT 入口（同一個 gateway、同一份 PlanState）見
[entrypoints/custom-gpt/](entrypoints/custom-gpt/README.md)。Skill 會：

1. 讀取唯一的 current PlanState，並在答案需要計畫本身沒有的證據時才取最新資料；
2. 自動寫回 identity-backed、可確定的 planned → actual 完成對帳；
3. 重新評估 28 天 primary／maintenance direction；
4. 建立本週可執行的跑步與重訓處方，不替缺失 baseline 捏造精度；
5. 週複盤或週期結束時，依序回答有沒有進步、實際練了什麼、身體怎麼回應、對照
   目標自己的 measurement protocol 得到什麼結果，以及接下來怎麼決定；
6. 第一屏先顯示目前目標、今天／本週做什麼，以及相較舊計畫真正重要的變更；
7. 對需要交付的 session 顯示一次精確 preview，確認後由 code 完成查重、
   寫入與 read-back verification。

正常使用不需要手工建立或修改 CoachContext、PlanState、DecisionEvent、
proposal 或 receipt JSON。

## 本機設定與 current state

Intervals.icu 是產品的預設資料來源，不需要 OpenAI API key。設定
INTERVALS_ICU_API_KEY 與 INTERVALS_ICU_ATHLETE_ID 後，私人 state 預設放在
~/.local/share/garmin-coach-loop；也可用 GARMIN_COACH_LOOP_HOME 指向另一個
repository 外的私人路徑。本機健康資料庫（`--health-db`，或與
`--source personal-os` 共用的環境變數）提供兩個選用的加強資料源——肌力執行
紀錄（strength_execution）與恢復訊號（recovery_signals：readiness／HRV
status／acute load／Body Battery／stress）；缺席時明確標記為 unknown，從不
阻塞。

運動員自己講出來、裝置量不到的事——人在哪個時區、課表用哪個語言、每週哪幾天能
練，以及自己回報的重訓組數與重量——存在同一個 state 目錄下的
`athlete-evidence.json`，下一次對話直接讀得到。都只需要講「變的那一件事」：常態
是一三五時，「這週三不能練」照樣留下一和五；重訓只要動作和組數，日期預設今天，
同一天同一個動作再講一次是更正而不是多做一組；時區與語言各自獨立，只講其中一個
不會清掉另一個。本機有 health.db 時，量到的紀錄優先，講出來的補上它沒有的動作。

已有 state 時，新計畫會成為同一個 append-only store 的唯一 current version；
不會另生成一套平行課表。第一次使用則在使用者選定 28 天方向後初始化 store。

## 時區與語言

「今天」與「下一堂課」一律由 athlete-local 時區決定，不會從伺服器或裝置所在地
推測。時區講一次就存起來（CLI `record-profile`、hosted `recordAthleteProfile`），
之後每次 build、session、status、withdrawal 都自己帶上，不用再講。順序是：這次
request 講的 > 存起來的 > 預設 `Asia/Taipei`——沒存過 profile 的既有 owner 行為
完全不變。CLI 的 `status`、`build-context`、`refresh-context` 與 hosted
`startCoachSession` 的 `timezone` 因此降級為單次 override（出差那一週用），給錯
時區名稱照樣直接回報一則明確錯誤，絕不悄悄退回預設值或用主機所在地代答。

同一份 profile 也記語言（`zh-Hant` 或 `en`）。它只換 prescription 那層自然語言外
殼——結構欄位、數字、以及動作的 `display_name`（本來就是運動員自己講的名字）都不
動。那句話會成為 Intervals event 的 description 與重訓日的標題，跟著同步鏈走到
手錶上，所以看得懂比較重要。已經寫好的課不會因為改語言被重寫，之後動到的才用新
語言重新生成。

## Deterministic command surface

這些是 Coach Skill 使用的 implementation detail，不是要求使用者手工串接的
工作流：

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
~~~

refresh-context 取得最新資料、套用可靠對帳，並回傳與最新 current version
一致的 context。Delivery commands 將 approval 綁定精確 preview，並在同一條
deterministic boundary 內完成 product-owned event 查重、寫入、read-back 與
state update。每個 session 都帶一份 `plan`，用執行模型分類（`time_axis`／
`movement_list`／`unstructured`），由 `kind` 而不是 sport 決定怎麼驗證；跑課的
provider-neutral `time_axis` 只存在於 current PlanState，prepare-delivery 只選
session_id，不能再提交另一份 workout 改寫它。`plan` 是交付時唯一的 executable
source；`prescription` 是它的人類可讀 rendering，由 code 生成、不由 Coach 撰寫，
也不授權 provider write。

已交付的課被改掉（移動、縮短、換成別的、換成休息）之後，Intervals 上那則舊
workout 不會自己消失。PlanState 會記下那個 event id，`status` 與 hosted session
都看得到；重新交付新內容會直接覆蓋同一則 event（移動日期也是同一則，不會兩則），
沒有東西可交付時則用 withdraw 把它撤掉——撤掉要單獨確認一次，只動這個產品自己
寫的 event，且不碰已經過去的日期。

一次配送多堂課時，Intervals 沒有跨 event 的 transaction，產品也不假裝有：配送
期間 plan 不能被改寫，中途失敗會保留已被 Intervals 接受並 read-back 通過的那幾
堂（記進 current state），並指名還沒送成的是哪一堂。

每一次可能改動 Intervals 的動作，在打出去之前就先寫進本地的配送紀錄——哪一堂、
是寫入還是刪除、產品自己的 marker、拿到的 event id、以及走到哪一步。所以「寫進
去了但 read-back 不符」「刪掉了但確認不了」「程序死在請求中間」都留得下來，不會
因為沒有 receipt 就被當成沒發生過。只要還有這種未收斂的動作，配送紀錄就不解除：
plan 不能改，snapshot-store、restore-store、adopt-owner-store --mode copy 也一律
拒絕，避免另一份 state 從此和同一本日曆各說各話。

重試就是把同一份已確認的配送再送一次：已記進 plan 的略過，Intervals 可能已經有
的先讀回來核對、不符就原地覆蓋，沒送成的才第一次寫入——都用同一個 marker，不會
生出第二則 event。已經寫進 plan、只差沒收尾的那種中斷，重試會直接回報成功。
doctor-store 與 status 都會顯示未收斂的動作；確認過 Intervals 日曆之後用
clear-delivery-attempt 解除，它會列出你接手的是哪幾筆。

結構化交付接受 open target、有個人 baseline 支持的絕對配速，或 easy／recovery／
long run 的心率上限。心率只能是上限，下限在 schema 中無法表達。

課表本身用 bpm 記上限，送到 Intervals 時改寫成 `% LTHR`：絕對 bpm 在錶端會變成
1-252 bpm，等於畫了一條不存在的線，所以已經移除。換算用的是 Intervals 帳號裡的
Run threshold HR，preview 會同時顯示百分比與它換算回來的 bpm，換算結果只會等於或
低於課表上限，不會四捨五入成更鬆的一條。帳號沒設 threshold HR 就在 preview 擋下來，
不會默默改成無目標；確認後 threshold HR 被改動也會擋，要求重新 preview。
Read-back 核對 provider 自己解析出來的百分比，並換算回 bpm 再比一次上限。

交付狀態只回報產品真正能觀察的證據，最遠到 intervals_accepted。Intervals
之後轉送到 Garmin Connect、再由裝置下載，是本產品目前無法逐筆觀察的外部 hop；
不得從 Intervals 成功推論 workout 已到 Garmin 或手錶。

## 產品與開發文件

- 穩定使用者故事：[docs/user-story.md](docs/user-story.md)
- 教練行為案例：[evals/README.md](evals/README.md)
- 當前進度與優先級：GitHub Issue #15
- Repository invariants 與驗證：[AGENTS.md](AGENTS.md)

真實 credentials、健康資料、context、plan、approval、receipt 與 provider state
不得 commit；repository 只保留匿名 fixtures。
