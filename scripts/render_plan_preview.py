#!/usr/bin/env python3
"""Render the current PlanState as a single self-contained HTML preview.

The preview answers, in one screen, the questions the athlete actually asks: what
is this week, what changed and why, what counts as having done it, and what would
end the cycle early. Everything it shows is read from the store -- plan,
DecisionEvent and receipt -- so it can be regenerated after every decision instead
of hand-drawn once.

It is a projection and nothing more. Every number on the page is read from the
plan, the event, or delivery evidence; the page never forecasts a fitness gain,
never fills in a measurement date, and never reports a delivery state the store
does not carry. A view that quietly coaches is a second coaching layer, and two
coaching layers disagree.

Structure bars are read from each session's own validated `time_axis` plan -- the same
executable content delivery derives its provider payload from -- never from reparsing
what Intervals.icu echoes back. A `time_axis` session renders its bar whether or not it
has been published yet, because the structure was already decided by the plan, not by
delivery; a `movement_list` or `unstructured` session renders none, on purpose.

The zone colours are shared with the personal-os training visualisation prototype
on purpose. The same run must not read as two different efforts on two surfaces.

Usage:
    python3 scripts/render_plan_preview.py --out /path/outside/the/repo.html
    python3 scripts/render_plan_preview.py --events exported-events.json --out ...
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Running a file under scripts/ puts scripts/, not the repository root, on
# sys.path. Make the documented `python3 scripts/render_plan_preview.py` command
# work from a checkout without requiring an editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from garmin_coach_loop.store import default_state_dir, status_store


DEFAULT_STATE_DIR = default_state_dir()

# Shared with personal-os exploration/tredict-viz-prototype: zone colour is a
# cross-repo token, not a per-page choice. `zx` is this page's own neutral: it
# means "outside the contract", not an intensity.
ZONE_COLOURS = {
    "z1": "#8e8e93",
    "z2": "#30d158",
    "z3": "#ffcc00",
    "z4": "#ff9500",
    "z5": "#ff3b30",
    "strength": "#5856d6",
    "zx": "rgba(120,120,128,.35)",
}
# Only the adaptations in contracts/plan-state.schema.json. A value outside the
# contract degrades to the neutral zone; the page must not answer schema drift by
# inventing a product vocabulary of its own.
ADAPTATION_ZONE = {
    "recovery": "z1",
    "aerobic_base": "z2",
    "threshold": "z4",
    "vo2": "z5",
    "strength": "strength",
    "hypertrophy": "strength",
    "power": "strength",
}
ZONE_NAME = {
    "z1": "恢復", "z2": "輕鬆有氧", "z3": "中強度",
    "z4": "門檻", "z5": "高強度", "strength": "肌力", "zx": "未分類",
}
SPORT_LABEL = {
    "running": "跑步", "strength": "重訓", "mobility": "活動度",
    "recovery": "恢復", "rest": "休息", "cycling": "騎車",
    "swimming": "游泳", "hiking": "健行", "rowing": "划船",
}
# The delivery ladder, 1:1 with the contract, which now stops where this product's
# knowledge stops. `intervals_accepted` is the finish line and is styled as one; the two
# hops after it -- intervals' background sync to Garmin Connect, and the watch pulling
# from Connect -- belong to the athlete and are named once in the footer rather than
# tracked with states nothing could ever set.
DELIVERY_LABEL = {
    "not_published": ("chip-warn", "未推送"),
    "intervals_accepted": ("chip-ok", "已送出 · Intervals"),
}
WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


# --------------------------------------------------------------------------------------
# Store reading
# --------------------------------------------------------------------------------------


def load_store(state_dir: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the doctor-verified current plan and latest coaching DecisionEvent."""
    plan = status_store(state_dir)["current_plan"]
    index = json.loads((state_dir / "store.json").read_text(encoding="utf-8"))
    current_sequence = int(index["current_sequence"])
    for sequence in range(current_sequence, 1, -1):
        matches = sorted((state_dir / "commits").glob(f"{sequence:08d}-*"))
        if len(matches) != 1:
            continue
        event_path = matches[0] / "event.json"
        if not event_path.exists():
            continue
        event = json.loads(event_path.read_text(encoding="utf-8"))
        reason_codes = event.get("reason_codes") or []
        if reason_codes not in (["planned_actual_reconciled"], ["delivery_verified"]):
            return plan, event
    return plan, None


# --------------------------------------------------------------------------------------
# Session plan -> structure bar
# --------------------------------------------------------------------------------------


