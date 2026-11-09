import Toybox.Application;
import Toybox.Communications;
import Toybox.Cryptography;
import Toybox.Lang;
import Toybox.StringUtil;
import Toybox.System;

// Pairs this field with a Coach account, the way a TV pairs with a phone (RFC 8628): ask
// for a short code with a secret only this watch holds, show the code, and poll until the
// conversation the athlete says it in lets the server issue a credential. That credential
// can only upload this account's runs, and is kept in Application.Storage.
//
// A field cannot be tapped, so it pairs by itself, and only before a run starts: at most
// three codes each time the activity opens, then it waits for the next time. A failed
// request is not a code: without the phone it keeps trying, which costs nothing.
class Pairing {
    static const MAX_CODES = 3;

    private var server;
    private var secret = null;
    private var code = null;
    private var expiresAt = 0;
    private var interval = 5000;
    private var nextAt = 0;
    private var flight = null;
    private var flightAt = 0;
    private var failures = 0;
    private var codesShown = 0;

    function initialize(origin) {
        server = origin;
    }

    function credential() {
        var value = load("credential");
        return (value instanceof Lang.String && value.length() > 0) ? value : null;
    }

    function account() {
        var value = load("account");
        return (value instanceof Lang.String && value.length() > 0) ? value : null;
    }

    // The code to show, split in two for reading aloud, or null while there is none.
    function shownCode() {
        if (code == null) { return null; }
        return code.substring(0, 4) + " " + code.substring(4, code.length());
    }

    // True once this opening of the activity has used up its codes.
    function resting() {
        return code == null && flight == null && codesShown >= MAX_CODES;
    }

    function failing() {
        return failures > 0 && code == null;
    }

    // The server refused the credential: another watch was paired, or the account is gone.
    function forget() {
        erase("credential");
        erase("account");
        clear();
        codesShown = 0;
    }

    function pump(ticks) {
        if (credential() != null) { return; }
        if (flight != null) {
            if (ticks - flightAt >= 20000) {
                flight = null;
                cancelRequest();
                fail(ticks);
            }
            return;
        }
        if (ticks < nextAt) { return; }
        if (code != null && ticks >= expiresAt) { clear(); }
        if (code == null) {
            if (codesShown >= MAX_CODES) { return; }
            secret = newSecret();
            send("ask", "/v1/watch/pairings", {"secret" => secret}, ticks);
        } else {
            send("poll", "/v1/watch/pairings/poll", {"code" => code, "secret" => secret}, ticks);
        }
    }

    private function send(kind, path, body, ticks) {
        flight = kind;
        flightAt = ticks;
        try {
            post(server + path, body, kind);
        } catch (error) {
            flight = null;
            fail(ticks);
        }
    }

    function post(url as Lang.String, body as Lang.Dictionary, kind as Lang.String) as Void {
        Communications.makeWebRequest(url, body, {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {"Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON},
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON,
            :context => kind
        }, method(:onResponse));
    }

    function cancelRequest() as Void {
        Communications.cancelAllRequests();
    }

    function onResponse(status as Lang.Number, data as Communications.ContentType or Null, context as Lang.Object) as Void {
        handle(status, data, context, System.getTimer());
    }

    function handle(status, data, kind, ticks) {
        if (flight == null || !flight.equals(kind)) { return; }
        flight = null;
        if (status != 200 || !(data instanceof Lang.Dictionary)) {
            // No phone, no connection, a server answering 429 or 5xx: try again later.
            fail(ticks);
            return;
        }
        failures = 0;
        if (kind.equals("ask")) {
            var shown = data["code"];
            if (!(shown instanceof Lang.String) || shown.length() != 8) {
                fail(ticks);
                return;
            }
            var ttl = data["expires_in"];
            var every = data["interval"];
            code = shown;
            codesShown += 1;
            expiresAt = ticks + ((ttl instanceof Lang.Number && ttl > 0) ? ttl : 600) * 1000;
            interval = (every instanceof Lang.Number && every >= 2) ? every * 1000 : 5000;
            nextAt = ticks + interval;
            return;
        }
        var state = data["status"];
        var issued = data["credential"];
        if ("issued".equals(state) && issued instanceof Lang.String && issued.length() > 0) {
            store("credential", issued);
            var label = data["account_label"];
            if (label instanceof Lang.String && label.length() > 0) {
                store("account", label);
            } else {
                erase("account");
            }
            clear();
        } else if ("pending".equals(state)) {
            nextAt = ticks + interval;
        } else {
            // Expired, used, or never this watch's: show a new code after one interval.
            clear();
            nextAt = ticks + interval;
        }
    }

    private function fail(ticks) {
        failures += 1;
        var wait = 5000 * failures;
        nextAt = ticks + (wait > 30000 ? 30000 : wait);
    }

    private function clear() {
        code = null;
        secret = null;
        nextAt = 0;
    }

    function newSecret() {
        return StringUtil.convertEncodedString(Cryptography.randomBytes(32), {
            :fromRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY,
            :toRepresentation => StringUtil.REPRESENTATION_STRING_HEX
        });
    }

    function load(key) {
        return Application.Storage.getValue(key);
    }

    function store(key, value) {
        Application.Storage.setValue(key, value);
    }

    function erase(key) {
        Application.Storage.deleteValue(key);
    }
}
