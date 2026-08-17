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
        roster.append({
            "shiftId": s["id"],
            "employmentId": s["assignedEmploymentId"],
            "name": emp["computedName"] if emp else "(unknown employee)",
            "isManager": emp["isManager"] if emp else False,
            "shiftStartTime": s["shiftStartTime"],
            "shiftEndTime": s["shiftEndTime"],
            "publishedStatus": s["publishedStatus"],
            "roles": [pr["businessRole"]["businessRoleName"] for pr in s["plannedRoles"]],
        })
    return jsonify({"shifts": roster})


if __name__ == "__main__":
    app.run(port=5151, debug=True)
