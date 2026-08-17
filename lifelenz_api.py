import os

import requests

GRAPHQL_URL = "https://us01-connect.lifelenz.com/manager/graphql"

DAR_QUERY = """
query GetDARData($businessId: ID!, $scheduleId: ID!, $startDateTime: ISO8601DateTime!, $endDateTime: ISO8601DateTime!, $granularity: DarGranularityEnum) {
  darData(
    businessId: $businessId
    scheduleId: $scheduleId
    startDateTime: $startDateTime
    endDateTime: $endDateTime
    granularity: $granularity
  ) {
    dar {
      data { fieldCode value __typename }
      missingActuals
      startTime
      __typename
    }
    totals { fieldCode value __typename }
    __typename
  }
}
"""

SHIFTS_QUERY = """
query GetShifts($businessId: ID!, $scheduleId: ID!, $start: ISO8601DateTime!, $end: ISO8601DateTime!) {
  shifts(businessId: $businessId, scheduleId: $scheduleId, startDateTime: $start, endDateTime: $end, first: 200) {
    totalCount
    nodes {
      id
      shiftStartTime
      shiftEndTime
      assignedEmploymentId
      isShiftRunner
      publishedStatus
      shiftType
      plannedRoles {
        businessRole { businessRoleName }
      }
    }
  }
}
"""

EMPLOYMENTS_QUERY = """
query GetEmployments($businessId: ID!, $scheduleId: ID!, $ids: [ID!]) {
  employments(businessId: $businessId, scheduleId: $scheduleId, ids: $ids, first: 200) {
    nodes {
      id
      computedName
      isManager
      employmentRoles {
        businessRole { businessRoleName }
      }
    }
  }
}
"""

# Real punch data (found via GraphQL introspection - not in LifeLenz's public docs, so this
# could break if they change the schema). This is what the mobile app's "who's on the clock"
# screen is actually backed by: real clockIn/clockOut timestamps and real break windows, not
# the published schedule. clockOut is null while someone is still clocked in -
# includeInProgressPivots is what makes that in-progress punch show up at all instead of
# being withheld until it closes out.
TIME_CLOCKS_QUERY = """
query GetTimeClocks($businessId: ID!, $scheduleId: ID!, $startDateTime: ISO8601DateTime!, $endDateTime: ISO8601DateTime!) {
  timeClocks(
    businessId: $businessId
    scheduleId: $scheduleId
    startDateTime: $startDateTime
    endDateTime: $endDateTime
    includeInProgressPivots: true
    excludeDeleted: true
    first: 200
  ) {
    nodes {
      id
      employmentId
      clockIn
      clockOut
      breaks {
        startTime
        endTime
      }
    }
  }
}
"""


class LifeLenzError(Exception):
    pass


def _headers() -> dict:
    token = os.environ.get("LIFELENZ_AUTH_TOKEN")
    if not token:
        raise LifeLenzError("LIFELENZ_AUTH_TOKEN is not set in .env")
    return {
        "Content-Type": "application/json",
        "x-auth-token": token,
        "x-lifelenz-device": "webadmin",
        "x-version": os.environ.get("LIFELENZ_VERSION", "1.76.20"),
        "Origin": "https://admin.lifelenz.com",
        "Referer": "https://admin.lifelenz.com/",
    }


def _post(operation_name: str, query: str, variables: dict, timeout: int = 40) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        json={"operationName": operation_name, "variables": variables, "query": query},
        headers=_headers(),
        timeout=timeout,
    )

    if resp.status_code in (401, 403):
        raise LifeLenzError(
            "LifeLenz rejected the request (401/403) - the x-auth-token in .env has "
            "likely expired. Grab a fresh one from DevTools and update .env."
        )
    resp.raise_for_status()

    body = resp.json()
    if "errors" in body:
        raise LifeLenzError(f"LifeLenz GraphQL error: {body['errors']}")
    return body["data"]


def fetch_dar_data(start_iso: str, end_iso: str, granularity: str = "hour") -> dict:
    business_id = os.environ.get("LIFELENZ_BUSINESS_ID")
    schedule_id = os.environ.get("LIFELENZ_SCHEDULE_ID")
    if not business_id or not schedule_id:
        raise LifeLenzError("LIFELENZ_BUSINESS_ID / LIFELENZ_SCHEDULE_ID are not set in .env")

    data = _post(
        "GetDARData",
        DAR_QUERY,
        {
            "businessId": business_id,
            "scheduleId": schedule_id,
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "granularity": granularity,
        },
        # Wide-range hourly queries (shift history over weeks/months) take LifeLenz
        # noticeably longer to compute than a single day - 15s occasionally wasn't enough.
        timeout=40,
    )
    return data["darData"]


def fetch_shifts(start_iso: str, end_iso: str) -> list[dict]:
    business_id = os.environ.get("LIFELENZ_BUSINESS_ID")
    schedule_id = os.environ.get("LIFELENZ_SCHEDULE_ID")
    if not business_id or not schedule_id:
        raise LifeLenzError("LIFELENZ_BUSINESS_ID / LIFELENZ_SCHEDULE_ID are not set in .env")

    data = _post(
        "GetShifts",
        SHIFTS_QUERY,
        {"businessId": business_id, "scheduleId": schedule_id, "start": start_iso, "end": end_iso},
        timeout=20,
    )
    return data["shifts"]["nodes"]


def fetch_employments(employment_ids: list[str]) -> list[dict]:
    business_id = os.environ.get("LIFELENZ_BUSINESS_ID")
    schedule_id = os.environ.get("LIFELENZ_SCHEDULE_ID")
    if not employment_ids:
        return []

    data = _post(
        "GetEmployments",
        EMPLOYMENTS_QUERY,
        {"businessId": business_id, "scheduleId": schedule_id, "ids": employment_ids},
        timeout=20,
    )
    return data["employments"]["nodes"]


def fetch_time_clocks(start_iso: str, end_iso: str) -> list[dict]:
    business_id = os.environ.get("LIFELENZ_BUSINESS_ID")
    schedule_id = os.environ.get("LIFELENZ_SCHEDULE_ID")
    if not business_id or not schedule_id:
        raise LifeLenzError("LIFELENZ_BUSINESS_ID / LIFELENZ_SCHEDULE_ID are not set in .env")

    data = _post(
        "GetTimeClocks",
        TIME_CLOCKS_QUERY,
        {"businessId": business_id, "scheduleId": schedule_id, "startDateTime": start_iso, "endDateTime": end_iso},
        timeout=20,
    )
    return data["timeClocks"]["nodes"]
