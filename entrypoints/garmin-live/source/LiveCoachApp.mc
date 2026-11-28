import Toybox.Activity;
import Toybox.Application;
import Toybox.Graphics;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.UserProfile;
import Toybox.WatchUi;

class LiveCoachApp extends Application.AppBase {
    private var field;

    function initialize() {
        AppBase.initialize();
    }

    function getInitialView() {
        field = newCoachField();
        return [field];
    }

    function onStop(state) {
        if (field != null) {
            field.finish();
        }
    }
}

(:live)
function newCoachField() {
    return new LiveCoachField();
}

// The Coach running field. A full data page: the heart-rate zone on the top arc, heart rate
// large, pace, time and distance under icons, and the mascot running a mountain trail that
// scrolls past at the runner's real speed. There are no words on it; the pairing code and the
// account it sends to are the only text, and only before the run.
//
// Underneath, the upload is unchanged: unpaired before a run it shows a code to say in the
// Coach conversation; paired, it sends a sample about every five seconds while running.
class LiveCoachField extends WatchUi.DataField {
    static const SAMPLES_PATH = "/v1/live-run/samples";

    // How the link to the Coach is doing, drawn as an icon.
    enum {
        LINK_OK,
        LINK_READY,
        LINK_CODE,
        LINK_CONNECTING,
        LINK_RETRY,
        LINK_NONE,
        LINK_FAILING,
        LINK_BROKEN
    }

    private var server = null;
    private var pairing = null;
    private var upload = null;
    private var allowRunning = true;
    private var completedSteps = 0;
    private var plannedSteps = null;

    private var zones = null;
    private var unit = 1000.0;
    private var paceUnit = 1000.0;
    private var cadence as CadenceMedian;
    private var hr = null;
    private var speed = null;
    private var timerMs = 0;
    private var metres = 0.0;
    private var timer = Activity.TIMER_STATE_OFF;
    private var frame = 0;
    private var walking = false;
    private var moving = false;
    private var passed = 0;
    private var counting = false;
    private var cheerUntil = 0;
    private var stampUntil = 0;
    private var doneUntil = 0;
    private var link = LINK_OK;
    private var code = null;
    private var retrySince = null;
    private var sprites as Lang.Dictionary = {};
    // The empty eye of the sprite spriteFor last chose ([x, y] from SpriteEyes), or null when
    // that sprite has its own pupil; and where its pupil rests when nothing is happening.
    private var eye = null;
    private var rest = :scenery;

    function initialize() {
        DataField.initialize();
        cadence = new CadenceMedian();
        var settings = System.getDeviceSettings();
        unit = (settings.distanceUnits == System.UNIT_STATUTE) ? 1609.344 : 1000.0;
        paceUnit = (settings.paceUnits == System.UNIT_STATUTE) ? 1609.344 : 1000.0;
        readZones();
        var origin = COACH_SERVER;
        if (origin.find("https://") == 0 && origin.length() > 8
                && !origin.substring(origin.length() - 1, origin.length()).equals("/")) {
            server = origin;
            pairing = newPairing(origin);
        }
    }

    // The athlete's own Garmin heart-rate zones for this activity, or null if the watch does
    // not give them to a data field.
    private function readZones() as Void {
        zones = heartRateZones();
    }

