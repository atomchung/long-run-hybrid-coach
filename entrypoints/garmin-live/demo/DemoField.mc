import Toybox.Activity;
import Toybox.Lang;
import Toybox.System;

// A scripted 80-second run for the simulator and store screenshots, built with
// `build.py --demo`. It feeds the field made-up readings and never touches the network.
(:demo)
function newCoachField() {
    return new DemoField();
}

class DemoPairing extends Pairing {
    var paired = false;

    function initialize() {
        Pairing.initialize("https://example.invalid");
    }

    function credential() { return paired ? "demo" : null; }
    function account() { return "t***@example.com"; }
    function shownCode() { return "K7P4 QX9A"; }
    function resting() { return false; }
    function failing() { return false; }
    function pump(ticks) { }
    function forget() { }
}

class DemoUpload extends LiveUpload {
    function initialize() {
        LiveUpload.initialize("https://example.invalid/v1/live-run/samples", "demo");
    }

    function post(payload as Lang.Dictionary) as Void { }
    function cancelRequest() as Void { }
    function plannedSteps() { return 2; }
}

class DemoField extends LiveCoachField {
    private var second = 0;
    private var metres = 4700.0;
    private var demoPairing = null;

    function initialize() {
        LiveCoachField.initialize();
    }

    function newPairing(origin) {
        demoPairing = new DemoPairing();
        return demoPairing;
    }

    function newUpload(endpoint, credential) {
        return new DemoUpload();
    }

    // A fixed set, so the scripted heart rates land in zones 1 to 5; the profile's own
    // zones are printed to show whether a data field can read them.
    function heartRateZones() {
        System.println("heart-rate zones from the profile: " + LiveCoachField.heartRateZones());
        return [95, 114, 133, 152, 171, 190];
    }

    function compute(info as Activity.Info) as Void {
        var s = second % 80;
        second += 1;
        if (s == 0) {
            metres = 4700.0;
            finish();
        }
        demoPairing.paired = s >= 5;
        var hr = 136;
        var speed = 2.70;
        var cadence = 82;
        var state = Activity.TIMER_STATE_ON;
        if (s < 10) {
            state = Activity.TIMER_STATE_OFF;
            speed = 0.0;
        } else if (s < 20) {
            hr = 136 + s % 3;
        } else if (s < 30) {
            hr = 178 + s % 3;
            speed = 3.83;
            cadence = 92;
        } else if (s < 50) {
            hr = 151 + s % 2;
            speed = 2.99;
            cadence = 86;
            if (s == 30) {
                metres = 4985.0;
            }
            if (s == 38 || s == 44) {
                onWorkoutStepComplete();
            }
        } else if (s < 56) {
            hr = 112;
            speed = 1.40;
            cadence = 55;
        } else if (s < 62) {
            hr = 124;
            speed = 0.0;
            state = Activity.TIMER_STATE_PAUSED;
        } else {
            hr = null;
            speed = 2.80;
        }
        if (state == Activity.TIMER_STATE_ON) {
            metres += speed;
        }
        var fake = new Activity.Info();
        fake.currentHeartRate = hr;
        fake.currentSpeed = speed;
        fake.currentCadence = cadence;
        fake.timerState = state;
        fake.timerTime = (s < 10) ? 0 : (1600 + s) * 1000;
        fake.elapsedDistance = (s < 10) ? 0.0 : metres;
        LiveCoachField.compute(fake);
    }
}
