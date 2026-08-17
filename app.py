import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from lifelenz_api import LifeLenzError, fetch_dar_data, fetch_employments, fetch_shifts, fetch_time_clocks

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

# The business "day" observed from LifeLenz doesn't start/end at local midnight -
# the captured sample ran 2026-08-16T08:00:00Z -> 2026-08-17T08:00:00Z for "Sun Aug 16",
# which lines up with a 4:00 AM local cutoff at UTC-4 (EDT). Both are configurable here
# since neither is guaranteed to hold for other stores/seasons (EDT vs EST).
UTC_OFFSET_HOURS = int(os.environ.get("LIFELENZ_UTC_OFFSET_HOURS", "-4"))
DAY_START_HOUR = int(os.environ.get("LIFELENZ_DAY_START_HOUR", "4"))

# Shift boundaries: mid and night start hours are fixed (11 AM / 4 PM per the store's
# actual shift structure); open/close hours are store-specific and set in .env.
MID_START_HOUR = 11
NIGHT_START_HOUR = 16


def _optional_float(key: str) -> float | None:
    v = os.environ.get(key)
    return float(v) if v not in (None, "") else None


def _name_list(key: str) -> list[str]:
    return [n.strip() for n in os.environ.get(key, "").split(",") if n.strip()]


def business_day_window(date_str: str | None) -> tuple[str, str]:
    if date_str:
        local_date = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        local_date = datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)

    start = local_date.replace(
        hour=DAY_START_HOUR, minute=0, second=0, microsecond=0
    ) - timedelta(hours=UTC_OFFSET_HOURS)
    start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return start.strftime(fmt), end.strftime(fmt)


def period_window(days: int, offset_days: int = 0) -> tuple[str, str]:
    """Window ending `offset_days` before today's business-day boundary, spanning `days` days.
    offset_days=0 is the trailing period ending now; offset_days=days gives the
    equal-length period immediately before that, for period-over-period comparison."""
    _, end_iso = business_day_window(None)
    end = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    end -= timedelta(days=offset_days)
    start = end - timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return start.strftime(fmt), end.strftime(fmt)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/trends")
def trends_page():
    return send_from_directory(app.static_folder, "trends.html")


@app.route("/ops")
def ops_page():
    return send_from_directory(app.static_folder, "ops.html")


@app.route("/api/shift-config")
def api_shift_config():
    return jsonify({
        "utcOffsetHours": UTC_OFFSET_HOURS,
        "dayStartHour": DAY_START_HOUR,
        "openHour": int(os.environ.get("LIFELENZ_STORE_OPEN_HOUR", "5")),
        "closeHour": int(os.environ.get("LIFELENZ_STORE_CLOSE_HOUR", "23")),
        "midStartHour": MID_START_HOUR,
        "nightStartHour": NIGHT_START_HOUR,
        "goals": {
            "morning": _optional_float("LIFELENZ_MORNING_GOAL_PCT"),
            "mid": _optional_float("LIFELENZ_MID_GOAL_PCT"),
            "night": _optional_float("LIFELENZ_NIGHT_GOAL_PCT"),
        },
        "managersInTraining": _name_list("LIFELENZ_MANAGERS_IN_TRAINING"),
        "generalManagers": _name_list("LIFELENZ_GENERAL_MANAGERS"),
        "manualManagers": _name_list("LIFELENZ_MANUAL_MANAGERS"),
        "skillGapExempt": _name_list("LIFELENZ_SKILL_GAP_EXEMPT"),
    })


