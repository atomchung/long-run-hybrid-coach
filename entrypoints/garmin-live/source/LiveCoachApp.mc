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
    private var completedSteps = 0;

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
                upload.setExtra({
                    "timer_ms" => info.timerTime,
                    "distance_m" => info.elapsedDistance,
                    "altitude_m" => info.altitude,
                    "ascent_m" => info.totalAscent,
                    "descent_m" => info.totalDescent,
                    "power_w" => (info has :currentPower) ? info.currentPower : null,
                    "steps_done" => completedSteps
                });
                upload.sample(now, ticks, info.currentHeartRate, info.currentCadence, info.currentSpeed);
            }
        }
        upload.pump(ticks);
        return upload.status(ticks);
    }

    // Count Garmin notifications, not a guessed index into a training plan.
    function onWorkoutStarted() {
        completedSteps = 0;
        label = "Done 0";
    }

    function onWorkoutStepComplete() {
        completedSteps += 1;
        label = "Done " + completedSteps.toString();
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
        completedSteps = 0;
        label = "Live Coach";
        allowRunning = false;
        upload.changeTo("ended", Time.now().value(), System.getTimer());
    }
}