def _step_zone(target: dict[str, Any], threshold_sec: int | None) -> str:
    """Classify one work step's zone from its own structured target, never from prose.

    Only a pace target carries a number this page can compare against the athlete's
    own threshold pace, the same delta bands the bar has always used for percent-of-
    threshold effort. `open` prescribes no intensity by design, and `hr_ceiling` is
    validated to stand alone -- never inside a repeat, never mixed with a pace target
    in the same workout (garmin_coach_loop/validation.py, issue #38) -- so neither
    carries a number with a comparable baseline to bucket against. Inventing one would
    be exactly the unsupported precision this page must not add; both render at the
    "nothing prescribed above easy" zone instead of a guessed number.
    """
    if target.get("kind") == "pace":
        low, high = target.get("low_seconds_per_km"), target.get("high_seconds_per_km")
        if (
            threshold_sec
            and isinstance(low, (int, float)) and not isinstance(low, bool)
            and isinstance(high, (int, float)) and not isinstance(high, bool)
        ):
            delta = threshold_sec - (low + high) / 2  # positive = faster than threshold
            if delta > 25:
                return "z5"
            if delta > -5:
                return "z4"
            if delta > -45:
                return "z2"
    return "z1"


def _step_seconds(step: dict[str, Any]) -> float | None:
    """How long one work step takes, or `None` when the plan does not say.

    The bar's width is a share of time, so a distance step needs converting -- and its
    own pace target already says how fast it covers the ground, which makes that a
    read, not a guess. An unpaced distance step (an `open` recovery jog, say) carries
    no such number, and neither the athlete's threshold pace nor the bare metre count
    is one: the first prescribes an intensity this step deliberately did not, and the
    second turns 400 metres into 400 seconds, which drew a recovery nearly twice the
    length of the 1000m work it followed (issue #18).

    `None` is not zero. Zero would drop the step out of the bar entirely, which is the
    vanishing distance step this bar was rewritten to fix; the caller renders an
    unknown duration at a fixed width instead, claiming no proportion for it.
    """
    duration = step.get("duration") if isinstance(step.get("duration"), dict) else {}
    if duration.get("kind") == "time":
        seconds = duration.get("seconds")
        valid = isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        return float(seconds) if valid else 0.0
    meters = duration.get("meters")
    if not isinstance(meters, (int, float)) or isinstance(meters, bool):
        return 0.0
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    if target.get("kind") == "pace":
        low, high = target.get("low_seconds_per_km"), target.get("high_seconds_per_km")
        if (
            isinstance(low, (int, float)) and not isinstance(low, bool)
            and isinstance(high, (int, float)) and not isinstance(high, bool)
        ):
            return meters / 1000 * (low + high) / 2
    return None


def _work_segment(step: dict[str, Any], threshold_sec: int | None) -> dict[str, Any]:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    return {
        "seconds": _step_seconds(step),
        "zone": _step_zone(target, threshold_sec),
        "label": step.get("name", ""),
    }


def _time_axis_segments(steps: Any, threshold_sec: int | None) -> list[dict[str, Any]]:
    """Flatten a `time_axis` plan's work/repeat tree into proportional bar segments.

    A repeat step's own `repetitions` count is read from that step alone and applied
    only to its own children, so it cannot survive past the step that declared it --
    which is what let a single top-level cooldown draw five times (issue #118 defect
    2). A repeat's children are always `work` steps (validation.py forbids a nested
    repeat), so this never recurses.
    """
    segments: list[dict[str, Any]] = []
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "work":
            segments.append(_work_segment(step, threshold_sec))
        elif kind == "repeat":
            children = [
                child for child in (step.get("steps") or [])
                if isinstance(child, dict) and child.get("kind") == "work"
            ]
            repetitions = step.get("repetitions")
            if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
                repetitions = 1
            for _ in range(repetitions):
                segments.extend(_work_segment(child, threshold_sec) for child in children)
    return segments


# --------------------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------------------


def esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def pace_str(sec_per_km: int | None) -> str:
    if not sec_per_km:
        return "—"
    return f"{sec_per_km // 60}:{sec_per_km % 60:02d}/km"


def zone_for(session: dict) -> str:
    """Zone token for a session, or the neutral token for an off-contract adaptation.

    Defaulting an unrecognised adaptation to `z1` used to read as "recovery", which
    is a training claim the plan never made.
    """
    return ADAPTATION_ZONE.get(session.get("adaptation"), "zx")


def render_structure_bar(session: dict, threshold_sec: int | None) -> str:
    """Render this session's own validated plan as proportional structure segments.

    Structure comes only from `session["plan"]` -- the executable content delivery
    itself derives its provider payload from -- never from Intervals workout text.
    Provider read-back is delivery evidence, not a second statement of what the
    workout is; reparsing it produced the defects this replaces (issue #118, #75).
    Dispatch is on `plan.kind` alone, exactly as delivery and validation dispatch,
    never on `sport`. A `movement_list` plan already reaches the athlete through the
    prescription paragraph this bar sits beside, so it renders nothing extra here
    rather than inventing a second visual language for sets and loads; an
    `unstructured` plan renders nothing because it declares nothing.
    """
    plan = session.get("plan")
    if not isinstance(plan, dict) or plan.get("kind") != "time_axis":
        return ""
    segments = [
        segment for segment in _time_axis_segments(plan.get("steps"), threshold_sec)
        if segment["seconds"] is None or segment["seconds"] > 0
    ]
    # Only the steps whose duration the plan states share the width out between them,
    # so what a reader compares is still elapsed time. A step whose duration is unknown
    # renders at one fixed width in its own zone colour: present and legible, claiming
    # no share of anybody else's time (issue #18).
    timed = [segment for segment in segments if segment["seconds"] is not None]
    total = sum(segment["seconds"] for segment in timed)
    if not segments or (timed and total <= 0):
        return ""
    parts = "".join(
        f'<i class="u" style="background:var(--{segment["zone"]})"'
        f' title="{esc(segment["label"])}｜未規定時間"></i>'
        if segment["seconds"] is None else
        f'<i style="width:{segment["seconds"] / total * 100:.2f}%;'
        f'background:var(--{segment["zone"]})" title="{esc(segment["label"])}"></i>'
        for segment in segments
    )
    return f'<div class="sbar">{parts}</div>'


