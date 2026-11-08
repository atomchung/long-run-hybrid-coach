# Coach data field (Long Run Hybrid Coach)

A Connect IQ data field for the full Coach product. Add **Coach** to a data screen of a
running activity. While the timer runs it sends a sample about every five seconds through
Garmin Connect on the phone, so the Coach can recap the run as soon as the watch stops,
without waiting for Garmin → Intervals.icu. What it sends and how long it is kept:
[docs/ops/watch-run-recap.md](../../docs/ops/watch-run-recap.md).

## Pairing

Nothing secret is in a build. Unpaired, before a run starts, the field asks the Coach
server for a code and shows it under **Code** (配對碼), for example `K7P4 QX9A`. The athlete
tells the Coach in their AI chat "connect my watch K7P4QX9A" (連接我的手錶 K7P4QX9A), and the
`connectWatch` tool binds the code to the account of that conversation. The field, polling
every five seconds with a secret only it holds, then receives a credential that can only
upload that account's runs, keeps it in `Application.Storage`, and shows **Connected**
(已連接) with the account's masked email.

- A code lasts ten minutes and works once. The field shows at most three codes each time
  the activity opens, then **Not connected** (未連接) until the next time. Without the phone
  it keeps retrying, with growing waits.
- Paired, before the timer starts, the field shows whose account it sends to. During the
  run it shows how the upload is going: **Sent 3s** (已送 3s) means the server acknowledged
  the latest sample three seconds ago; **Sending**, **Retry**, **Paused**, **Pause sent**,
  **End pending** and **End sent** follow the timer.
- Pairing another watch to the account retires the old one: its next upload is refused
  (401), and the field forgets its credential and shows a new code before the next run. A
  paired field shows no code, so moving this watch to another account means uninstalling
  the field and installing it again.
- Removing the field from the data screens, or uninstalling it, stops everything.

## Build

Install the [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/) and the device
profiles with SDK Manager. `monkeyc` needs Java; SDK Manager's own runtime works:
`export JAVA_HOME="$HOME/Library/Application Support/Garmin/ConnectIQ/Toolchains/temurin-17-jre/Contents/Home"`.
Point `CIQ_SIGNING_KEY` at a DER signing key outside any Git checkout. The Store package
must be signed with the developer account's own key, and every later update with the same
one: keep that key backed up. Commands below run from this directory.

```sh
mkdir -m 700 -p "$HOME/Library/Caches/CoachFieldBuilds"
# Sideload binaries for the Forerunner 570 (both sizes), against production
python3 -B build.py --output-dir "$HOME/Library/Caches/CoachFieldBuilds"
# Against a test deployment
python3 -B build.py --server https://staging.example.com --output-dir "$HOME/Library/Caches/CoachFieldBuilds"
# The Connect IQ Store package for every listed watch
python3 -B build.py --package --output-dir "$HOME/Library/Caches/CoachFieldBuilds"
# Check the manifest's watches against the installed device profiles
python3 -B build.py --output-dir "$HOME/Library/Caches/CoachFieldBuilds" \
  --check-devices "$HOME/Library/Application Support/Garmin/ConnectIQ/Devices"
```

A sideload build writes `Coach.prg` per device; copy the one for your watch into
`GARMIN/APPS` over USB. `--device` (repeatable) picks other watches.

The manifest lists every watch whose profiles run data fields at API level 5.0 or later
(the first level that lets a data field send from the foreground): 76 profiles in SDK
9.3.0, none of them bike computers or handhelds. On API 5.2 and later, the manifest's
activity filter for running lets the install offer the field for runs. Only the Forerunner 570 has run
it on a real device.

## Upload behavior

The source reads current heart rate (bpm), cadence (Garmin's raw rpm) and speed (m/s) from
`Activity.Info`. Missing readings stay JSON null. Running samples also carry the timer,
distance, altitude, ascent, descent, power and completed step count; no location. Each
sample carries `session_id`, a zero-based sequence number, UTC epoch observation time in
whole seconds, and session state. Paused and ended events carry null measurements.

Manual stop and Auto Pause keep the session ID. Restart or resume takes a fresh sample.
Ending or resetting the activity sends an ended event; the next run gets a new ID. Pending
running requests are cancelled when the timer stops; cancellation is best effort, so the
server still enforces sequence ordering. One request is outstanding at a time, gets 20
seconds, and failures back off for 5 to 30 seconds. Only control events retry, keeping
their original timestamps, for at most 30 seconds.

A 2xx JSON receipt with a matching session ID, sequence number and `status` of `accepted`
or `duplicate` marks delivery. `ignored`, malformed receipts and other failures leave the
sample unconfirmed.

For a structured workout, the label changes to **Done 0** (完成 0) on Garmin's
workout-start callback and counts workout-step-complete callbacks. The count is local to
the watch and does not identify the current step or the workout.

## Simulator and device checks

```sh
python3 -B build.py --test --output-dir "$HOME/Library/Caches/CoachFieldBuilds"
connectiq
monkeydo "$HOME/Library/Caches/CoachFieldBuilds/fr57042mm/CoachTest.prg" fr57042mm -t
```

Tests replace the network and storage with in-memory captures. On a paired watch, check
samples before and after manual pause, Auto Pause, save and the next run; a missing sensor
value; the phone disconnecting and reconnecting; and a locked iPhone. Compare raw running
cadence with the native display before trusting steps per minute.

[Garmin DataField](https://developer.garmin.com/connect-iq/api-docs/Toybox/WatchUi/DataField.html) · [Activity measurements](https://developer.garmin.com/connect-iq/api-docs/Toybox/Activity/Info.html) · [Communications](https://developer.garmin.com/connect-iq/api-docs/Toybox/Communications.html#makeWebRequest-instance_function)
