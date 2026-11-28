import Toybox.Lang;

// What the field shows, worked out from Activity.Info. Nothing here draws, so the on-device
// tests can check it without a screen.
module RunMath {
    // Heart-rate zone 1 to 5 from Garmin's thresholds [min zone 1, max zone 1, max zone 2,
    // max zone 3, max zone 4, max zone 5], or null when the heart rate or the zones are
    // missing. Below zone 1 reads as zone 1 and above zone 5 as zone 5.
    function zoneFor(hr, zones) {
        if (hr == null || zones == null || !(zones instanceof Lang.Array) || zones.size() < 6) {
            return null;
        }
        for (var i = 1; i <= 5; i += 1) {
            if (zones[i] == null) {
                return null;
            }
            if (hr <= zones[i]) {
                return i;
            }
        }
        return 5;
    }

    // Timer time as m:ss, or h:mm:ss from an hour on.
    function clock(ms) {
        if (ms == null || ms < 0) {
            ms = 0;
        }
        var s = ms / 1000;
        var h = s / 3600;
        var m = (s % 3600) / 60;
        var sec = s % 60;
        if (h > 0) {
            return h.format("%d") + ":" + m.format("%02d") + ":" + sec.format("%02d");
        }
        return m.format("%d") + ":" + sec.format("%02d");
    }

    // Seconds per kilometre or mile as m:ss; "--:--" when not moving or slower than 99:59.
    function pace(speed, unitMetres) {
        if (speed == null || speed < 0.3) {
            return "--:--";
        }
        var s = (unitMetres / speed + 0.5).toNumber();
        if (s >= 6000) {
            return "--:--";
        }
        return (s / 60).format("%d") + ":" + (s % 60).format("%02d");
    }

    // Kilometres or miles to two decimals, cut rather than rounded, as Garmin shows them.
    function distance(metres, unitMetres) {
        if (metres == null || metres < 0) {
            metres = 0;
        }
        var hundredths = (metres * 100.0 / unitMetres).toNumber();
        return (hundredths / 100).format("%d") + "." + (hundredths % 100).format("%02d");
    }

    // Whole kilometres (or miles) run so far.
    function marksPassed(metres, unitMetres) {
        if (metres == null || metres <= 0) {
            return 0;
        }
        return (metres / unitMetres).toNumber();
    }

    // Largest whole number not above value, for scroll positions that can be negative.
    function floorDiv(value, divisor) {
        var q = (value / divisor).toNumber();
        if (q * divisor > value) {
            q -= 1;
        }
        return q;
    }
}

// The run's own walking rule, as in live_run.py: walking is a cadence below 80% of this run's
// median moving cadence. The median means nothing until enough moving seconds are counted.
class CadenceMedian {
    static const MIN_SAMPLES = 30;
    private var counts as Lang.Array<Lang.Number>;
    private var total = 0;

    function initialize() {
        counts = new Lang.Array<Lang.Number>[256];
        reset();
    }

    function reset() as Void {
        for (var i = 0; i < counts.size(); i += 1) {
            counts[i] = 0;
        }
        total = 0;
    }

    function add(cadence) as Void {
        if (cadence != null && cadence > 0 && cadence < counts.size()) {
            counts[cadence] += 1;
            total += 1;
        }
    }

    function median() {
        if (total < MIN_SAMPLES) {
            return null;
        }
        var half = (total + 1) / 2;
        var seen = 0;
        for (var i = 0; i < counts.size(); i += 1) {
            seen += counts[i];
            if (seen >= half) {
                return i;
            }
        }
        return null;
    }

    function walking(cadence) {
        var m = median();
        return m != null && cadence != null && cadence > 0 && cadence * 10 < m * 8;
    }
}