    function heartRateZones() {
        try {
            return UserProfile.getHeartRateZones(UserProfile.getCurrentSport());
        } catch (error) {
            return null;
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

    function compute(info as Activity.Info) as Void {
        var ticks = System.getTimer();
        hr = info.currentHeartRate;
        speed = info.currentSpeed;
        timerMs = (info.timerTime == null) ? 0 : info.timerTime;
        metres = (info.elapsedDistance == null) ? 0.0 : info.elapsedDistance.toFloat();
        // A missing timer state keeps the last one: reading it as stopped would end the upload
        // session mid-run and split the recap in two.
        if (info.timerState != null) {
            timer = info.timerState;
        }
        moving = timer == Activity.TIMER_STATE_ON && speed != null && speed >= 0.3;
        if (moving) {
            cadence.add(info.currentCadence);
            frame += 1;
        }
        walking = moving && cadence.walking(info.currentCadence);
        var whole = RunMath.marksPassed(metres, unit);
        if (timer != Activity.TIMER_STATE_OFF) {
            if (!counting) {
                // A field that starts mid-run takes the distance already run as passed.
                passed = whole;
                counting = true;
            } else if (whole > passed) {
                passed = whole;
                cheerUntil = ticks + 5000;
            }
        }
        send(info, ticks);
    }

    private function send(info as Activity.Info, ticks) as Void {
        if (pairing == null) {
            link = LINK_BROKEN;
            return;
        }
        var now = Time.now().value();
        var up = uploader();
        if (up == null) {
            unpaired(timer == Activity.TIMER_STATE_OFF, ticks);
            return;
        }
        code = null;
        if (timer == Activity.TIMER_STATE_OFF) {
            up.changeTo("ended", now, ticks);
        } else if (timer == Activity.TIMER_STATE_STOPPED || timer == Activity.TIMER_STATE_PAUSED) {
            // The pause carries the totals at the stop, so the recap is not short by the last
            // seconds between uploads or the step finished in them. The end that follows
            // keeps these: after a reset the activity reads zero.
            up.setExtra(totals(info));
            up.changeTo("paused", now, ticks);
        } else if (timer == Activity.TIMER_STATE_ON && allowRunning) {
            var profile = Activity.getProfileInfo();
            if (profile != null && profile.sport == Activity.SPORT_RUNNING) {
                up.setExtra(totals(info));
                up.sample(now, ticks, info.currentHeartRate, info.currentCadence, info.currentSpeed);
            }
        }
        up.pump(ticks);
        if (up.refused()) {
            pairing.forget();
            upload = null;
            unpaired(false, ticks);
            return;
        }
        // The count belongs to the running session: it follows the upload's answer, null
        // included, and is not copied back after finish() cleared it for the next run.
        if (timer == Activity.TIMER_STATE_ON) {
            plannedSteps = up.plannedSteps();
        }
        var status = up.status(ticks);
        if (status.equals("Ready")) {
            link = LINK_READY;
            retrySince = null;
            return;
        }
        if (status.equals("Retry")) {
            if (retrySince == null) {
                retrySince = ticks;
            }
        } else {
            retrySince = null;
        }
        link = (retrySince != null && ticks - retrySince >= 30000) ? LINK_FAILING : LINK_OK;
    }

    private function totals(info as Activity.Info) {
        return {
            "timer_ms" => info.timerTime,
            "distance_m" => info.elapsedDistance,
            "altitude_m" => info.altitude,
            "ascent_m" => info.totalAscent,
            "descent_m" => info.totalDescent,
            "power_w" => (info has :currentPower) ? info.currentPower : null,
            "steps_done" => completedSteps
        };
    }

    // Pairing happens before a run starts. A field that is not paired during a run sends
    // nothing; a code from before the start is not polled any more, so it is not shown either.
    function unpaired(beforeRun, ticks) as Void {
        code = null;
        if (!beforeRun) {
            link = LINK_NONE;
            return;
        }
        pairing.pump(ticks);
        code = pairing.shownCode();
        if (code != null) {
            link = LINK_CODE;
        } else if (pairing.resting()) {
            link = LINK_NONE;
        } else {
            link = pairing.failing() ? LINK_RETRY : LINK_CONNECTING;
        }
    }

    // ---------------- drawing ----------------

    function onUpdate(dc as Graphics.Dc) as Void {
        dc.setColor(Ink.BLACK, Ink.BLACK);
        dc.clear();
        if (dc has :setAntiAlias) {
            dc.setAntiAlias(true);
        }
        var screen = System.getDeviceSettings();
        var w = dc.getWidth();
        var h = dc.getHeight();
        if (w * 10 >= screen.screenWidth * 9 && h * 10 >= screen.screenHeight * 9) {
            full(dc, w < h ? w : h);
        } else {
            compact(dc);
        }
    }

    private function full(dc as Graphics.Dc, size) as Void {
        var p = new Painter(dc, size);
        var ticks = System.getTimer();
        var zone = RunMath.zoneFor(hr, zones);
        Face.arc(p, zone);
        Face.numbers(p, hr, RunMath.pace(moving ? speed : null, paceUnit), RunMath.clock(timerMs),
            RunMath.distance(metres, unit));
        // The lane: the trail, the next and the last whole-km flags, the mascot, then events.
        dc.setClip(p.x(0), p.y(298), p.len(454), p.len(160));
        var scroll = metres * Face.PX_PER_M;
        Trail.draw(p, scroll);
        if (timer != Activity.TIMER_STATE_OFF) {
            for (var m = passed; m <= passed + 1; m += 1) {
                if (m < 1) {
                    continue;
                }
                var fx = Face.RUN_X + (m * unit - metres) * Face.PX_PER_M;
                if (fx > -60 && fx < 470) {
                    Face.flag(p, fx, m, m <= passed);
                }
            }
        }
        var sprite = spriteFor(zone, ticks);
        var surge = (moving && !walking && frame % 2 == 1) ? 3 : (moving && !walking ? -3 : 0);
        dc.drawBitmap(p.x(Face.SPRITE_X + surge), p.y(Face.SPRITE_Y), bitmap(sprite));
        if (eye != null) {
            Face.pupil(dc, p.x(Face.SPRITE_X + surge), p.y(Face.SPRITE_Y), p.k, eye, look(ticks));
        }
        if (ticks < doneUntil) {
            Face.done(p);
        } else if (ticks < stampUntil) {
            Face.stamp(p);
        }
        dc.clearClip();
        drawLink(p);
    }

    // Before the run: the pairing code, or the account this watch sends to. During it: a
    // crossed-out link only when uploads have failed for 30 seconds.
    private function drawLink(p as Painter) as Void {
        if (link == LINK_CODE && code != null) {
            var parts = splitCode(code);
            Face.link(p, 316, 322, false);
            p.text(316, 352, Graphics.FONT_MEDIUM, parts[0], Ink.CREAM);
            p.text(316, 384, Graphics.FONT_MEDIUM, parts[1], Ink.CREAM);
        } else if (link == LINK_CONNECTING) {
            Face.waiting(p, 316, 352, (System.getTimer() / 1000).toNumber());
        } else if (link == LINK_READY && timer == Activity.TIMER_STATE_OFF) {
            // Ready also holds through an activity that is not a run, which sends nothing.
            var account = pairing.account();
            if (account != null) {
                p.text(227, 434, Graphics.FONT_XTINY, account, Ink.DIM);
            } else {
                Face.link(p, 227, 430, false);
            }
        } else if (link == LINK_RETRY || link == LINK_NONE || link == LINK_FAILING || link == LINK_BROKEN) {
            Face.link(p, 360, 322, true);
        }
    }

    private function splitCode(value) as Lang.Array {
        var space = value.find(" ");
        if (space != null) {
            return [value.substring(0, space), value.substring(space + 1, value.length())];
        }
        var half = value.length() / 2;
        return [value.substring(0, half), value.substring(half, value.length())];
    }

    // A slot smaller than the page: heart rate and the zone, with the mascot when it fits.
    private function compact(dc as Graphics.Dc) as Void {
        var w = dc.getWidth();
        var h = dc.getHeight();
        var zone = RunMath.zoneFor(hr, zones);
        var screen = System.getDeviceSettings().screenWidth;
        var spriteW = (screen * 0.3502).toNumber();
        var spriteH = (spriteW * 136 / 159).toNumber();
        var left = 0;
        if (h >= spriteH && w >= spriteW * 2) {
            var ticks = System.getTimer();
            dc.drawBitmap(w / 2 - spriteW - 4, (h - spriteH) / 2, bitmap(spriteFor(zone, ticks)));
            if (eye != null) {
                Face.pupil(dc, w / 2 - spriteW - 4, (h - spriteH) / 2, screen / 454.0, eye, look(ticks));
            }
            left = w / 2;
        }
        var value = (hr == null) ? "--" : hr.format("%d");
        var fonts = [Graphics.FONT_NUMBER_HOT, Graphics.FONT_NUMBER_MEDIUM, Graphics.FONT_NUMBER_MILD, Graphics.FONT_MEDIUM];
        var font = fonts[fonts.size() - 1];
        for (var i = 0; i < fonts.size(); i += 1) {
            if (dc.getFontHeight(fonts[i]) <= h * 0.7 && dc.getTextWidthInPixels(value, fonts[i]) <= (w - left) * 0.8) {
                font = fonts[i];
                break;
            }
        }
        var cx = left + (w - left) / 2;
        dc.setColor(Ink.CREAM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, h / 2, font, value, Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
        var seg = (w - left) / 8;
        var top = h / 2 + dc.getFontHeight(font) / 2;
        if (top + 6 > h) {
            top = h - 6;
        }
        for (var i = 0; i < 5; i += 1) {
            var lit = zone != null && zone == i + 1;
            dc.setColor(lit ? Ink.ZONE[i] : Ink.ZONE_DIM[i], Graphics.COLOR_TRANSPARENT);
            dc.fillRectangle(cx - seg * 5 / 2 + i * seg + 1, top, seg - 2, lit ? 6 : 4);
        }
    }

    private function spriteFor(zone, ticks) {
        var z = (zone == null) ? 1 : zone;
        eye = null;
        if (timer == Activity.TIMER_STATE_PAUSED || timer == Activity.TIMER_STATE_STOPPED) {
            return Rez.Drawables.Sit;
        }
        if (ticks < cheerUntil || ticks < doneUntil) {
            return [Rez.Drawables.CheerZ1, Rez.Drawables.CheerZ2, Rez.Drawables.CheerZ3,
                    Rez.Drawables.CheerZ4, Rez.Drawables.CheerZ5][z - 1];
        }
        if (!moving) {
            return Rez.Drawables.Stand;
        }
        if (walking) {
            eye = SpriteEyes.WALK[frame % 2];
            rest = :scenery;
            return (frame % 2 == 0) ? Rez.Drawables.Walk0 : Rez.Drawables.Walk1;
        }
        var run = [
            [Rez.Drawables.RunZ1P0, Rez.Drawables.RunZ1P1, Rez.Drawables.RunZ1P2, Rez.Drawables.RunZ1P3],
            [Rez.Drawables.RunZ2P0, Rez.Drawables.RunZ2P1, Rez.Drawables.RunZ2P2, Rez.Drawables.RunZ2P3],
            [Rez.Drawables.RunZ3P0, Rez.Drawables.RunZ3P1, Rez.Drawables.RunZ3P2, Rez.Drawables.RunZ3P3],
            [Rez.Drawables.RunZ4P0, Rez.Drawables.RunZ4P1, Rez.Drawables.RunZ4P2, Rez.Drawables.RunZ4P3],
            [Rez.Drawables.RunZ5P0, Rez.Drawables.RunZ5P1, Rez.Drawables.RunZ5P2, Rez.Drawables.RunZ5P3]
        ];
        if (z <= 2) {
            eye = SpriteEyes.RUN[z - 1][frame % 4];
            rest = (z == 1) ? :scenery : :level;
        }
        return run[z - 1][frame % 4];
    }

    // Where the drawn pupil looks this second (RunMath.gaze), with :rest resolved.
    private function look(ticks) {
        var ahead = null;
        if (timer != Activity.TIMER_STATE_OFF) {
            ahead = ((passed + 1) * unit - metres) * Face.PX_PER_M;
        }
        var cheerEnd = (cheerUntil > doneUntil) ? cheerUntil : doneUntil;
        var g = RunMath.gaze(ticks, cheerEnd, stampUntil, ahead);
        return (g == :rest) ? rest : g;
    }

    // Bitmaps load into the graphics pool on first use and stay referenced here.
    private function bitmap(id) {
        var found = sprites[id];
        if (found == null) {
            found = WatchUi.loadResource(id);
            sprites[id] = found;
        }
        return found;
    }

    // ---------------- read by the on-device tests ----------------

    function completedStepCount() { return completedSteps; }
    function linkState() { return link; }
    function setPlannedSteps(value) as Void { plannedSteps = value; }

    // "done" while the check for the last planned step shows, "stamp" for any other
    // completed step, or null.
    function celebration(ticks) {
        if (ticks < doneUntil) {
            return "done";
        }
        return (ticks < stampUntil) ? "stamp" : null;
    }

    // ---------------- workout and timer callbacks ----------------

    // Count Garmin notifications, not a guessed index into a training plan.
    function onWorkoutStarted() as Void {
        completedSteps = 0;
    }

    function onWorkoutStepComplete() as Void {
        completedSteps += 1;
        var ticks = System.getTimer();
        // Exactly the planned count: a longer workout that is not today's Coach run would
        // otherwise show the check again on every later step.
        if (plannedSteps != null && completedSteps == plannedSteps) {
            doneUntil = ticks + 10000;
        } else {
            stampUntil = ticks + 5000;
        }
    }

    function onTimerStart() as Void {
        allowRunning = true;
        readZones();
    }
    function onTimerResume() as Void { allowRunning = true; }
    function onTimerStop() as Void { pause(); }
    function onTimerPause() as Void { pause(); }
    function onTimerReset() as Void { finish(); }

    private function pause() as Void {
        allowRunning = false;
        if (upload != null) {
            upload.changeTo("paused", Time.now().value(), System.getTimer());
        }
    }

    function finish() as Void {
        completedSteps = 0;
        plannedSteps = null;
        passed = 0;
        counting = false;
        frame = 0;
        cheerUntil = 0;
        stampUntil = 0;
        doneUntil = 0;
        cadence.reset();
        allowRunning = false;
        if (upload != null) {
            upload.changeTo("ended", Time.now().value(), System.getTimer());
        }
    }
}