def delivery_chip(session: dict) -> str:
    execution = session.get("execution") or {}
    if not execution.get("publish_supported"):
        return '<span class="chip chip-muted">文字處方</span>'
    state = execution.get("delivery_state")
    style, label = DELIVERY_LABEL.get(state, ("chip-muted", str(state or "交付狀態未知")))
    return f'<span class="chip {style}">{esc(label)}</span>'


def render_session_row(
    session: dict,
    events: dict,
    threshold_sec: int | None,
    *,
    today: date | None = None,
    show_date: bool = True,
) -> str:
    # `events` (optional provider read-back) is accepted here only for interface
    # stability -- the structure bar no longer reads it; see render_structure_bar.
    zone = zone_for(session)
    day = date.fromisoformat(session["scheduled_date"])
    done = session.get("match_status") == "completed"
    missed = session.get("match_status") == "missed"
    is_today = today is not None and day == today

    metrics = [("時長", f'{session.get("planned_minutes", 0)} 分')]
    if session.get("priority") == "anchor":
        metrics.append(("定位", "主課"))
    if session.get("hard"):
        metrics.append(("強度", "高"))
    metric_html = "".join(
        f'<div class="m"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in metrics
    )

    prescription = session.get("prescription")
    rx = f'<p class="rx">{esc(prescription)}</p>' if prescription else ""

    # The coach's own sentence about this session, shown where the athlete reads the
    # session rather than only where the watch does. It is the half that says *why* the
    # week looks like this, and a note that travels to Intervals but is invisible here
    # would be visible on the watch and nowhere the plan is actually reviewed.
    note = session.get("coach_note")
    coach_note = f'<p class="snote">{esc(note)}</p>' if note else ""

    # One saturated chip per row at most, and it belongs to "completed" -- whether the
    # athlete actually trained outranks how the workout traveled (#25). Delivery only
    # speaks while the session is still ahead. The structure bar itself has no "missing"
    # state left to announce: it is read straight from the session's own plan, which the
    # store already validated, so a time_axis session always has one -- published or not
    # (issue #118). Whether the watch actually got it is a separate fact, and it stays
    # the delivery chip's job to say so.
    structure_bar = render_structure_bar(session, threshold_sec)
    if session.get("sport") == "rest":
        chips = ""  # a rest day neither travels nor carries chips, done or not
    elif done:
        chips = '<span class="chip chip-done">已完成</span>'
    else:
        chips = delivery_chip(session)

    today_tag = '<span class="today-tag">今天</span>' if is_today else ""
    if show_date:
        sday = f'<div class="sday"><b>{day.month}/{day.day}</b><span>週{WEEKDAY[day.weekday()]}</span>{today_tag}</div>'
    else:
        # Same calendar day as the previous row: keep the grid column, drop the repeat.
        sday = f'<div class="sday">{today_tag}</div>'

    row_class = "srow" + (" missed" if missed else "") + (" today" if is_today else "")
    return f"""
    <div class="{row_class}" style="--zone:var(--{zone})">
      {sday}
      <div class="sbody">
        <div class="shead">
          <span class="ssport">{esc(SPORT_LABEL.get(session.get("sport"), session.get("sport")))}</span>
          {chips}
        </div>
        <p class="spurpose">{esc(session.get("purpose", ""))}</p>
        {rx}
        {coach_note}
        {structure_bar}
      </div>
      <div class="smetrics">{metric_html}</div>
    </div>"""


def render_zone_distribution(sessions: list[dict]) -> str:
    totals: dict[str, float] = {}
    for session in sessions:
        minutes = session.get("planned_minutes") or 0
        if not minutes:
            continue
        zone = zone_for(session)
        totals[zone] = totals.get(zone, 0) + minutes
    grand = sum(totals.values())
    if not grand:
        return ""
    order = ["z1", "z2", "z3", "z4", "z5", "strength", "zx"]
    bar = "".join(
        f'<i style="width:{totals[z] / grand * 100:.2f}%;background:var(--{z})"></i>'
        for z in order if z in totals
    )
    legend = "".join(
        f'<span class="lg"><i style="background:var(--{z})"></i>{ZONE_NAME[z]}'
        f' <b>{int(totals[z])}分</b> <em>{totals[z] / grand * 100:.0f}%</em></span>'
        for z in order if z in totals
    )
    return f'<div class="zbar">{bar}</div><div class="legend">{legend}</div>'


