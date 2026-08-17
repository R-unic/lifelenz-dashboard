import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from lifelenz_api import LifeLenzError, fetch_dar_data, fetch_employments, fetch_shifts

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


@app.route("/api/dar")
def api_dar():
    start_iso, end_iso = business_day_window(request.args.get("date"))
    try:
        data = fetch_dar_data(start_iso, end_iso)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


@app.route("/api/trends")
def api_trends():
    days = int(request.args.get("days", "30"))
    offset_days = int(request.args.get("offset_days", "0"))
    granularity = request.args.get("granularity", "day")
    start_iso, end_iso = period_window(days, offset_days)
    try:
        data = fetch_dar_data(start_iso, end_iso, granularity=granularity)
    except LifeLenzError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


@app.route("/api/shift-history")
def api_shift_history():
    days = int(request.args.get("days", "30"))
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