# Auto-fills "Editing as" from whoever's actually connected, so managers on their own
# Tailscale-joined devices don't have to type their name every time. `tailscale whois`
# resolves a connecting IP to a tailnet identity - since Tailscale traffic is
# WireGuard-authenticated, the peer IP Flask sees can't be spoofed the way a typed name
# field could, so this is a stronger signal than it looks. Only works for people actually
# connecting over Tailscale (not plain LAN wifi); those users just fall back to typing
# their name like before.
_TAILSCALE_BIN_CANDIDATES = [
    os.environ.get("TAILSCALE_BIN"),
    "tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]
_tailscale_bin_cache: dict = {"checked": False, "path": None}


def _resolve_tailscale_bin() -> str | None:
    if _tailscale_bin_cache["checked"]:
        return _tailscale_bin_cache["path"]
    _tailscale_bin_cache["checked"] = True
    for candidate in _TAILSCALE_BIN_CANDIDATES:
        if not candidate:
            continue
        try:
            subprocess.run([candidate, "version"], capture_output=True, timeout=3)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        _tailscale_bin_cache["path"] = candidate
        break
    return _tailscale_bin_cache["path"]


def _tailscale_whois(ip: str) -> dict | None:
    if ip in ("127.0.0.1", "::1"):
        return None
    tailscale_bin = _resolve_tailscale_bin()
    if not tailscale_bin:
        return None
    try:
        result = subprocess.run(
            [tailscale_bin, "whois", "--json", ip], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    display_name = (data.get("UserProfile") or {}).get("DisplayName")
    return {"name": display_name} if display_name else None


@app.route("/api/whoami")
def api_whoami():
    identity = _tailscale_whois(request.remote_addr) if request.remote_addr else None
    return jsonify({"name": identity["name"] if identity else None})


@app.route("/api/dar")
def api_dar():
    start_iso, end_iso = business_day_window(request.args.get("date"))
    try:
        data = fetch_dar_data(start_iso, end_iso)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


# LifeLenz rejects any single darData query whose range is too wide, and the cap is
# tighter for finer granularity - confirmed 42 days for "day" buckets but only 32 days
# for "hour" buckets (presumably a row-count limit: 32 days * 24h ~= 42 days * 1). Clamp
# here regardless of what the client asks for, since this is the API's hard limit, not a
# preference, and 502ing on a bad value helps no one.
LIFELENZ_MAX_RANGE_DAYS = {"day": 42, "hour": 32}


@app.route("/api/trends")
def api_trends():
    granularity = request.args.get("granularity", "day")
    days = min(int(request.args.get("days", "30")), LIFELENZ_MAX_RANGE_DAYS.get(granularity, 32))
    offset_days = int(request.args.get("offset_days", "0"))
    start_iso, end_iso = period_window(days, offset_days)
    try:
        data = fetch_dar_data(start_iso, end_iso, granularity=granularity)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


@app.route("/api/shift-history")
def api_shift_history():
    days = min(int(request.args.get("days", "30")), LIFELENZ_MAX_RANGE_DAYS["hour"])
    start_iso, end_iso = period_window(days)
    try:
        data = fetch_dar_data(start_iso, end_iso, granularity="hour")
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


@app.route("/api/roster")
def api_roster():
    start_iso, end_iso = business_day_window(request.args.get("date"))
    try:
        shifts = fetch_shifts(start_iso, end_iso)
        employment_ids = sorted({s["assignedEmploymentId"] for s in shifts if s["assignedEmploymentId"]})
        employments = fetch_employments(employment_ids)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502

    employments_by_id = {e["id"]: e for e in employments}
    roster = []
    for s in shifts:
        emp = employments_by_id.get(s["assignedEmploymentId"])
        # trainedRoles is the employee's whole recorded skill/permission catalog (not this
        # shift's assignment) - LifeLenz mixes real stations (GRILL, WINDOW) with admin/
        # program tags (HIRING, SCHEDULES) in the same list, so this is a rough breadth
        # signal, not a literal reading of which physical stations someone can run.
        trained_roles = sorted({
            er["businessRole"]["businessRoleName"] for er in (emp["employmentRoles"] if emp else [])
        })
        roster.append({
            "shiftId": s["id"],
            "employmentId": s["assignedEmploymentId"],
            "name": emp["computedName"] if emp else "(unknown employee)",
            "isManager": emp["isManager"] if emp else False,
            "shiftStartTime": s["shiftStartTime"],
            "shiftEndTime": s["shiftEndTime"],
            "publishedStatus": s["publishedStatus"],
            "roles": [pr["businessRole"]["businessRoleName"] for pr in s["plannedRoles"]],
            "trainedRoles": trained_roles,
            "trainedRoleCount": len(trained_roles),
        })
    return jsonify({"shifts": roster})


@app.route("/api/time-clocks")
def api_time_clocks():
    start_iso, end_iso = business_day_window(request.args.get("date"))
    try:
        clocks = fetch_time_clocks(start_iso, end_iso)
        employment_ids = sorted({c["employmentId"] for c in clocks if c["employmentId"]})
        employments = fetch_employments(employment_ids)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502

    employments_by_id = {e["id"]: e for e in employments}
    punches = []
    for c in clocks:
        emp = employments_by_id.get(c["employmentId"])
        punches.append({
            "employmentId": c["employmentId"],
            "name": emp["computedName"] if emp else "(unknown employee)",
            "clockIn": c["clockIn"],
            "clockOut": c["clockOut"],
            "breaks": [{"start": b["startTime"], "end": b["endTime"]} for b in (c["breaks"] or [])],
        })
    return jsonify({"punches": punches})


# Private, local-only notes about your own night-shift crew - never sent to LifeLenz or
# anywhere else. Keep team_notes.json out of git (see .gitignore) since it holds
# subjective opinions about real coworkers.
TEAM_NOTES_PATH = os.path.join(os.path.dirname(__file__), "team_notes.json")


def _load_team_notes() -> dict:
    if not os.path.exists(TEAM_NOTES_PATH):
        return {}
    with open(TEAM_NOTES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_team_notes(notes: dict) -> None:
    with open(TEAM_NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)


TEAM_NOTE_FIELD_LABELS = {
    "rapport": "rapport",
    "strengths": "strengths",
    "limitations": "limitations",
    "skillNotes": "skill notes",
    "caution": "caution flag",
    "cautionNote": "caution note",
    "notes": "notes",
    "isMinor": "minor status",
}


def _diff_summary(old: dict | None, new: dict) -> str:
    if old is None:
        return "Created"
    changed = [label for field, label in TEAM_NOTE_FIELD_LABELS.items() if old.get(field) != new.get(field)]
    return "Changed: " + ", ".join(changed) if changed else "No changes"


@app.route("/api/team-notes", methods=["GET"])
def api_team_notes_list():
    return jsonify(list(_load_team_notes().values()))


@app.route("/api/team-notes", methods=["POST"])
def api_team_notes_upsert():
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    editor = (payload.get("editorName") or "").strip() or "Unknown"

    notes = _load_team_notes()
    old = notes.get(name)
    history = list(old.get("history", [])) if old else []

    record = {
        "name": name,
        "rapport": payload.get("rapport", ""),
        "strengths": payload.get("strengths", ""),
        "limitations": payload.get("limitations", ""),
        "skillNotes": payload.get("skillNotes", ""),
        "caution": bool(payload.get("caution", False)),
        "cautionNote": payload.get("cautionNote", ""),
        "notes": payload.get("notes", ""),
        "isMinor": bool(payload.get("isMinor", False)),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedBy": editor,
    }
    history.append({"author": editor, "timestamp": record["updatedAt"], "summary": _diff_summary(old, record)})
    record["history"] = history

    notes[name] = record
    _save_team_notes(notes)
    return jsonify(notes[name])


@app.route("/api/team-notes/<name>", methods=["DELETE"])
def api_team_notes_delete(name):
    notes = _load_team_notes()
    notes.pop(name, None)
    _save_team_notes(notes)
    return jsonify({"ok": True})


# Day-by-day staffing log: who got called in/covered/cut/took a break, and when, during
# your own shift. This doesn't recompute LifeLenz's labor numbers (its actual_punch_hours
# is the real source of truth once it settles) - it's a record of *why* a day's numbers
# look the way they do, kept for explaining anomalies later, for informing the
# cut-suggestion reasoning, and for reconstructing who was actually on the floor at a given
# moment (see computeEffectiveStaffing in ops.html).
SHIFT_EVENTS_PATH = os.path.join(os.path.dirname(__file__), "shift_events.json")
# "called_in" used to be called "added" and "covered" used to be called "subbed" - old
# events with those values are still valid to read, just not offered as a choice for new
# ones (the frontend maps both to the same display label).
EVENT_TYPES = {"called_in", "covered", "cut", "break_start", "break_end"}


def _load_shift_events() -> dict:
    if not os.path.exists(SHIFT_EVENTS_PATH):
        return {}
    with open(SHIFT_EVENTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_shift_events(events: dict) -> None:
    with open(SHIFT_EVENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)


@app.route("/api/shift-events")
def api_shift_events_list():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    events = _load_shift_events()
    day_events = sorted(events.get(date_str, []), key=lambda e: e["time"])
    return jsonify({"events": day_events})


@app.route("/api/shift-events", methods=["POST"])
def api_shift_events_create():
    payload = request.get_json(force=True)
    date_str = (payload.get("date") or "").strip()
    employee_name = (payload.get("employeeName") or "").strip()
    event_type = (payload.get("eventType") or "").strip()
    if not date_str or not employee_name or event_type not in EVENT_TYPES:
        return jsonify({"error": "date, employeeName, and a valid eventType are required"}), 400

    # "Who got covered" is what makes a covered event useful later (for payroll questions,
    # and for reconstructing effective staffing) - without it it's indistinguishable from a
    # called_in. untilTime is optional and only meaningful for a *partial* cover (someone
    # stepping in for part of a shift, not a full handoff) - see computeEffectiveStaffing.
    covered_for = (payload.get("coveredFor") or "").strip()
    if event_type == "covered" and not covered_for:
        return jsonify({"error": "coveredFor is required for a covered event"}), 400

    events = _load_shift_events()
    day_events = events.setdefault(date_str, [])
    entry = {
        "id": str(uuid.uuid4()),
        "employeeName": employee_name,
        "eventType": event_type,
        "time": payload.get("time") or datetime.now(timezone.utc).isoformat(),
        "note": payload.get("note", ""),
        "coveredFor": covered_for or None,
        "untilTime": (payload.get("untilTime") or None) if event_type == "covered" else None,
        "author": (payload.get("editorName") or "").strip() or "Unknown",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    day_events.append(entry)
    _save_shift_events(events)
    return jsonify(entry)


@app.route("/api/shift-events/<event_id>", methods=["DELETE"])
def api_shift_events_delete(event_id):
    events = _load_shift_events()
    for date_str in events:
        events[date_str] = [e for e in events[date_str] if e["id"] != event_id]
    _save_shift_events(events)
    return jsonify({"ok": True})


if __name__ == "__main__":
    if os.environ.get("LIFELENZ_DEV") == "1":
        # Flask's dev server + reloader - handy while actively editing, but the reloader
        # runs a background file-watcher thread that has no reason to exist for
        # leave-it-running use.
        app.run(port=5151, debug=True)
    else:
        # waitress: a small fixed thread pool that blocks waiting for requests rather than
        # polling - idle CPU is ~0% and memory footprint is tens of MB, safe to leave running
        # indefinitely alongside anything else (games included). Binds 0.0.0.0 so it's
        # reachable over Tailscale (and the LAN) at this machine's Tailscale IP, not just
        # localhost.
        from waitress import serve

        print("Serving on http://0.0.0.0:5151 (reachable via localhost, LAN, and Tailscale)")
        serve(app, host="0.0.0.0", port=5151, threads=4)