def week_tally(sessions: list[dict]) -> dict[str, tuple[int, int]]:
    """Count planned vs completed sessions per training role for this week."""
    roles = {"quality": (0, 0), "easy": (0, 0), "strength": (0, 0)}
    for session in sessions:
        sport, adaptation = session.get("sport"), session.get("adaptation")
        if sport == "running" and adaptation in {"threshold", "vo2"}:
            role = "quality"
        elif sport == "running":
            role = "easy"
        elif sport == "strength":
            role = "strength"
        else:
            continue
        planned, done = roles[role]
        roles[role] = (planned + 1, done + (1 if session.get("match_status") == "completed" else 0))
    return roles


def _action_sessions(sessions: list[dict], today: date) -> tuple[str, list[dict]]:
    actionable = {"planned", "moved", "replaced"}
    same_day = [
        session for session in sessions
        if session.get("scheduled_date") == today.isoformat()
        and session.get("match_status") in actionable
    ]
    if same_day:
        return "今天", same_day
    upcoming = [
        session for session in sessions
        if session.get("match_status") in actionable
        and session.get("scheduled_date", "") > today.isoformat()
    ]
    if not upcoming:
        return "接下來", []
    next_date = upcoming[0]["scheduled_date"]
    return f"下一個訓練日 · {next_date}", [s for s in upcoming if s["scheduled_date"] == next_date]


def _render_action_focus(plan: dict, event: dict | None, sessions: list[dict], today: date) -> str:
    label, focused = _action_sessions(sessions, today)
    if focused:
        focus = "".join(
            f"<li><b>{esc(SPORT_LABEL.get(session.get('sport'), session.get('sport')))}</b> "
            f"{esc(session.get('purpose', ''))}"
            + (f"<span>{esc(session.get('prescription'))}</span>" if session.get("prescription") else "")
            + "</li>"
            for session in focused
        )
    else:
        focus = "<li>目前沒有尚待執行的本週課表。</li>"
    change = (event or {}).get("change") or {}
    evidence = (event or {}).get("evidence") or []
    change_summary = change.get("summary") or "目前沒有可呈現的上一版 coaching 變更。"
    anchors = [
        session.get("purpose", "")
        for session in sessions
        if session.get("priority") == "anchor" and session.get("match_status") == "planned"
    ]
    anchor_html = "".join(f"<li>{esc(anchor)}</li>" for anchor in anchors[:3])
    reasons = [
        item.get("observation")
        for item in evidence[:3]
        if isinstance(item, dict) and item.get("observation")
    ]
    reason_html = (
        "<ul class=\"reason-list\">"
        + "".join(f"<li>{esc(reason)}</li>" for reason in reasons)
        + "</ul>"
        if reasons
        else '<p class="reason">目前 DecisionEvent 沒有額外的證據摘要。</p>'
    )
    delivery_rows: list[str] = []
    for session in sessions:
        execution = session.get("execution") or {}
        # No sport test: publish_supported already answers whether delivery has anything
        # to send, for a run and for the strength day that reaches the calendar as a title.
        if (
            session.get("match_status") not in {"planned", "moved", "replaced"}
            or execution.get("publish_supported") is not True
        ):
            continue
        state = execution.get("delivery_state")
        state_label = DELIVERY_LABEL.get(state, ("", "未知交付狀態"))[1]
        next_step = "待精確 preview 確認" if state == "not_published" else state_label
        delivery_rows.append(
            f"<li><b>{esc(session.get('scheduled_date'))} · {esc(session.get('purpose', '課表'))}</b>"
            f"<span>{esc(next_step)}</span></li>"
        )
    delivery_html = (
        "".join(delivery_rows)
        if delivery_rows
        else "<li>本週沒有待交付的課表。</li>"
    )
    return f"""
  <section class="card action-focus">
    <h2>現在照這份計畫做</h2>
    <div class="action-grid">
      <div><h3>現在的目標</h3><p class="summary">{esc(plan['goal'].get('outcome', ''))}</p><p class="minor">主攻 {esc(plan['cycle'].get('primary_adaptation'))} · 維持 {esc(plan['cycle'].get('maintenance_adaptation'))}</p></div>
      <div><h3>{esc(label)}</h3><ul class="action-list">{focus}</ul></div>
      <div><h3>本週方向</h3><p>{esc(plan['week'].get('intent', ''))}</p><ul class="reason-list">{anchor_html}</ul></div>
      <div><h3>相較上一版</h3><p>{esc(change_summary)}</p><b class="reason-title">真正重要的原因</b>{reason_html}</div>
      <div class="delivery-focus"><h3>交付</h3><ul class="action-list">{delivery_html}</ul></div>
    </div>
  </section>"""


