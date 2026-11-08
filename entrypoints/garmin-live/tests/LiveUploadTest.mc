import Toybox.Lang;
import Toybox.Test;

class CapturingUpload extends LiveUpload {
    var requests as Lang.Array<Lang.Dictionary> = [];
    var cancellations = 0;
    function initialize() {
        LiveUpload.initialize("https://example.invalid/v1/live-run/samples", "fixture");
    }
    function post(payload as Lang.Dictionary) as Void { requests.add(payload); }
    function cancelRequest() { cancellations += 1; }
    function reply(index, status, ticks) {
        var sample = requests[index];
        handleResponse(200, {
            "status" => status, "session_id" => sample["session_id"], "seq" => sample["seq"]
        }, [sample["session_id"], sample["seq"]], ticks);
    }
}

(:test)
function stopAndResumePreserveSession(logger) {
    var upload = new CapturingUpload();
    upload.sample(100, 1000, null, 84, 2.5);
    upload.changeTo("paused", 101, 2000);
    Test.assert(upload.requests.size() == 2 && upload.cancellations == 1);
    var stop = upload.requests[1];
    Test.assert(stop["session_id"].equals(upload.requests[0]["session_id"]));
    Test.assert(stop["state"].equals("paused") && stop["seq"] == 1);
    Test.assert(stop["heart_rate_bpm"] == null && stop["cadence_rpm"] == null && stop["speed_mps"] == null);
    upload.reply(0, "accepted", 2100); // A cancelled callback cannot acknowledge the pause.
    Test.assert(!upload.status(2100).equals("Pause sent"));
    upload.reply(1, "accepted", 2200);
    Test.assert(upload.status(2200).equals("Pause sent"));
    upload.sample(103, 4000, 130, null, null);
    Test.assert(upload.requests[2]["session_id"].equals(stop["session_id"]));
    Test.assert(upload.requests[2]["seq"] == 2 && upload.requests[2]["cadence_rpm"] == null);
    upload.changeTo("ended", 104, 5000);
    upload.reply(3, "accepted", 5100);
    Test.assert(upload.status(5200).equals("End sent"));
    upload.sample(111, 12000, null, null, null);
    Test.assert(!upload.requests[4]["session_id"].equals(stop["session_id"]) && upload.requests[4]["seq"] == 0);
    return true;
}

(:test)
function ignoredAndMismatchedReceiptsDoNotCount(logger) {
    var upload = new CapturingUpload();
    upload.sample(100, 1000, null, 82, null);
    var sample = upload.requests[0];
    upload.handleResponse(200, {"status" => "accepted", "session_id" => "wrong", "seq" => 0},
        [sample["session_id"], 0], 1200);
    Test.assert(upload.status(1200).equals("Retry"));
    upload.sample(101, 2000, null, null, null);
    Test.assert(upload.requests.size() == 1);
    upload.sample(106, 7000, 123, null, null);
    Test.assert(upload.requests[1]["observed_at"] == 106 && upload.requests[1]["seq"] == 1);
    upload.reply(1, "ignored", 7200);
    Test.assert(upload.status(7200).equals("Retry"));
    upload.sample(118, 19000, null, null, null);
    upload.reply(2, "duplicate", 19200);
    Test.assert(upload.status(19200).equals("Sent 0s"));
    return true;
}

(:test)
function oneRequestAtATimeAndNoStaleSampleReplay(logger) {
    var upload = new CapturingUpload();
    upload.sample(100, 0, 120, 83, 2.8);
    upload.sample(105, 5000, 121, 84, 2.9);
    Test.assert(upload.requests.size() == 1);
    upload.pump(20000);
    Test.assert(upload.cancellations == 1 && upload.status(20000).equals("Retry"));
    upload.sample(122, 22000, 121, 84, 2.9);
    upload.sample(126, 26000, null, null, 0.0);
    Test.assert(upload.requests.size() == 2);
    Test.assert(upload.requests[1]["observed_at"] == 126 && upload.requests[1]["heart_rate_bpm"] == null);
    upload.reply(0, "accepted", 26100);
    Test.assert(!upload.status(26100).equals("Sent 0s"));
    upload.reply(1, "accepted", 26200);
    Test.assert(upload.status(26200).equals("Sent 0s"));
    upload.sample(127, 27000, 140, 85, 3.0);
    Test.assert(upload.requests.size() == 2);
    return true;
}

(:test)
function eachPauseNeedsItsOwnReceipt(logger) {
    var upload = new CapturingUpload();
    upload.sample(100, 1000, null, null, null);
    upload.reply(0, "accepted", 1050);
    upload.changeTo("paused", 101, 2000);
    upload.reply(1, "accepted", 2050);
    Test.assert(upload.status(2100).equals("Pause sent"));
    upload.sample(102, 3000, 130, null, null);
    Test.assert(upload.status(3050).equals("Sending"));
    upload.changeTo("paused", 103, 4000);
    Test.assert(upload.status(4050).equals("Paused"));
    upload.reply(1, "accepted", 4100);
    Test.assert(upload.status(4100).equals("Paused"));
    upload.reply(3, "accepted", 4150);
    Test.assert(upload.status(4200).equals("Pause sent"));
    return true;
}

(:test)
function countWorkoutEventsAcrossPauseAndReset(logger) {
    var field = new LiveCoachField();
    field.onWorkoutStarted();
    Test.assert(field.label.equals("Done 0"));
    field.onWorkoutStepComplete();
    field.onTimerPause();
    field.onTimerResume();
    Test.assert(field.label.equals("Done 1"));
    field.onWorkoutStepComplete();
    Test.assert(field.label.equals("Done 2"));
    field.onTimerReset();
    Test.assert(field.label.equals("Coach"));
    return true;
}

(:test)
function aRefusedCredentialIsReported(logger) {
    var upload = new CapturingUpload();
    upload.sample(100, 1000, 140, 84, 2.8);
    Test.assert(!upload.refused());
    var sample = upload.requests[0];
    upload.handleResponse(401, null, [sample["session_id"], sample["seq"]], 1100);
    Test.assert(upload.refused());
    upload.sample(106, 7000, 140, 84, 2.8);
    Test.assert(upload.requests.size() == 1);
    return true;
}

(:test)
function theFieldTranslatesUploadCodesAndShowsPairing(logger) {
    var field = new LiveCoachField();
    Test.assert(field.describe("Sent 3s").equals("Sent 3s"));
    Test.assert(field.describe("Pause sent").equals("Pause sent"));
    Test.assert(field.describe("Ready").equals("Ready"));
    return true;
}

