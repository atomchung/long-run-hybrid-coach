# 跑完回顧：手錶直送 Coach

在原生「跑步」加上 Connect IQ 資料欄位 **Coach**（商店名稱 Long Run Hybrid Coach）並配對後，計時進行中錶約每五秒經手機的 Garmin Connect 送一筆樣本到 hybrid Coach gateway。停錶後問 Coach，`startCoachSession` 與 `getCoachState` 的結果多一個 `watch_run`：開始時間、計時與移動時間、距離、平均配速、平均與最高心率、平均步頻、累積爬升與下降、前三分之一對後三分之一、停下或走路的段落、每 10 分鐘一列，以及錶記到完成的課表步驟數。不用等 Intervals.icu 同步。

## 配對

不經過 Intervals.icu，也不用設定任何環境變數；每個帳號自己配對自己的錶。

1. 錶上的欄位沒有憑證時，用自己產生的密鑰向 `POST /v1/watch/pairings` 要一組 8 碼代碼並顯示出來。代碼 10 分鐘內有效、只能用一次。
2. 運動員在 Coach 對話裡講出代碼，模型呼叫 `connectWatch`，代碼綁到這條連線的帳號。帳號標籤是 Intervals 個人資料裡的 email，遮罩後才存（`t***@gmail.com`）；讀不到 email 就不存標籤，配對照樣成立。
3. 錶每 5 秒用代碼加密鑰輪詢 `POST /v1/watch/pairings/poll`，拿到 `issued` 時收下只能上傳樣本的憑證，存進 `Application.Storage`，顯示「已連接」加遮罩 email。
4. 一個帳號一支錶：配對另一支錶，或同一支錶重配，舊憑證就失效。舊錶上傳收到 `401`，回到顯示代碼。

沒有憑證就能打的兩條配對路由和上傳路由都按來源位址限速（Railway 邊緣代理設定的 `X-Real-IP`），上傳另外按帳號限速；超過回 `429` 加 `Retry-After`。一個帳號講代碼也有次數上限。配對與裝置認證失敗寫進安全事件，見 [security-events.md](security-events.md)。

## 邊界

- 只有 hybrid 且開著健康資料的部署會配對。其他部署（training 也就是 Plugin 版，不管健康資料開或關）沒有 `connectWatch`，三條手錶路由都回 404，讀取永遠不帶 `watch_run`。
- 配對過錶的帳號，讀取一定帶 `watch_run`，沒有跑步時是 `null`；沒配對過的帳號沒有這個欄位，讀取和以前一樣。和 Intervals 授權了哪些權限無關。
- 只在記憶體：最後一筆樣本後 24 小時清除，下一趟取代上一趟，服務重啟或重新部署也清除。不寫進 store，也不在 owner 匯出裡。錶送樣本與 Coach 讀取必須落在同一個程序，所以服務只能有一個副本、一個程序。
- `watch_run` 放在回答旁邊，不在 context 裡：週量、課表對實際、送達都只讀 Intervals。同一趟同步到 Intervals 後也會出現在實際紀錄，`watch_run.note` 會告訴模型這件事。
- 身分登錄表存的是代碼、錶密鑰、裝置憑證的金鑰指紋，加上遮罩 email、配對時間與最後上傳時間（每 10 分鐘最多更新一次）。owner 匯出列出配對筆數；刪除帳號一起刪掉，刪除預覽用固定字樣 `watch_pairing_removed` 說明而不計數，因為過期的配對紀錄會被下一支來要代碼的錶清掉。
- 停下：計時暫停（手動或 Auto Pause），或計時中速度低於 0.3 m/s。走路：步頻低於這趟移動中步頻中位數的八成，所以慢跑配速或用跑步步頻爬坡都不算。段落至少 10 秒才列出。
- 步頻換算成每分鐘步數：錶回傳的原始值不超過 100 時視為單腳而加倍。

## 上線前

- 舊的單人設定（`GARMIN_COACH_LOOP_LIVE_RUN_OWNER_ID`、`GARMIN_COACH_LOOP_LIVE_RUN_TOKEN_SHA256`、`GARMIN_COACH_LOOP_PILOT_ATHLETE_SHA256`）已經不讀了；服務上若還設著，部署前刪掉。
- 用舊建置（把金鑰編進 PRG 的版本）的錶，部署後上傳都會收到 401。換裝新建置，在對話裡講一次代碼。

## 真機驗收

1. 裝上新建置，在「跑步」的資料頁加上 **Coach** 欄位，開啟「跑步」但先不要按開始。
2. 欄位顯示代碼。在 Coach 對話裡說「連接我的手錶 <代碼>」，確認回覆說了欄位送哪些數值、保留 24 小時、怎麼停止。
3. 幾秒內欄位改成「已連接」加遮罩 email，確認是自己的 email。
4. 跑一趟，停錶並儲存，然後問 Coach「回顧剛剛這趟」。
   - `watch_run.state` 是 `ended`，`started_at` 對得上錶上的開始時間，`distance_km` 與 `timer_minutes` 和錶上的總結一致。
   - 錶上總結的平均步頻和 `average_steps_per_minute` 接近；後者只算移動中的樣本。差到一倍表示錶回傳原始步頻的方式和 `live_run._steps_per_minute` 假設的不同，要回頭改它。
5. 等 Intervals 同步後再問一次，確認模型把兩邊當成同一趟，沒有加總兩次。