def render_page(plan: dict, event: dict | None, events: dict, today: date) -> str:
    cycle, goal = plan["cycle"], plan["goal"]
    baseline = plan.get("athlete_baseline") or {}
    sessions = sorted(plan["week"]["sessions"], key=lambda s: (s["scheduled_date"], s["sport"]))
    threshold_sec = baseline.get("threshold_pace_sec_per_km")

    start, end = date.fromisoformat(cycle["start"]), date.fromisoformat(cycle["end"])
    day_n = (today - start).days + 1
    total_days = (end - start).days + 1

    tally = week_tally(sessions)
    run_minutes = sum(s.get("planned_minutes") or 0 for s in sessions if s.get("sport") == "running")
    action_focus = _render_action_focus(plan, event, sessions, today)

    # --- change card -------------------------------------------------------------------
    change_html = ""
    if event:
        change = event.get("change", {})
        codes = " ".join(f'<span class="code">{esc(c)}</span>' for c in event.get("reason_codes", []))
        evidence = "".join(
            f'<li><b>{esc(e["field"])}</b>{esc(e["observation"])}</li>' for e in event.get("evidence", [])
        )
        # The change card sits after the weekly menu, folded: "what do I do this week"
        # is the action view and must come first; the latest revision is context a
        # reader opens on demand (#17 gap 1). This stays the latest event only -- the
        # full history lives in `cli history`, and this page never pretends otherwise.
        change_html = f"""
        <details class="card accent changelog">
          <summary><span class="changelog-title" role="heading" aria-level="2">這一輪改了什麼 <span class="vtag">v{event.get('plan_version_before')} → v{event.get('plan_version_after')}</span></span></summary>
          <p class="summary">{esc(change.get('summary', ''))}</p>
          <div class="ba">
            <div class="b"><span>原本</span><p>{esc(change.get('before', ''))}</p></div>
            <div class="a"><span>改成</span><p>{esc(change.get('after', ''))}</p></div>
          </div>
          <div class="codes">{codes}</div>
          <details><summary>依據的證據（{len(event.get('evidence', []))} 條）</summary><ul class="ev">{evidence}</ul></details>
          <p class="next"><b>下次要決定：</b>{esc(event.get('next_review_condition', ''))}</p>
        </details>"""

    # The rest of the cycle, as an outline (issue #61). Rendered from the plan's own
    # `cycle.outlook`, and deliberately without a delivery chip or a structure bar: these
    # weeks carry no sessions the product could publish, and a card that looked like the
    # weekly menu would invite the athlete to treat them as one.
    outlook_cards = "".join(
        f'<div class="ow"><b>{esc(week.get("week_start"))}</b>'
        f'<p class="summary">{esc(week.get("intent", ""))}</p>'
        "<ul class=\"plain\">"
        + "".join(f"<li>{esc(item)}</li>" for item in week.get("key_sessions", []))
        + "</ul>"
        f'<p class="reason">{esc(week.get("relation_to_primary", ""))}</p></div>'
        for week in cycle.get("outlook", [])
    )
    outlook_html = (
        f"""
  <section class="card">
    <h2>接下來三週的方向</h2>
    <p class="sub" style="margin:-6px 0 12px">輪廓，不是課表：這幾週還沒有可以送到手錶的 session，配速與重量等到那一週變成本週才會定下來。</p>
    <div class="outlook">{outlook_cards}</div>
  </section>"""
        if outlook_cards
        else """
  <section class="card">
    <h2>接下來三週的方向</h2>
    <p class="sub">這是這個週期的最後一週，後面沒有其他週了。</p>
  </section>"""
    )

    evidence_items = "".join(f"<li>{esc(item)}</li>" for item in cycle.get("planned_evidence", []))
    adjust_items = "".join(f"<li>{esc(item)}</li>" for item in cycle.get("adjust_conditions", []))
    stop_items = "".join(f"<li>{esc(item)}</li>" for item in cycle.get("stop_conditions", []))

    row_parts: list[str] = []
    previous_date: str | None = None
    for session in sessions:
        row_parts.append(
            render_session_row(
                session,
                events,
                threshold_sec,
                today=today,
                show_date=session["scheduled_date"] != previous_date,
            )
        )
        previous_date = session["scheduled_date"]
    rows = "".join(row_parts)
    # The bar is a share of time, so it has to say so -- and say which steps are not in
    # it, because a step whose duration the plan never stated is drawn at a fixed sliver
    # rather than at a guessed length (issue #18). Written once under the week rather
    # than beside every row, and only when a bar was actually drawn.
    structure_caption = (
        '<p class="sub" style="margin:12px 0 0">'
        "結構條的寬度是課表寫下的時間比例；沒有規定時間的段落（例如沒有配速的距離恢復）"
        "以固定窄條標示，不佔任何時間比例。"
        "</p>"
        if 'class="sbar"' in rows
        else ""
    )
    outcome = goal.get("outcome", "")
    headline = outcome if len(outcome) <= 40 else outcome[:40] + "…"

    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>訓練計畫預覽 — {esc(plan['plan_id'])} v{plan['version']}</title>
