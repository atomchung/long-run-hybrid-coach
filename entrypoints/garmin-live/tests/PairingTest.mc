import Toybox.Lang;
import Toybox.Test;

class CapturingPairing extends Pairing {
    var requests as Lang.Array<Lang.Array> = [];
    var cancellations = 0;
    var kept as Lang.Dictionary = {};
    var secrets = 0;

    function initialize() {
        Pairing.initialize("https://example.invalid");
    }
    function post(url as Lang.String, body as Lang.Dictionary, kind as Lang.String) as Void {
        requests.add([url, body, kind]);
    }
    function cancelRequest() as Void { cancellations += 1; }
    function newSecret() {
        secrets += 1;
        return "secret-" + secrets.toString() + "-0123456789abcdef0123456789abcdef";
    }
    function load(key) { return kept[key]; }
    function store(key, value) { kept[key] = value; }
    function erase(key) { kept.remove(key); }

    function last() { return requests[requests.size() - 1]; }
}

function codeReply(code, ttl) {
    return {"code" => code, "expires_in" => ttl, "interval" => 5};
}

(:test)
function aSaidCodeBecomesAStoredCredential(logger) {
    var pairing = new CapturingPairing();
    pairing.pump(0);
    Test.assert(pairing.requests.size() == 1);
    Test.assert(pairing.last()[0].equals("https://example.invalid/v1/watch/pairings"));
    var secret = pairing.last()[1]["secret"];
    Test.assert(pairing.last()[1].keys().size() == 1 && secret.length() > 32);
    pairing.handle(200, codeReply("K7P4QX9A", 600), "ask", 100);
    Test.assert(pairing.shownCode().equals("K7P4 QX9A"));
    pairing.pump(1000);
    Test.assert(pairing.requests.size() == 1);
    pairing.pump(5100);
    Test.assert(pairing.last()[0].equals("https://example.invalid/v1/watch/pairings/poll"));
    Test.assert(pairing.last()[1]["code"].equals("K7P4QX9A") && pairing.last()[1]["secret"].equals(secret));
    pairing.handle(200, {"status" => "pending"}, "poll", 5200);
    Test.assert(pairing.shownCode() != null && pairing.credential() == null);
    pairing.pump(10200);
    pairing.handle(200, {"status" => "issued", "credential" => "device-credential",
        "account_label" => "t***@gmail.com"}, "poll", 10300);
    Test.assert(pairing.credential().equals("device-credential"));
    Test.assert(pairing.account().equals("t***@gmail.com"));
    Test.assert(pairing.shownCode() == null);
    pairing.pump(20000);
    Test.assert(pairing.requests.size() == 3);
    return true;
}

(:test)
function aCodeThatIsRefusedOrRunsOutIsReplaced(logger) {
    var pairing = new CapturingPairing();
    pairing.pump(0);
    pairing.handle(200, codeReply("K7P4QX9A", 600), "ask", 0);
    pairing.pump(5000);
    pairing.handle(200, {"status" => "invalid"}, "poll", 5100);
    Test.assert(pairing.shownCode() == null);
    pairing.pump(6000);
    Test.assert(pairing.requests.size() == 2);
    pairing.pump(10100);
    Test.assert(pairing.last()[2].equals("ask") && pairing.secrets == 2);
    pairing.handle(200, codeReply("M2N3PQ4R", 10), "ask", 10100);
    pairing.pump(20100);
    Test.assert(pairing.last()[2].equals("ask") && pairing.secrets == 3);
    return true;
}

(:test)
function threeCodesThenItWaitsForTheNextRun(logger) {
    var pairing = new CapturingPairing();
    var ticks = 0;
    for (var i = 0; i < 3; i += 1) {
        pairing.pump(ticks);
        pairing.handle(200, codeReply("K7P4QX9A", 10), "ask", ticks);
        ticks += 11000;
        pairing.pump(ticks);
    }
    Test.assert(pairing.resting());
    var asked = pairing.requests.size();
    pairing.pump(ticks + 60000);
    Test.assert(pairing.requests.size() == asked);
    pairing.forget();
    pairing.pump(ticks + 61000);
    Test.assert(pairing.requests.size() == asked + 1);
    return true;
}

(:test)
function noPhoneMeansWaitingLongerEachTime(logger) {
    var pairing = new CapturingPairing();
    pairing.pump(0);
    pairing.handle(-104, null, "ask", 100);
    Test.assert(pairing.failing());
    pairing.pump(5000);
    Test.assert(pairing.requests.size() == 1);
    pairing.pump(5100);
    pairing.handle(429, {"error" => "rate_limited"}, "ask", 5200);
    pairing.pump(15100);
    Test.assert(pairing.requests.size() == 2);
    pairing.pump(15200);
    Test.assert(pairing.requests.size() == 3);
    // A request with no answer at all is given up on after 20 seconds.
    pairing.pump(35300);
    Test.assert(pairing.cancellations == 1 && pairing.failing());
    return true;
}

(:test)
function aLateOrMismatchedAnswerChangesNothing(logger) {
    var pairing = new CapturingPairing();
    pairing.pump(0);
    pairing.handle(200, {"status" => "issued", "credential" => "x"}, "poll", 50);
    Test.assert(pairing.credential() == null);
    pairing.handle(200, codeReply("SHORT", 600), "ask", 60);
    Test.assert(pairing.shownCode() == null && pairing.failing());
    return true;
}

(:test)
function forgettingTheCredentialStartsOver(logger) {
    var pairing = new CapturingPairing();
    pairing.store("credential", "device-credential");
    pairing.store("account", "t***@gmail.com");
    pairing.pump(0);
    Test.assert(pairing.requests.size() == 0);
    pairing.forget();
    Test.assert(pairing.credential() == null && pairing.account() == null);
    pairing.pump(0);
    Test.assert(pairing.requests.size() == 1);
    return true;
}
