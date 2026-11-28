# Connect IQ Store listing

The text and choices the developer dashboard asks for once an `.iq` validates. The beta and
the store app use the same listing. The upload form shows image sizes only after an `.iq`
validates, so the images below the text are added then. The
icons, and `resources/drawables/launcher.png` (70 x 70, the largest launcher size among the
listed watches; the resource compiler scales it down for the rest), come from
`store_assets.py` beside the sprite generator.

- **Developer display name:** chosen in the dashboard; it may not contain spaces.
- **App name:** Long Run Hybrid Coach
- **Activity types:** Running.
- **Requires payment:** no.
- **Privacy policy:** https://paceandstaystrong.com/privacy.html#watch-field
- **Support email (public):** the address on https://paceandstaystrong.com/support.html
- **Website:** https://paceandstaystrong.com

## Description (English)

Ask your AI coach about the run the moment you stop the watch.

Coach is a running data field. While you run it sends your run to Long Run Hybrid Coach, so the
coach in your AI chat can recap it as soon as you stop, without waiting for the activity to sync.

On the watch:
- your heart-rate zone as an arc, from your own Garmin zones
- heart rate, pace, time and distance
- a one-eyed mascot that runs along a trail at your speed, walks when you walk, cheers at every
  kilometre or mile, and shows a gold check when you finish today's Coach workout

What you need:
- The full Long Run Hybrid Coach connector (mcp.paceandstaystrong.com) added to an AI chat app
  such as Claude or ChatGPT. Sign-in uses an Intervals.icu account. Setup:
  paceandstaystrong.com. The ChatGPT Plugin edition does not pair with the watch.
- The Garmin Connect app on your phone during the run. Without it nothing is sent, and the run
  reaches the coach the usual way, through Intervals.icu.

Pairing: add Coach as a data screen in Run. Before your first run it shows an 8-character code.
Tell the coach "connect my watch" and the code.

What is sent: about every five seconds while the timer runs, heart rate, cadence, speed, time,
distance, altitude, ascent, descent, power and completed workout steps. No location. The service
keeps only your latest run, for 24 hours. Privacy: paceandstaystrong.com/privacy.html

Tested on Forerunner 570. Not a medical device. Long Run Hybrid Coach is an independent project
and is not affiliated with Garmin.

Support: paceandstaystrong.com/support.html

## 說明（繁體中文）

停錶那一刻，就能問 AI 教練這一趟跑得怎麼樣。

Coach 是跑步用的資料欄位。跑步時它把這一趟送到 Long Run Hybrid Coach，停錶後在 AI 對話裡就能請教練回顧，
不用等活動同步。

手錶上會看到：
- 心率區間圓弧，依你自己在 Garmin 設定的區間
- 心率、配速、時間與距離
- 一隻獨眼小怪照你的速度在山徑上跑：你走它就走，每公里（或每英里）為你歡呼，完成今天的 Coach 課表時亮出金色勾勾

需要準備：
- 在 Claude、ChatGPT 等 AI 對話 App 加入完整版 Long Run Hybrid Coach 連接器（mcp.paceandstaystrong.com），
  登入要用 Intervals.icu 帳號。設定步驟：paceandstaystrong.com。ChatGPT Plugin 版不能配對手錶。
- 跑步時手機上要有 Garmin Connect App。沒有手機就不會送出，這一趟照原本的方式經 Intervals.icu 到教練那裡。

配對：在「跑步」加一頁 Coach 資料欄位。第一次跑步前它會顯示 8 碼代碼，在對話裡跟教練說「連接我的手錶」加上代碼。

送出什麼：計時進行中約每五秒一筆，心率、步頻、速度、時間、距離、海拔、爬升、下降、功率與完成的課表步驟數。
不送位置。服務只保留你最近的一趟，24 小時。隱私說明：paceandstaystrong.com/zh/privacy.html

已在 Forerunner 570 實測。非醫療器材。Long Run Hybrid Coach 是獨立專案，與 Garmin 沒有隸屬關係。

客服：paceandstaystrong.com/zh/support.html
