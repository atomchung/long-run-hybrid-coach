# Live Coach for Forerunner 570

Add **Live Coach** to a data screen in your Garmin **Run** activity. The field sends a sample to the Coach receiver when the activity timer is running, about once every five seconds. It shows how long ago the receiver last acknowledged a running sample. A phone with Garmin Connect and internet access provides the intended connection during a run.

## Build and install

Install the official [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/) and both Forerunner 570 device profiles with SDK Manager. Add its `bin` directory to your PATH or pass `--monkeyc`. Point `CIQ_SIGNING_KEY` to your local Garmin developer signing key (DER, outside any Git checkout). Commands below run from this directory.

Build with transmission disabled to check the toolchain and view the data field:

```sh
mkdir -m 700 -p "$HOME/Library/Caches/LiveCoachBuilds"
python3 -B build.py --output-dir "$HOME/Library/Caches/LiveCoachBuilds"
```

To pair a private sideload with a receiver, set `LIVE_COACH_ENDPOINT` to the complete HTTPS samples URL, ending in `/v1/live-run/samples`. Supply a dedicated write-only device token in `LIVE_COACH_DEVICE_TOKEN` from your local secret manager or a hidden shell prompt. Then run:

```sh
python3 -B build.py --provision --output-dir "$HOME/Library/Caches/LiveCoachBuilds"
unset LIVE_COACH_DEVICE_TOKEN
```

The script uses a private temporary project and writes one `LiveCoach.prg` per device. Each provisioned PRG contains the token. Keep that PRG private, rotate its token if it is lost, and rebuild when the receiver or token changes. A normal build always disables transmission. To target one size, pass `--device fr57042mm` or `--device fr57047mm`.

Use Garmin's USB sideload procedure to copy the PRG for your watch size into `GARMIN/APPS`, disconnect the watch, then add **Live Coach** to a data screen for **Run**. Start the activity timer. While running, **Sent 3s** means the receiver acknowledged the latest accepted sample three seconds ago. **Set up** means the binary has no usable configuration. **Sending** waits for a matching receipt, **Retry** follows a network or receipt failure, and **Check key** means the receiver refused the credential.

## Upload behavior

The source reads current heart rate (bpm), cadence (Garmin's raw rpm) and speed (m/s) from `Activity.Info`. Missing readings stay JSON null. The field requests the Communications permission and sends those three optional measurements with `session_id`, zero-based sequence number, UTC epoch observation time in whole seconds, and session state. Running samples also carry the timer, distance, altitude, ascent, descent, power and completed step count, so the receiver can tell a hill from a pace change. Paused/ended events carry null measurements.

Manual stop and Auto Pause keep the session ID. Restart or resume takes a fresh sample. Ending/resetting the activity sends an ended event; the next run receives a new ID. Pending running requests are cancelled when the timer stops. Transport cancellation is best effort, so the receiver must still enforce sequence ordering. The field limits uploads to one outstanding request, gives a request 20 seconds, and backs off for 5–30 seconds after failure. Only control events can retry, retaining their original timestamps, for at most 30 seconds while the field can run. Closing the activity or losing the phone link can prevent a final end receipt; the receiver should expire stale sessions.

A 2xx JSON receipt with a matching session ID, sequence number and `status` of `accepted` or `duplicate` marks delivery. `ignored`, malformed receipts and other failures leave the sample unconfirmed. A watch acknowledgement covers the upload to Coach. Earbud playback is a separate capability.

## Simulator and device checks

```sh
python3 -B build.py --test --output-dir "$HOME/Library/Caches/LiveCoachBuilds"
connectiq
monkeydo "$HOME/Library/Caches/LiveCoachBuilds/fr57042mm/LiveTest.prg" fr57042mm -t
monkeydo "$HOME/Library/Caches/LiveCoachBuilds/fr57047mm/LiveTest.prg" fr57047mm -t
```

For a structured workout, the field label changes to **Done 0** on Garmin's workout-start callback and counts workout-step-complete callbacks. The value under the label still reports upload status. This count is local to the watch and does not identify the current step or the workout. On a three-step workout, compare the count with the native workout screen after one automatic transition and one manual skip. Record what actually happened on the watch, including if a transition produced no callback.

Tests replace the network sender with an in-memory capture. On the paired watch, verify samples before/after manual pause, Auto Pause, save and the next run. Check a missing sensor value, phone disconnection/reconnection, and return to a locked iPhone. Compare raw running cadence with the native display before showing steps per minute. Phone background delivery, network latency, and the lifecycle of a real activity require that device check.

[Garmin DataField](https://developer.garmin.com/connect-iq/api-docs/Toybox/WatchUi/DataField.html) · [Activity measurements](https://developer.garmin.com/connect-iq/api-docs/Toybox/Activity/Info.html) · [Communications](https://developer.garmin.com/connect-iq/api-docs/Toybox/Communications.html#makeWebRequest-instance_function)
