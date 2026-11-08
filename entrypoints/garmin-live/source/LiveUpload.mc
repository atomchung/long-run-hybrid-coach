import Toybox.Communications;
import Toybox.Lang;
import Toybox.Math;
import Toybox.System;

// Only a fresh compute() sample may create a running request. Pause/end
// notifications can retry with their original observation time and sequence.
class LiveUpload {
    private var endpoint;
    private var token;
    private var configured = false;
    private var authFailed = false;
    private var sessionId = null;
    private var phase = "idle";
    private var seq = 0;
    private var lastSampleAt = null;

    private var flight as Lang.Dictionary or Null = null;
    private var flightAt = 0;
    private var control as Lang.Dictionary or Null = null;
    private var controlAt = 0;
    private var failedAt = null;
    private var retryMs = 5000;
    private var ackAt = null;
    private var ackPhase = "";
    private var extra as Lang.Dictionary or Null = null;

    function initialize(url, deviceToken) {
        endpoint = url;
        token = deviceToken;
        var suffix = "/v1/live-run/samples";
        configured = url instanceof Lang.String && deviceToken instanceof Lang.String
            && deviceToken.length() > 0 && url.length() > 8 + suffix.length()
            && url.find("https://") == 0
            && url.substring(url.length() - suffix.length(), url.length()).equals(suffix);
    }

    // Measurements beyond the three the receiver has always taken, for running samples only.
    function setExtra(values as Lang.Dictionary) as Void {
        extra = values;
    }

    private function body(when, hr, cadence, speed) {
        var result = {
            "session_id" => sessionId, "seq" => seq, "observed_at" => when,
            "state" => phase, "heart_rate_bpm" => hr, "cadence_rpm" => cadence,
            "speed_mps" => speed
        };
        if (phase.equals("running") && extra != null) {
            var keys = extra.keys();
            for (var i = 0; i < keys.size(); i += 1) {
                result[keys[i]] = extra[keys[i]];
            }
        }
        seq += 1;
        return result;
    }

    function sample(when, ticks, hr, cadence, speed) {
        if (!configured || authFailed) { return; }
        if (phase.equals("idle") || phase.equals("ended")) {
            sessionId = "run-" + when.toString() + "-" + Math.rand().toString();
            seq = 0;
            ackAt = null;
            lastSampleAt = null;
        } else if (phase.equals("paused")) {
            ackAt = null;
            control = null;
            cancelFlight();
            lastSampleAt = null;
        }
        phase = "running";
        pump(ticks);
        if (control != null || !ready(ticks)) { return; }
        if (lastSampleAt != null && ticks - lastSampleAt < 5000) { return; }
        lastSampleAt = ticks;
        send(body(when, hr, cadence, speed), ticks);
    }

    function changeTo(next, when, ticks) {
        if (!configured || authFailed || sessionId == null || phase.equals(next) || phase.equals("ended")) {
            return;
        }
        phase = next;
        ackAt = null;
        cancelFlight();
        control = body(when, null, null, null);
        controlAt = ticks;
        pump(ticks);
    }

    private function ready(ticks) {
        return flight == null && (failedAt == null || ticks - failedAt >= retryMs);
    }

    function pump(ticks) {
        if (!configured || authFailed) { return; }
        if (flight != null && ticks - flightAt >= 20000) {
            cancelFlight();
            backOff(ticks);
        }
        if (control == null) { return; }
        if (ticks - controlAt >= 30000) {
            control = null;
        } else if (ready(ticks)) {
            send(control, ticks);
        }
    }

    private function backOff(ticks) {
        retryMs = (failedAt == null) ? 5000 : ((retryMs < 15000) ? retryMs * 2 : 30000);
        failedAt = ticks;
    }

    private function cancelFlight() {
        if (flight != null) {
            flight = null;
            cancelRequest();
        }
    }

    function cancelRequest() {
        Communications.cancelAllRequests();
    }

    private function send(payload, ticks) {
        flight = payload;
        flightAt = ticks;
        try {
            post(payload);
        } catch (error) {
            flight = null;
            backOff(ticks);
        }
    }

    function post(payload as Lang.Dictionary) as Void {
        Communications.makeWebRequest(endpoint, payload, {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {
                "Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON,
                "Authorization" => "Bearer " + token
            },
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON,
            :context => [payload["session_id"], payload["seq"]]
        }, method(:onResponse));
    }

    function onResponse(code as Lang.Number, data as Communications.ContentType or Null, context as Lang.Object) as Void {
        if (context instanceof Lang.Array) {
            handleResponse(code, data, context, System.getTimer());
        }
    }

    function handleResponse(code, data, context as Lang.Array, ticks) {
        if (flight == null || !flight["session_id"].equals(context[0]) || flight["seq"] != context[1]) {
            return;
        }
        var sent = flight;
        flight = null;
        if (code == 401 || code == 403) {
            authFailed = true;
            control = null;
            return;
        }
        var receipt = null;
        if (code >= 200 && code < 300 && data instanceof Lang.Dictionary
                && context[0].equals(data["session_id"]) && context[1] == data["seq"]) {
            receipt = data["status"];
        }
        if ("accepted".equals(receipt) || "duplicate".equals(receipt)) {
            failedAt = null;
            if (sessionId.equals(context[0])) {
                ackAt = ticks;
                ackPhase = sent["state"];
            }
            if (control == sent) { control = null; }
        } else {
            if ("ignored".equals(receipt) && control == sent) { control = null; }
            backOff(ticks);
        }
        pump(ticks);
    }

    function status(ticks) {
        if (!configured) { return "Set up"; }
        if (authFailed) { return "Check key"; }
        if (phase.equals("idle")) { return "Ready"; }
        if (failedAt != null) { return "Retry"; }
        if (ackAt != null && phase.equals(ackPhase)) {
            if (phase.equals("ended")) { return "End sent"; }
            if (phase.equals("paused")) { return "Pause sent"; }
            return "Sent " + ((ticks - ackAt) / 1000).toString() + "s";
        }
        if (phase.equals("ended")) { return "End pending"; }
        if (phase.equals("paused")) { return "Paused"; }
        return "Sending";
    }
}
