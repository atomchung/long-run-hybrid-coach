import Toybox.Activity;
import Toybox.Application;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.WatchUi;

class LiveCoachApp extends Application.AppBase {
    private var field;

    function initialize() {
        AppBase.initialize();
    }

    function getInitialView() {
        field = new LiveCoachField();
        return [field];
    }

    function onStop(state) {
        if (field != null) {
            field.finish();
        }
    }
}

// Unpaired, before a run starts, the field shows a code to say in the Coach conversation.
// Paired, it shows whose account it joined until the timer starts, then how the upload of
// the run is going.
class LiveCoachField extends WatchUi.SimpleDataField {
    static const SAMPLES_PATH = "/v1/live-run/samples";

    private var server = null;
    private var pairing = null;
    private var upload = null;
    private var text;
    private var allowRunning = true;
    private var completedSteps = 0;
    private var workout = false;

    function initialize() {
        SimpleDataField.initialize();
        text = {
            "Label" => WatchUi.loadResource(Rez.Strings.Label),
            "Code" => WatchUi.loadResource(Rez.Strings.Code),
            "Connected" => WatchUi.loadResource(Rez.Strings.Connected),
            "Connecting" => WatchUi.loadResource(Rez.Strings.Connecting),
            "NotConnected" => WatchUi.loadResource(Rez.Strings.NotConnected),
            "Set up" => WatchUi.loadResource(Rez.Strings.SetUp),
            "Ready" => WatchUi.loadResource(Rez.Strings.Ready),
            "Sending" => WatchUi.loadResource(Rez.Strings.Sending),
            "Sent" => WatchUi.loadResource(Rez.Strings.Sent),
            "Retry" => WatchUi.loadResource(Rez.Strings.Retry),
            "Paused" => WatchUi.loadResource(Rez.Strings.Paused),
            "Pause sent" => WatchUi.loadResource(Rez.Strings.PauseSent),
            "End pending" => WatchUi.loadResource(Rez.Strings.EndPending),
            "End sent" => WatchUi.loadResource(Rez.Strings.EndSent),
            "Done" => WatchUi.loadResource(Rez.Strings.Done)
        };
        label = text["Label"];
        var origin = COACH_SERVER;
        if (origin.find("https://") == 0 && origin.length() > 8
                && !origin.substring(origin.length() - 1, origin.length()).equals("/")) {
            server = origin;
            pairing = newPairing(origin);
        }
    }

    function newPairing(origin) {
        return new Pairing(origin);
    }

    function newUpload(endpoint, credential) {
        return new LiveUpload(endpoint, credential);
    }

    // The uploader for the paired account, or null while there is none.
    private function uploader() {
        var credential = pairing.credential();
        if (credential == null) {
            upload = null;
        } else if (upload == null) {
            upload = newUpload(server + SAMPLES_PATH, credential);
        }
        return upload;
    }

    function compute(info) {
        if (pairing == null) {
            label = text["Label"];
            return text["Set up"];
        }
        var now = Time.now().value();
        var ticks = System.getTimer();
        var timer = info.timerState;
        var up = uploader();
        if (up == null) {
            return unpaired(timer == Activity.TIMER_STATE_OFF, ticks);
        }
        if (timer == Activity.TIMER_STATE_OFF) {
            up.changeTo("ended", now, ticks);
        } else if (timer == Activity.TIMER_STATE_STOPPED || timer == Activity.TIMER_STATE_PAUSED) {
            up.changeTo("paused", now, ticks);
        } else if (timer == Activity.TIMER_STATE_ON && allowRunning) {
            var profile = Activity.getProfileInfo();
            if (profile != null && profile.sport == Activity.SPORT_RUNNING) {
                up.setExtra({
                    "timer_ms" => info.timerTime,
                    "distance_m" => info.elapsedDistance,
                    "altitude_m" => info.altitude,
                    "ascent_m" => info.totalAscent,
                    "descent_m" => info.totalDescent,
                    "power_w" => (info has :currentPower) ? info.currentPower : null,
                    "steps_done" => completedSteps
                });
                up.sample(now, ticks, info.currentHeartRate, info.currentCadence, info.currentSpeed);
            }
        }
        up.pump(ticks);
        if (up.refused()) {
            pairing.forget();
            upload = null;
            return unpaired(false, ticks);
        }
        var status = up.status(ticks);
        if (status.equals("Ready")) {
            // Paired and not running: show whose account this watch sends to.
            var account = pairing.account();
            label = (account == null) ? text["Label"] : text["Connected"];
            return (account == null) ? text["Connected"] : account;
        }
        label = workout ? text["Done"] + " " + completedSteps.toString() : text["Label"];
        return describe(status);
    }

    // Pairing happens before a run starts. A field that is not paired during a run
    // says so and sends nothing.
    function unpaired(beforeRun, ticks) {
        if (beforeRun) {
            pairing.pump(ticks);
        }
        var shown = pairing.shownCode();
        if (shown != null) {
            label = text["Code"];
            return shown;
        }
        label = text["Label"];
        if (!beforeRun || pairing.resting()) {
            return text["NotConnected"];
        }
        return pairing.failing() ? text["Retry"] : text["Connecting"];
    }

    function describe(status) {
        if (status.find("Sent ") == 0) {
            return text["Sent"] + " " + status.substring(5, status.length());
        }
        var known = text[status];
        return (known == null) ? status : known;
    }

    // Count Garmin notifications, not a guessed index into a training plan.
    function onWorkoutStarted() {
        completedSteps = 0;
        workout = true;
        label = text["Done"] + " 0";
    }

    function onWorkoutStepComplete() {
        completedSteps += 1;
        workout = true;
        label = text["Done"] + " " + completedSteps.toString();
    }

    function onTimerStart() { allowRunning = true; }
    function onTimerResume() { allowRunning = true; }
    function onTimerStop() { pause(); }
    function onTimerPause() { pause(); }
    function onTimerReset() { finish(); }

    private function pause() {
        allowRunning = false;
        if (upload != null) {
            upload.changeTo("paused", Time.now().value(), System.getTimer());
        }
    }

    function finish() {
        completedSteps = 0;
        workout = false;
        label = text["Label"];
        allowRunning = false;
        if (upload != null) {
            upload.changeTo("ended", Time.now().value(), System.getTimer());
        }
    }
}
