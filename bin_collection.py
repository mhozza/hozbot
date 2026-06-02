"""Bin collection schedule lookup for St Albans.

Uses the VeoliaProxy API behind the St Albans notice board
(gis.stalbans.gov.uk/NoticeBoard9) to fetch bin collection dates
by UPRN (Unique Property Reference Number).

Usage:
    from bin_collection import get_bin_schedule, format_bin_schedule, check_schedule

    bins = get_bin_schedule("100080843318")
    print(format_bin_schedule(bins))

    print(check_schedule())  # reads BIN_UPRN from env
"""

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

load_dotenv()

logger = logging.getLogger(__name__)

BASE = "https://gis.stalbans.gov.uk/NoticeBoard9"

BIN_LABELS = {
    "Collect Domestic Refuse": "Brown bin (general waste)",
    "Collect Domestic Recycling": "Black bin (recycling)",
    "Collect Domestic Food": "Food waste caddy",
    "Collect Domestic Paid Garden": "Green bin (garden waste)",
    "Collect Communal Refuse": "Communal refuse",
    "Collect Communal Recycling": "Communal recycling",
    "Collect Communal Food": "Communal food waste",
    "Collect Recycling": "Recycling",
    "Collect Refuse": "General waste",
    "Collect Food": "Food waste",
    "Collect Paid Garden": "Garden waste",
}


def _friendly_name(task_type: str) -> str:
    return BIN_LABELS.get(task_type, task_type)


def get_bin_schedule(uprn: str) -> list[dict]:
    """Fetch bin collection services for a UPRN from the Veolia API.

    Returns a list of services, each containing ServiceHeaders with
    TaskType, Next, Last, and ScheduleDescription fields.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.get(f"{BASE}/NoticeBoard.aspx", timeout=30)

    try:
        r = session.post(
            f"{BASE}/VeoliaProxy.NoticeBoard.asmx/GetServicesByUprnAndNoticeBoard",
            json={"uprn": uprn, "noticeBoard": "default"},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException:
        logger.error("Bin collection API request failed for UPRN %s: %s", uprn, r.text[:500])
        raise
    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        logger.error("Bin collection API returned captcha/block page for UPRN %s", uprn)
        raise RuntimeError("Bin collection service is temporarily blocked (captcha challenge). Try again later.")
    try:
        data = r.json()
    except requests.JSONDecodeError:
        logger.error("Bin collection API returned invalid JSON for UPRN %s: %s", uprn, r.text[:500])
        raise
    return data.get("d", [])


def format_bin_schedule(services: list[dict]) -> str:
    """Format bin collection services into a human-readable string."""
    if not services:
        return "No bin collection data found for this address."

    now = datetime.now(timezone.utc)
    headers = []

    for svc in services:
        for h in svc.get("ServiceHeaders", []):
            try:
                next_dt = datetime.fromisoformat(h["Next"])
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                next_dt = None

            headers.append({
                "task": _friendly_name(h.get("TaskType", "?")),
                "raw_task": h.get("TaskType", "?"),
                "next": h.get("Next", "?"),
                "next_dt": next_dt,
                "last": h.get("Last", "?"),
                "schedule": h.get("ScheduleDescription", ""),
            })

    if not headers:
        return "No bin collection data found for this address."

    headers.sort(key=lambda x: x["next_dt"] if x["next_dt"] else datetime.max.replace(tzinfo=timezone.utc))

    lines = ["<b>🗑️ Bin Collection Schedule</b>\n"]

    lines.append("<b>Next collections:</b>")
    for h in headers:
        if h["next_dt"]:
            days = (h["next_dt"] - now).days
            if days < 0:
                when = "today"
            elif days == 0:
                when = "today"
            elif days == 1:
                when = "tomorrow"
            else:
                when = f"in {days} days"
            date_str = h["next_dt"].strftime("%a %d %b")
            lines.append(f"  • {h['task']}: <b>{date_str}</b> ({when})")
        else:
            lines.append(f"  • {h['task']}: {h['next']}")

    lines.append("")
    lines.append("<b>Schedule:</b>")
    for h in headers:
        lines.append(f"  • {h['task']}: {h['schedule']}")

    lines.append("")
    lines.append(
        '<a href="https://www.stalbans.gov.uk/rubbish-collections">'
        "St Albans rubbish collections</a>"
    )

    return "\n".join(lines)


def check_schedule() -> str:
    """Read BIN_UPRN from the environment and return the formatted schedule."""
    uprn = os.getenv("BIN_UPRN", "")
    if not uprn:
        return "Bin collection is not configured. Set BIN_UPRN in your .env file."
    try:
        services = get_bin_schedule(uprn)
        return format_bin_schedule(services)
    except Exception as e:
        logger.error("Error checking bin collection: %s", e, exc_info=True)
        return f"Error checking bin collection: {e}"