<style>
:root {{
  --bg:#f2f2f7; --paper:#fff; --ink:#000; --ink-2:rgba(0,0,0,.78); --ink-3:rgba(0,0,0,.56);
  --ink-4:rgba(0,0,0,.36); --line:rgba(60,60,67,.14); --line-soft:rgba(60,60,67,.07);
  --ok:#28a745; --warn:#e08e00; --bad:#e0352b; --accent:#ff6a4d;
  --z1:{ZONE_COLOURS['z1']}; --z2:{ZONE_COLOURS['z2']}; --z3:{ZONE_COLOURS['z3']};
  --z4:{ZONE_COLOURS['z4']}; --z5:{ZONE_COLOURS['z5']}; --strength:{ZONE_COLOURS['strength']};
  --zx:{ZONE_COLOURS['zx']};
  --shadow:0 0 0 .5px rgba(0,0,0,.05), 0 2px 4px rgba(0,0,0,.04), 0 8px 20px rgba(20,30,60,.06);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#000; --paper:#1c1c1e; --ink:#fff; --ink-2:rgba(255,255,255,.8); --ink-3:rgba(255,255,255,.58);
    --ink-4:rgba(255,255,255,.38); --line:rgba(255,255,255,.16); --line-soft:rgba(255,255,255,.08);
    /* zx (unclassified) needs its own dark value: the light-mode grey collapses into
       the dark card background and reads as a hole in the zone bar. */
    --zx:rgba(174,174,182,.5);
    --shadow:0 0 0 .5px rgba(255,255,255,.06), 0 8px 20px rgba(0,0,0,.5);
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:20px 16px 48px; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang TC","Noto Sans TC",sans-serif;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:820px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }}
h2 {{ font-size:15px; margin:0 0 12px; display:flex; align-items:center; gap:8px; }}
h3 {{ font-size:12.5px; margin:18px 0 8px; color:var(--ink-3); font-weight:600;
  text-transform:uppercase; letter-spacing:.04em; }}
p {{ margin:0 0 8px; }}
.sub {{ color:var(--ink-3); font-size:12.5px; margin-bottom:18px; }}
.card {{ background:var(--paper); border-radius:16px; padding:18px; margin-bottom:14px; box-shadow:var(--shadow); }}
.card.accent {{ border-left:3px solid var(--accent); }}
.action-focus {{ border-top:3px solid var(--accent); }}
.action-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px 22px; }}
.action-grid > div {{ min-width:0; }}
.action-grid .delivery-focus {{ grid-column:1/-1; border-top:1px solid var(--line-soft); padding-top:12px; }}
.action-grid h3 {{ margin:0 0 5px; }}
.action-grid p {{ color:var(--ink-2); }}
.action-grid .summary {{ color:var(--ink); }}
.action-grid .minor {{ color:var(--ink-3); font-size:12px; }}
.action-list {{ margin:0; padding:0; list-style:none; }}
.action-list li {{ color:var(--ink-2); margin-bottom:6px; }}
.action-list b {{ color:var(--ink); }}
.action-list span {{ display:block; margin-top:2px; color:var(--ink-3); font-size:12px; }}
.reason {{ font-size:12px; color:var(--ink-3)!important; }}
.reason-title {{ display:block; font-size:11px; color:var(--ink-4); margin-top:8px; }}
.reason-list {{ margin:3px 0 0; padding-left:18px; color:var(--ink-3); font-size:12px; }}
.vtag {{ font-size:11px; font-weight:600; color:var(--accent); background:color-mix(in srgb,var(--accent) 12%,transparent);
  padding:2px 7px; border-radius:99px; }}
.summary {{ font-size:15px; line-height:1.45; color:var(--ink); font-weight:500; }}
.ba {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:12px 0; }}
.ba > div {{ padding:10px 12px; border-radius:10px; background:var(--bg); }}
.ba span {{ display:block; font-size:11px; color:var(--ink-4); margin-bottom:3px; }}
.ba p {{ margin:0; font-size:13px; color:var(--ink-2); }}
.ba .a {{ border-left:2px solid var(--ok); }}
.outlook {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
.outlook .ow {{ padding:12px 14px; border-radius:10px; background:var(--bg); border-left:2px solid var(--line-soft); }}
.outlook .ow > b {{ display:block; font-size:11px; color:var(--ink-4); margin-bottom:4px; }}
.outlook .ow .summary {{ font-size:13.5px; margin:0 0 6px; }}
.outlook .ow ul {{ margin:0 0 8px; }}
.code {{ display:inline-block; font-size:11px; font-family:ui-monospace,SFMono-Regular,monospace;
  color:var(--ink-3); background:var(--bg); padding:3px 8px; border-radius:6px; margin:0 4px 4px 0; }}
details {{ margin:10px 0 0; }}
summary {{ cursor:pointer; font-size:12.5px; color:var(--ink-3); }}
.ev {{ margin:8px 0 0; padding-left:0; list-style:none; }}
.ev li {{ font-size:12.5px; color:var(--ink-2); padding:6px 0; border-top:1px solid var(--line-soft); }}
.ev b {{ display:block; font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-4); font-weight:500; }}
.next {{ margin:12px 0 0; padding-top:10px; border-top:1px solid var(--line-soft);
  font-size:12.5px; color:var(--ink-2); }}
