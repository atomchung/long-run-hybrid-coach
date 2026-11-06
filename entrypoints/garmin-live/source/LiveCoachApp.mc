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

class LiveCoachField extends WatchUi.SimpleDataField {
    private var upload;
    private var allowRunning = true;

    function initialize() {
        SimpleDataField.initialize();
        label = "Live Coach";
        upload = new LiveUpload(
            Application.Properties.getValue("Endpoint"),
            Application.Properties.getValue("DeviceToken")
        );
    }

    function compute(info) {
        var now = Time.now().value();
        var ticks = System.getTimer();
        var timer = info.timerState;
        if (timer == Activity.TIMER_STATE_OFF) {
            upload.changeTo("ended", now, ticks);
        } else if (timer == Activity.TIMER_STATE_STOPPED || timer == Activity.TIMER_STATE_PAUSED) {
            upload.changeTo("paused", now, ticks);
        } else if (timer == Activity.TIMER_STATE_ON && allowRunning) {
            var profile = Activity.getProfileInfo();
            if (profile != null && profile.sport == Activity.SPORT_RUNNING) {
                upload.sample(now, ticks, info.currentHeartRate, info.currentCadence, info.currentSpeed);
            }
        }
        upload.pump(ticks);
        return upload.status(ticks);
    }

    function onTimerStart() { allowRunning = true; }
    function onTimerResume() { allowRunning = true; }
    function onTimerStop() { pause(); }
    function onTimerPause() { pause(); }
    function onTimerReset() { finish(); }

    private function pause() {
        allowRunning = false;
        upload.changeTo("paused", Time.now().value(), System.getTimer());
    }

    function finish() {
        allowRunning = false;
        upload.changeTo("ended", Time.now().value(), System.getTimer());
    }
}
