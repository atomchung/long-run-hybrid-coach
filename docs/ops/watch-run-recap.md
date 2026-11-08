# 跑完回顧：手錶直送 Coach

在 Forerunner 570 原生「跑步」加上 Connect IQ 資料欄位 **Live Coach** 後，計時進行中錶約每五秒經手機的 Garmin Connect 送一筆樣本到 hybrid Coach gateway。停錶後問 Coach，`startCoachSession` 與 `getCoachState` 的結果多一個 `watch_run`：開始時間、計時與移動時間、距離、平均配速、平均與最高心率、平均步頻、累積爬升與下降、前三分之一對後三分之一、停下或走路的段落、每 10 分鐘一列，以及錶記到完成的課表步驟數。不用等 Intervals.icu 同步。

## 邊界

- 只給一個 owner：`GARMIN_COACH_LOOP_LIVE_RUN_OWNER_ID` 指定的那位，而且該連線記錄的權限要有 `ACTIVITY:READ`。其他帳號的結果沒有這個欄位。
- 只有 hybrid 且開著健康資料的部署能設定。training（Plugin 版）設定了會拒絕啟動；它的 `/v1/live-run/samples` 回 404，讀取永遠不帶 `watch_run`。
- 只在記憶體：最後一筆樣本後 24 小時清除，下一趟取代上一趟，服務重啟或重新部署也清除。不寫進 store，也不在 owner 匯出裡。錶送樣本與 Coach 讀取必須落在同一個程序，所以服務只能有一個副本、一個程序。
- `watch_run` 放在回答旁邊，不在 context 裡：週量、課表對實際、送達都只讀 Intervals。同一趟同步到 Intervals 後也會出現在實際紀錄，`watch_run.note` 會告訴模型這件事。
- 停下：計時暫停（手動或 Auto Pause），或計時中速度低於 0.3 m/s。走路：步頻低於這趟移動中步頻中位數的八成，所以慢跑配速或用跑步步頻爬坡都不算。段落至少 10 秒才列出。
- 步頻換算成每分鐘步數：錶回傳的原始值不超過 100 時視為單腳而加倍。

私人試跑服務另外可以設 `GARMIN_COACH_LOOP_PILOT_ATHLETE_SHA256`（Intervals athlete ID 的 SHA-256 小寫十六進位），讓那個服務只接受這一個帳號授權。正式服務不設它。

## 啟用

前提：該帳號已經在這個 gateway 完成過一次 OAuth 連線。

1. 查 owner UUID。這是唯讀查詢，正式服務的 state root 是 `/data`：

   ```bash
   railway ssh "python3 -c 'from garmin_coach_loop.identity import owner_for_provider_athlete as o; print(o(\"/data/identity.db\", \"intervals\", \"<Intervals athlete ID>\"))'"
   ```

2. 在本機產生錶的寫入金鑰，輸出到 Git 以外、還不存在的私人目錄。金鑰只在這台機器產生，gateway 只拿到它的 SHA-256：

   ```bash
   python3 scripts/provision_live_run_pilot.py --owner-id "<owner UUID>" \
     --gateway-url "https://<gateway 網域>" --output-dir "<私人目錄>"
   ```

   能直接讀到 state root 的環境（例如本機測試 gateway）也可以改用 `--state-root` 加 `--athlete-id`。

3. 把 `gateway.env` 的兩個變數設到服務上，照 [roll-with-railway-cli.md](roll-with-railway-cli.md) 的順序部署，再照 [verify-production-status.md](verify-production-status.md) 讀 `/readyz`。
4. 載入 `watch.env`（`set -a; . "<私人目錄>/watch.env"; set +a`），照[手錶建置指引](../../entrypoints/garmin-live/README.md)建 PRG 並側載到錶。PRG 含金鑰，跟兩個 env 檔一樣只放在 Git 以外。

停用：移除兩個變數並重新部署，樣本跟著清除，錶上的金鑰也失效。換金鑰就重做步驟 2 到 4。

## 真機驗收

跑一趟，停錶並儲存，然後問 Coach「回顧剛剛這趟」。

- `watch_run.state` 是 `ended`，`started_at` 對得上錶上的開始時間，`distance_km` 與 `timer_minutes` 和錶上的總結一致。
- 錶上總結的平均步頻和 `average_steps_per_minute` 接近；後者只算移動中的樣本。差到一倍表示錶回傳原始步頻的方式和 `live_run._steps_per_minute` 假設的不同，要回頭改它。
- 等 Intervals 同步後再問一次，確認模型把兩邊當成同一趟，沒有加總兩次。
