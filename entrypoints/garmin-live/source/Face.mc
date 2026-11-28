import Toybox.Graphics;
import Toybox.Lang;
import Toybox.WatchUi;

// Colours of the running field. Coral and cream carry the numbers and the mascot; gold only
// marks an achievement; the five zone colours only appear on the top arc.
module Ink {
    const BLACK = 0x000000;
    const CREAM = 0xF1E6D0;
    const CORAL = 0xFF7A6B;
    const DIM = 0xA89F8C;
    const LINE = 0x2A2620;
    const OFF = 0x4A443B;
    const GOLD = 0xF2C14E;
    const BLUE = 0x4FA3E0;
    const GREEN = 0x5DC46B;
    // Zone 1 to 5 lit, then the same colour at about a quarter strength as the scale.
    const ZONE = [0x9C9C9C, 0x4FA3E0, 0x5DC46B, 0xF59A3A, 0xE5484D];
    const ZONE_DIM = [0x2C2C2C, 0x162E3F, 0x1A371E, 0x452B10, 0x401416];
    // The trail.
    const FAR = 0x1A1712;
    const SNOW = 0x4A4237;
    const PINE = 0x3A332A;
    const TRUNK = 0x5A5246;
    const NEAR = 0x8A806E;
}

// Draws in the 454 x 454 design space of the running field, scaled and centred on the slot.
class Painter {
    var dc as Graphics.Dc;
    var k as Lang.Float;
    var ox as Lang.Float;
    var oy as Lang.Float;

    function initialize(target as Graphics.Dc, size as Lang.Number) {
        dc = target;
        k = size / 454.0;
        ox = (target.getWidth() - size) / 2.0;
        oy = (target.getHeight() - size) / 2.0;
    }

    function x(v) { return (ox + v * k).toNumber(); }
    function y(v) { return (oy + v * k).toNumber(); }
    function len(v) { return (v * k + 0.5).toNumber(); }

    function pen(width, colour) as Void {
        var w = len(width);
        dc.setPenWidth(w < 1 ? 1 : w);
        dc.setColor(colour, Graphics.COLOR_TRANSPARENT);
    }

    function line(x0, y0, x1, y1) as Void {
        dc.drawLine(x(x0), y(y0), x(x1), y(y1));
    }

    function poly(points as Lang.Array, colour) as Void {
        var out = new [points.size()];
        for (var i = 0; i < points.size(); i += 1) {
            out[i] = [x(points[i][0]), y(points[i][1])];
        }
        dc.setColor(colour, Graphics.COLOR_TRANSPARENT);
        dc.fillPolygon(out);
    }

    function outline(points as Lang.Array, width, colour) as Void {
        pen(width, colour);
        for (var i = 0; i < points.size(); i += 1) {
            var a = points[i];
            var b = points[(i + 1) % points.size()];
            line(a[0], a[1], b[0], b[1]);
        }
    }