.srow {{ display:grid; grid-template-columns:52px 1fr auto; gap:14px; align-items:start;
  padding:14px 0 14px 12px; border-top:1px solid var(--line-soft); border-left:3px solid var(--zone); }}
.srow:first-of-type {{ border-top:none; }}
/* Completed sessions stay fully opaque -- 62% transparency read as "disabled control".
   The faded treatment belongs to sessions that genuinely did not happen. */
.srow.missed {{ opacity:.62; }}
.srow.today {{ background:color-mix(in srgb,var(--accent) 5%,transparent); border-radius:0 8px 8px 0; }}
.sday {{ text-align:center; }}
.sday b {{ display:block; font-size:16px; letter-spacing:-.01em; }}
.sday span {{ font-size:11px; color:var(--ink-4); }}
.today-tag {{ display:inline-block; margin-top:3px; font-size:10px; font-weight:600; color:var(--accent);
  border:1px solid var(--accent); border-radius:99px; padding:0 6px; }}
.shead {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:4px; }}
.ssport {{ font-size:13px; font-weight:600; }}
.chip {{ font-size:10.5px; padding:2px 7px; border-radius:99px; font-weight:500; }}
.changelog > summary {{ cursor:pointer; list-style:none; }}
.changelog > summary::-webkit-details-marker {{ display:none; }}
/* summary's content model is phrasing content, so the title is a span styled as the
   page's h2 and exposed as a heading via ARIA, not a real h2. */
.changelog-title {{ display:inline-flex; align-items:center; gap:8px; font-size:15px; font-weight:600; }}
.changelog > summary::after {{ content:"展開"; font-size:11px; color:var(--ink-4); margin-left:8px; }}
.changelog[open] > summary::after {{ content:"收合"; }}
.changelog[open] > summary {{ margin-bottom:12px; }}
.chip-ok {{ color:var(--ok); background:color-mix(in srgb,var(--ok) 14%,transparent); }}
.chip-warn {{ color:var(--warn); background:color-mix(in srgb,var(--warn) 16%,transparent); }}
.chip-muted {{ color:var(--ink-4); background:var(--bg); }}
/* The one saturated chip a row may carry: whether the athlete actually trained
   outranks how the workout traveled. */
