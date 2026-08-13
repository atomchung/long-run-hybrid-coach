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
[Garmin Coach Loop Skill](.agents/skills/garmin-coach-loop/SKILL.md)；
行動端的 Custom GPT 入口（同一個 gateway、同一份 PlanState）見
[entrypoints/custom-gpt/](entrypoints/custom-gpt/README.md)。Skill 會：

1. 讀取唯一的 current PlanState 與最新可用資料；
2. 自動寫回 identity-backed、可確定的 planned → actual 完成對帳；
3. 重新評估 28 天 primary／maintenance direction；
4. 建立本週可執行的跑步與重訓處方，不替缺失 baseline 捏造精度；
5. 第一屏先顯示目前目標、今天／本週做什麼，以及相較舊計畫真正重要的變更；
6. 對需要交付的跑步 workout 顯示一次精確 preview，確認後由 code 完成查重、
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

已有 state 時，新計畫會成為同一個 append-only store 的唯一 current version；
不會另生成一套平行課表。第一次使用則在使用者選定 28 天方向後初始化 store。

## Deterministic command surface

這些是 Coach Skill 使用的 implementation detail，不是要求使用者手工串接的
工作流：

~~~bash
python3 -m garmin_coach_loop.cli doctor-store
python3 -m garmin_coach_loop.cli status
python3 -m garmin_coach_loop.cli refresh-context --help
python3 scripts/render_plan_preview.py --help
python3 -m garmin_coach_loop.cli prepare-delivery --help
python3 -m garmin_coach_loop.cli approve-delivery --help
python3 -m garmin_coach_loop.cli publish-delivery --help
~~~

refresh-context 取得最新資料、套用可靠對帳，並回傳與最新 current version
一致的 context。Delivery commands 將 approval 綁定精確 preview，並在同一條
deterministic boundary 內完成 product-owned event 查重、寫入、read-back 與
state update。每堂可交付跑課的 provider-neutral `structured_workout` 只存在於
current PlanState；prepare-delivery 只選 session_id，不能再提交另一份 workout 改寫它。
`structured_workout` 是交付時唯一的 executable source；`prescription` 只是人類可讀
摘要，不授權 provider write。歷史 PlanState 可以沒有 `structured_workout` 以維持
append-only 相容性，但這類 session 在 prepare-delivery 會 fail closed，不能被送出。

結構化交付接受 open target、有個人 baseline 支持的絕對配速，或 easy／recovery／
long run 的絕對 BPM 心率上限。心率只能是上限，下限在 schema 中無法表達。
Read-back 核對 provider 實際強制的 bpm 本身，不只看 event 是否存在。

交付狀態只回報產品真正能觀察的證據，最遠到 intervals_accepted。Intervals
之後轉送到 Garmin Connect、再由裝置下載，是本產品目前無法逐筆觀察的外部 hop；
不得從 Intervals 成功推論 workout 已到 Garmin 或手錶。

## 產品與開發文件

- 穩定使用者故事：[docs/user-story.md](docs/user-story.md)
- Repository invariants 與驗證：[AGENTS.md](AGENTS.md)

真實 credentials、健康資料、context、plan、approval、receipt 與 provider state
不得 commit；repository 只保留匿名 fixtures。