    function disc(cx, cy, r, colour) as Void {
        dc.setColor(colour, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(x(cx), y(cy), len(r));
    }

    function ring(cx, cy, r, width, colour) as Void {
        pen(width, colour);
        dc.drawCircle(x(cx), y(cy), len(r));
    }

    function text(cx, cy, font, value, colour) as Void {
        dc.setColor(colour, Graphics.COLOR_TRANSPARENT);
        dc.drawText(x(cx), y(cy), font, value, Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
    }

    // The largest of the fonts whose rendering of value fits the width (design units).
    function fit(value, fonts as Lang.Array, width) {
        for (var i = 0; i < fonts.size(); i += 1) {
            if (dc.getTextWidthInPixels(value, fonts[i]) <= width * k) {
                return fonts[i];
            }
        }
        return fonts[fonts.size() - 1];
    }
}

module Face {
    const GROUND = 410;
    const RUN_X = 186;
    const PX_PER_M = 12.0;
    // Where the sprite box sits in the design space (from the sprite generator).
    const SPRITE_X = 72;
    const SPRITE_Y = 285;

    // The pupil for a sprite drawn with an empty eye (SpriteEyes). left and top: the sprite's
    // top-left corner on screen; k: screen pixels per design pixel; eye: [x, y] from
    // SpriteEyes; look: :scenery (up and ahead), :level, :ahead, :down or :you. Offsets are the
    // design's, in the sprite's 0.68 scale, and keep the pupil inside the 15-pixel eye.
    function pupil(dc as Graphics.Dc, left, top, k, eye as Lang.Array, look) as Void {
        var dx = 6.8;
        var dy = -4.8;
        if (look == :level) {
            dx = 5.4;
            dy = 0.7;
        } else if (look == :ahead) {
            dx = 7.5;
            dy = 0.0;
        } else if (look == :down) {
            dx = 6.8;
            dy = 4.8;
        } else if (look == :you) {
            dx = 0.0;
            dy = 1.4;
        }
        var cx = left + (eye[0] + dx) * k;
        var cy = top + (eye[1] + dy) * k;
        dc.setColor(Ink.BLACK, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(cx.toNumber(), cy.toNumber(), (7.1 * k + 0.5).toNumber());
        var shine = (1.8 * k + 0.5).toNumber();
        dc.setColor(Ink.CREAM, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle((cx + 2.0 * k).toNumber(), (cy - 2.7 * k).toNumber(), shine < 1 ? 1 : shine);
    }

    // The heart-rate zone across the top of the dial: five segments, the current one lit
    // and thicker. No zone (no heart rate, or zones unavailable) leaves all five dim.
    function arc(p as Painter, zone) as Void {
        var r = p.len(212);
        for (var i = 0; i < 5; i += 1) {
            var lit = zone != null && zone == i + 1;
            p.dc.setPenWidth(p.len(lit ? 16 : 10));
            p.dc.setColor(lit ? Ink.ZONE[i] : Ink.ZONE_DIM[i], Graphics.COLOR_TRANSPARENT);
            var start = 150 - i * 24 - 1.5;
            p.dc.drawArc(p.x(227), p.y(227), r, Graphics.ARC_CLOCKWISE, start, start - 21);
        }
    }

    function heart(p as Painter, cx, cy, s) as Void {
        p.disc(cx - 6 * s, cy - 2 * s, 7 * s, Ink.CORAL);
        p.disc(cx + 6 * s, cy - 2 * s, 7 * s, Ink.CORAL);
        p.poly([[cx - 12.6 * s, cy], [cx + 12.6 * s, cy], [cx, cy + 14 * s]], Ink.CORAL);
    }

    // Pace: a running shoe with two speed lines.
    function shoe(p as Painter, cx, cy) as Void {
        var x0 = cx + 4;
        p.poly([[x0 - 10, cy + 8], [x0 - 10, cy - 3], [x0 - 7, cy - 9], [x0 - 3, cy - 9], [x0 + 1, cy - 4],
                [x0 + 8, cy - 1], [x0 + 13, cy + 1], [x0 + 14, cy + 5], [x0 + 14, cy + 8]], Ink.DIM);
        p.pen(2, Ink.BLACK);
        p.line(x0 - 10, cy + 4.5, x0 + 14, cy + 4.5);
        p.pen(2.4, Ink.DIM);
        p.line(x0 - 15, cy - 3, x0 - 22, cy - 3);
        p.line(x0 - 14, cy + 3, x0 - 24, cy + 3);
    }

    // Time: a stopwatch.
    function stopwatch(p as Painter, cx, cy) as Void {
        p.ring(cx, cy + 3, 11, 2.8, Ink.DIM);
        p.dc.setColor(Ink.DIM, Graphics.COLOR_TRANSPARENT);
        p.dc.fillRectangle(p.x(cx - 3.5), p.y(cy - 13), p.len(7), p.len(4));
        p.pen(2.4, Ink.DIM);
        p.line(cx, cy + 3, cx, cy - 4);
        p.line(cx, cy + 3, cx + 4, cy + 6);
        p.line(cx + 9, cy - 6, cx + 12, cy - 9);
    }

    // Distance: a dashed route ending at a map pin.
    function pin(p as Painter, cx, cy) as Void {
        var x0 = cx - 2;
        p.pen(2.4, Ink.DIM);
        p.line(x0 - 14, cy + 10, x0 - 11, cy + 6);
        p.line(x0 - 7, cy + 5, x0 - 3, cy + 8);
        p.line(x0 + 1, cy + 9, x0 + 4, cy + 7);
        p.disc(x0 + 9, cy - 6, 7, Ink.DIM);
        p.poly([[x0 + 2.5, cy - 4], [x0 + 15.5, cy - 4], [x0 + 9, cy + 8]], Ink.DIM);
        p.disc(x0 + 9, cy - 6, 2.6, Ink.BLACK);
    }

    // Heart rate large on top, pace | time | distance under icons, the lane below.
    function numbers(p as Painter, hr, pace, time, dist) as Void {
        heart(p, 110, 126, 1.4);
        var hrText = (hr == null) ? "--" : hr.format("%d");
        p.text(242, 132, p.fit(hrText, [Graphics.FONT_NUMBER_THAI_HOT, Graphics.FONT_NUMBER_HOT], 230), hrText, Ink.CREAM);
        p.pen(2, Ink.LINE);
        p.line(30, 196, 424, 196);
        p.line(38, 296, 416, 296);
        p.line(166, 206, 166, 286);
        p.line(288, 206, 288, 286);
        shoe(p, 105, 224);
        stopwatch(p, 227, 224);
        pin(p, 349, 224);
        var fonts = [Graphics.FONT_NUMBER_MILD, Graphics.FONT_LARGE, Graphics.FONT_MEDIUM, Graphics.FONT_SMALL];
        p.text(105, 264, p.fit(pace, fonts, 112), pace, Ink.CREAM);
        p.text(227, 264, p.fit(time, fonts, 112), time, Ink.CREAM);
        p.text(349, 264, p.fit(dist, fonts, 112), dist, Ink.CREAM);
    }

    // A whole-kilometre (or mile) flag at design x; gold once the runner has passed it.
    function flag(p as Painter, fx, n, reached) as Void {
        var top = GROUND - 80;
        p.dc.setColor(reached ? Ink.CREAM : Ink.OFF, Graphics.COLOR_TRANSPARENT);
        p.dc.fillRectangle(p.x(fx), p.y(top), p.len(4) < 2 ? 2 : p.len(4), p.len(80));
        var cloth = [[fx + 4, top], [fx + 44, top], [fx + 36, top + 13], [fx + 44, top + 26], [fx + 4, top + 26]];
        if (reached) {
            p.poly(cloth, Ink.GOLD);
        } else {
            p.outline(cloth, 2, Ink.DIM);
        }
        p.text(fx + 21, top + 13, Graphics.FONT_XTINY, n.format("%d"), reached ? Ink.BLACK : Ink.DIM);
    }

    // One cream stamp for a completed workout step.
    function stamp(p as Painter) as Void {
        p.ring(316, 352, 24, 4, Ink.CREAM);
        p.pen(6, Ink.CREAM);
        p.line(303, 352, 311, 360);
        p.line(311, 360, 327, 342);
    }

    // The gold check and confetti for the last step of today's Coach workout.
    function done(p as Painter) as Void {
        var pieces = [[-52, -30], [-40, 8], [40, -36], [54, 4], [-20, -46], [22, -50], [-58, -6], [60, -20], [-30, 28], [34, 30]];
        var colours = [Ink.GOLD, Ink.BLUE, Ink.CREAM, Ink.CORAL, Ink.GREEN];
        for (var i = 0; i < pieces.size(); i += 1) {
            var px = 306 + pieces[i][0];
            var py = 360 + pieces[i][1];
            var d = (i % 2 == 0) ? 4 : -4;
            p.poly([[px - 3 + d, py - 6], [px + 3 + d, py - 6], [px + 3 - d, py + 6], [px - 3 - d, py + 6]], colours[i % colours.size()]);
        }
        p.disc(306, 360, 30, Ink.GOLD);
        p.pen(8, Ink.BLACK);
        p.line(290, 360, 301, 371);
        p.line(301, 371, 322, 346);
    }

    // A chain link, crossed out when the watch is not getting through to the Coach.
    function link(p as Painter, cx, cy, broken) as Void {
        p.ring(cx - 6, cy, 7, 2.6, Ink.DIM);
        p.ring(cx + 6, cy, 7, 2.6, Ink.DIM);
        if (broken) {
            p.pen(3, Ink.CORAL);
            p.line(cx - 13, cy + 11, cx + 13, cy - 11);
        }
    }

    // Three dots, one more lit each second, while the watch is asking for a code.
    function waiting(p as Painter, cx, cy, tick) as Void {
        for (var i = 0; i < 3; i += 1) {
            p.disc(cx - 14 + i * 14, cy, 4, (i <= tick % 3) ? Ink.CREAM : Ink.OFF);
        }
    }
}

// The mountain trail behind the runner: far peaks, pines, then rocks, grass and stones near
// the runner. Each layer scrolls left by the distance run times its own speed, and repeats
// more than twice its one-second shift apart, or at one frame a second the eye reads it as
// moving backwards.
module Trail {
    // Shape noise in [0, 1) for object n of a layer, the same every time it is drawn.
    function noise(n, seed) {
        var v = (n * 7919 + seed * 104729) % 10007;
        if (v < 0) {
            v += 10007;
        }
        return v / 10007.0;
    }

    // Object positions of a layer: [index, design x] for every object in view.
    function placed(shift, period, jitter, seed) as Lang.Array {
        var out = [];
        var first = RunMath.floorDiv(shift - 140, period);
        var last = RunMath.floorDiv(shift + 600, period);
        for (var n = first; n <= last; n += 1) {
            out.add([n, n * period + (noise(n, seed) * 2 - 1) * jitter * period - shift]);
        }
        return out;
    }

    function draw(p as Painter, scroll) as Void {
        var g = Face.GROUND;
        var objs = placed(scroll * 0.1, 150, 0.15, 11);
        for (var i = 0; i < objs.size(); i += 1) {
            var n = objs[i][0];
            var cx = objs[i][1];
            var h = 50 + 30 * noise(n, 12);
            var w = 70 + 30 * noise(n, 13);
            p.poly([[cx - w, g], [cx, g - h], [cx + w, g]], Ink.FAR);
            p.poly([[cx - w * 0.22, g - h * 0.78], [cx, g - h], [cx + w * 0.22, g - h * 0.78]], Ink.SNOW);
        }
        objs = placed(scroll * 0.45, 100, 0.2, 21);
        for (var i = 0; i < objs.size(); i += 1) {
            var n = objs[i][0];
            var cx = objs[i][1];
            var h = 40 + 22 * noise(n, 22);
            var w = h * 0.36;
            p.dc.setColor(Ink.TRUNK, Graphics.COLOR_TRANSPARENT);
            p.dc.fillRectangle(p.x(cx - 2), p.y(g - 10), p.len(4), p.len(10));
            p.poly([[cx, g - h], [cx + w, g - h * 0.45], [cx + w * 0.55, g - h * 0.45], [cx + w * 1.2, g - 10],
                    [cx - w * 1.2, g - 10], [cx - w * 0.55, g - h * 0.45], [cx - w, g - h * 0.45]], Ink.PINE);
        }
        p.pen(3, Ink.CREAM);
        p.line(0, g, 454, g);
        objs = placed(scroll, 140, 0.12, 31);
        for (var i = 0; i < objs.size(); i += 1) {
            var cx = objs[i][1];
            p.poly([[cx - 9, g + 14], [cx - 5, g + 5], [cx + 4, g + 3], [cx + 10, g + 14]], Ink.NEAR);
            p.pen(1.6, Ink.OFF);
            p.line(cx + 14, g + 9, cx + 30, g + 9);
        }
        objs = placed(scroll, 115, 0.12, 41);
        for (var i = 0; i < objs.size(); i += 1) {
            var cx = objs[i][1];
            p.pen(2, Ink.NEAR);
            p.line(cx, g + 30, cx + 3, g + 22);
            p.line(cx + 3, g + 22, cx + 5, g + 30);
            p.line(cx + 5, g + 30, cx + 8, g + 24);
            p.line(cx + 8, g + 24, cx + 10, g + 30);
        }
        objs = placed(scroll * 1.5, 210, 0.2, 51);
        for (var i = 0; i < objs.size(); i += 1) {
            var n = objs[i][0];
            p.dc.setColor(Ink.NEAR, Graphics.COLOR_TRANSPARENT);
            p.dc.fillEllipse(p.x(objs[i][1]), p.y(g + 40), p.len(4 + 3 * noise(n, 52)), p.len(3));
        }
    }
}