.chip-done {{ color:#fff; background:var(--ok); font-weight:600; }}
.spurpose {{ font-size:13px; color:var(--ink-2); margin:0 0 6px; }}
.rx {{ font-size:12px; color:var(--ink-3); margin:0 0 8px; padding:8px 10px; background:var(--bg); border-radius:8px; }}
.snote {{ font-size:12px; color:var(--ink-2); margin:0 0 8px; padding:8px 10px; border-left:3px solid var(--zone); background:var(--bg); border-radius:0 8px 8px 0; }}
.sbar {{ display:flex; height:8px; border-radius:4px; overflow:hidden; gap:1px; }}
.sbar i {{ display:block; }}
/* A step the plan gave no duration takes a fixed sliver and never shrinks, so the
   timed steps beside it keep dividing the rest in proportion to their own seconds. */
.sbar i.u {{ flex:0 0 10px; opacity:.55; }}
.smetrics {{ display:flex; gap:14px; }}
/* Fixed column width so the 時長 numbers align down the page regardless of how many
   optional metrics (定位/強度) a row carries. */
.m {{ text-align:right; min-width:52px; }}
.m b {{ display:block; font-size:15px; letter-spacing:-.01em; }}
.m span {{ font-size:10.5px; color:var(--ink-4); }}
.zbar {{ display:flex; height:14px; border-radius:7px; overflow:hidden; gap:1px; margin-bottom:10px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:12px; }}
.lg {{ font-size:12px; color:var(--ink-3); display:flex; align-items:center; gap:5px; }}
.lg i {{ width:8px; height:8px; border-radius:2px; }}
.lg b {{ color:var(--ink); font-weight:600; }}
.lg em {{ font-style:normal; color:var(--ink-4); }}
.tal {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:14px; }}
.tal > div {{ background:var(--bg); border-radius:10px; padding:12px; }}
.tal b {{ font-size:20px; letter-spacing:-.02em; }}
.tal b em {{ font-style:normal; font-size:13px; color:var(--ink-4); }}
.tal span {{ display:block; font-size:11.5px; color:var(--ink-3); margin-top:2px; }}
ul.plain {{ margin:0; padding-left:18px; }}
ul.plain li {{ font-size:13px; color:var(--ink-2); padding:3px 0; }}
table.rev {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
table.rev th {{ text-align:left; font-size:11px; color:var(--ink-4); font-weight:500; padding:6px 8px 6px 0; }}
table.rev td {{ padding:7px 8px 7px 0; border-top:1px solid var(--line-soft); color:var(--ink-2); }}
table.rev td:first-child {{ color:var(--ink); font-weight:500; }}
.pending {{ color:var(--ink-4); }}
.foot {{ font-size:11.5px; color:var(--ink-4); text-align:center; margin-top:20px; line-height:1.6; }}
@media (max-width:600px) {{
  .action-grid {{ grid-template-columns:1fr; }}
  .srow {{ grid-template-columns:44px 1fr; }}
  .smetrics {{ grid-column:1/-1; justify-content:flex-start; padding-left:58px; }}
  .m {{ text-align:left; }}
  .ba {{ grid-template-columns:1fr; }}
  .outlook {{ grid-template-columns:1fr; }}
}}
</style>
<div class="wrap">
  <h1>{esc(headline)}</h1>
  <p class="sub">{esc(plan['plan_id'])} · v{plan['version']} · 週期 {cycle['start']} → {cycle['end']} ·
     <b>第 {day_n} / {total_days} 天</b> · 主攻 {esc(cycle.get('primary_adaptation'))} ·
     維持 {esc(cycle.get('maintenance_adaptation'))}</p>

  {action_focus}

  <section class="card">
    <h2>本週安排</h2>
    {rows}
    {structure_caption}
  </section>

  {change_html}

  <section class="card">
    <h2>本週的強度分配</h2>
    {render_zone_distribution(sessions)}
    <p class="sub" style="margin:12px 0 0">
      依課表意圖分類，不是實際心率區間時間。跑步共 {run_minutes} 分鐘。
    </p>
  </section>

  <section class="card">
    <h2>這週要換到什麼</h2>
    <div class="tal">
      <div><b>{tally['quality'][1]}<em> / {tally['quality'][0]}</em></b><span>品質跑（完成／計畫）</span></div>
      <div><b>{tally['easy'][1]}<em> / {tally['easy'][0]}</em></b><span>輕鬆跑</span></div>
      <div><b>{tally['strength'][1]}<em> / {tally['strength'][0]}</em></b><span>肌力課</span></div>
    </div>
    <h3>這個週期認定「有做到」的標準</h3>
    <ul class="plain">{evidence_items}</ul>
  </section>

{outlook_html}

  <section class="card">
    <h2>這個週期的目標與退場條件</h2>
    <p class="summary">{esc(goal.get('outcome', ''))}</p>
    <h3>會觸發改計畫的訊號</h3>
    <ul class="plain">{adjust_items}</ul>
    <h3>會停止自動決策、改由人判斷</h3>
    <ul class="plain">{stop_items}</ul>
  </section>

  <section class="card">
    <h2>復盤：目標 vs 實際</h2>
    <p class="sub" style="margin:-6px 0 12px">{esc(goal.get('measurement_protocol', ''))}</p>
    <table class="rev">
      <tr><th>指標</th><th>Day 0（{cycle['start']}）</th><th>Day {total_days}（{cycle['end']}）</th></tr>
      <tr><td>門檻配速</td><td>{pace_str(threshold_sec)}</td><td class="pending">待重測</td></tr>
      <tr><td>最大心率</td><td>{baseline.get('max_hr') or '—'}</td><td class="pending">待重測</td></tr>
      <tr><td>最長單次跑</td><td>{baseline.get('longest_recent_run_km') or '—'} km</td><td class="pending">待累計</td></tr>
      <tr><td>週跑量（4 週均）</td><td>{baseline.get('weekly_volume_km_4wk_avg') or '—'} km</td><td class="pending">待累計</td></tr>
      <tr><td>本週品質跑完成數</td><td>{tally['quality'][1]} / {tally['quality'][0]}</td><td class="pending">累計中</td></tr>
    </table>
  </section>

  <p class="foot">
    由 <code>scripts/render_plan_preview.py</code> 從 store 生成 ·
    資料源：PlanState v{plan['version']}<br>
    這一頁只呈現已記錄的狀態，不預測成效<br>
    「已送出」＝課表已進 Intervals，這是系統能確認的最後一步；
    Intervals→Garmin Connect 的同步與手錶下載由你自己完成
  </p>
</div>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--events", type=Path, default=None,
                        help="JSON map of intervals event id -> {name, description}; accepted "
                             "for interface stability only -- the structure bar is read from "
                             "the plan and does not use it (issue #118)")
    parser.add_argument("--today", default=None, help="ISO date; defaults to today")
    parser.add_argument("--out", type=Path, required=True, help="output HTML path (keep it outside the repo)")
    args = parser.parse_args()

    plan, event = load_store(args.state_dir)
    events = json.loads(args.events.read_text()) if args.events else {}
    today = date.fromisoformat(args.today) if args.today else datetime.now().date()

    args.out.write_text(render_page(plan, event, events, today), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
