# 跑步中的 Coach（Forerunner 570 私人試跑）

在手錶用平常的「跑步」記錄活動，配對的手機透過 Garmin Connect 把目前樣本傳到同一個 hybrid Coach gateway。手機需要網路。第一次請只跑這條資料路徑，再另外驗證語音用戶端能否靠同一筆樣本開口。

## 啟用單一試跑帳號

使用有 HTTPS 的私人測試 gateway，配置單一程序與單一副本。它必須已執行此版本，且試跑者已經在**該 gateway** 連結過自己的帳號。分別部署的兩個 Coach 有不同身份資料庫與記憶體。請只在對應的 gateway 讀取，並使用其原有連線，連線需有活動讀取權限。正式服務、training 產品和 Garmin Connect 商店都不是建置步驟。

私人單人服務先設定 `GARMIN_COACH_LOOP_PILOT_ATHLETE_SHA256` 為該 Intervals athlete ID 原字串（UTF-8，無換行）的 SHA-256 小寫十六進位值，再開放本人授權。Gateway 以 Intervals 在 OAuth 交換時回傳的帳號識別比對，拒絕其他帳號。將測試服務的精確 HTTPS `/oauth/callback` 加到 Intervals OAuth 應用的 redirect 清單；試跑只讀運動資料時可在授權請求指定 `scope=ACTIVITY:READ`。

在該 gateway 的管理環境中執行以下指令。state root 是 gateway 的資料根，athlete ID 是本人已連結的 Intervals 帳號。指定一個尚不存在、位於 Git 以外的私有目錄。

```sh
python3 scripts/provision_live_run_pilot.py \
  --state-root "$GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT" \
  --athlete-id "$PILOT_ATHLETE_ID" --gateway-url "$PILOT_HTTPS_ORIGIN" \
  --output-dir "$PILOT_PRIVATE_DIR"
```

1. 將 `gateway.env` 的兩個設定交給該 gateway 的祕密管理機制，再重新啟動。伺服器只需寫入金鑰的雜湊和已註冊帳號的內部 ID。未啟用時，裝置傳送路徑回 404。
2. 在編譯 Mac 載入 `watch.env`（`set -a; . "$PILOT_PRIVATE_DIR/watch.env"; set +a`），用[手錶建置指引](../../entrypoints/garmin-live/README.md)產生對應尺寸的 PRG。已配對的 PRG 含這支裝置的寫入金鑰，只交給試跑者。所有 PRG、金鑰及環境檔都留在 Git 以外。
3. 如需停止試跑，移除 gateway 的兩個設定並重啟。這同時撤銷手錶寫入並清除記憶體中的樣本。要換金鑰，重新產生一組並更新 gateway 和 PRG。

## 第一次真機驗收

1. 在 570 原生「跑步」新增 Connect IQ 資料欄位 **Live Coach**，將 iPhone 的 Garmin Connect 與錶連上。在錶上開始活動。每約五秒嘗試送出一筆；欄位收到與本次運動及序號相符的伺服器回執才會顯示 **Sent**。
2. 用已連結該 gateway 的 Coach 對話問「用 getCoachState 看我這趟跑步目前的心率與步頻，告訴我讀數是多少秒前的」。也可使用現有 CLI 的 `hosted-status`。即使尚無課表，也可回傳 `live_run`。比對同時刻錶上的心率及步頻，再自然改變腳步，看連續幾筆是否跟著變化。將錶上 steps/min 與 SDK 原始 rpm 對照過再換算；速度單位是 m/s。
3. 將 iPhone 鎖屏一分鐘，從另一個已連結同一帳號的客戶端再讀一次。驗收要求是鎖屏期間採樣時間、序號持續往前，讀到的樣本不超過 15 秒；同一筆舊樣本重送不算更新。停止廣播或斷開手機後再讀，確認 Coach 不再給出當下心率。暫停、恢復、結束各檢查一次。真機可能在最後一筆網路請求完成前卸載資料欄位，這時以過期狀態為準。

`getCoachState` 可多次讀取最新數值，無須向 Intervals 要求跑後記錄。伺服器只在記憶體保留當前這趟的樣本（約每五秒一筆，沒有 GPS），用來算 `trend`：最近 1、3、10 分鐘與每 10 分鐘的心率、配速、步頻、爬升和坡度。這些只是算術，判斷交給模型。暫停、結束或過期時即時數值都會清空；超過五分鐘無新資料時，整趟樣本清除，查詢回覆 `no_recent_sample`。重啟服務也會清除樣本。

## 不開口也能陪跑

設定 `GARMIN_COACH_LOOP_LIVE_RUN_HOLD_SECONDS` 後，跑步期間連續呼叫 `getCoachState` 會等到距離上次回覆滿這個秒數才回來，並在結果裡要模型看完就再呼叫一次、沒事不要出聲。這樣用戶端不需要計時，也不用跑者開口。`GARMIN_COACH_LOOP_LIVE_RUN_BRIEF` 是今天的課與目的，隨結果交給模型，內容屬於試跑者，只放在服務的環境變數。這是語音用戶端能否主動開口的實驗，只在這個私人服務開啟。
