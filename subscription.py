"""
subscription.py — Weekly budget tracking for subscription plans.

Computes weekly billing windows, plan-preset → API-equivalent budget,
and pace ratios for the dashboard gauge.

Config lives in ~/.claude/usage-subscription.json (user-data dir, NOT
in the repo) so plan changes don't show up as commit noise.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SUBSCRIPTION_PATH = Path.home() / ".claude" / "usage-subscription.json"

# Anthropic plan → API-equivalent USD/week. Rates as of 2026-05-06; if
# Anthropic changes plan caps these need updating. Numbers are derived
# from public plan pages: monthly limits / 4.3 weeks rounded sensibly.
PLAN_BUDGETS = {
    "pro":      25,    # Claude Pro
    "pro-5x":   125,   # Claude Pro 5x (legacy / promo tier)
    "max-5x":   200,   # Claude Max 5x
    "max-20x":  800,   # Claude Max 20x
    "custom":   None,  # user-supplied number
}

PLAN_LABELS = {
    "pro":      "Pro",
    "pro-5x":   "Pro 5×",
    "max-5x":   "Max 5×",
    "max-20x":  "Max 20×",
    "custom":   "Custom",
}

DEFAULT_CONFIG = {
    "plan": "max-20x",
    "weekly_budget_api_equivalent": 800,
    "reset": {"timezone": "UTC", "day": "Monday", "time": "00:00"},
}

_REQUIRED_FIELDS = ("plan", "weekly_budget_api_equivalent", "reset")
_REQUIRED_RESET_FIELDS = ("timezone", "day", "time")
_VALID_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def resolve_budget(plan, custom_budget=None):
    """Resolve a plan name to its weekly USD/week budget. `custom_budget` overrides
    when plan == 'custom'. Returns None for unknown plans."""
    if plan == "custom":
        try:
            return max(0, float(custom_budget))
        except (TypeError, ValueError):
            return None
    return PLAN_BUDGETS.get(plan)


def save_subscription_config(data, path=SUBSCRIPTION_PATH):
    """Write config to disk. Creates parent dir if missing. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def load_subscription_config(path=SUBSCRIPTION_PATH):
    """Load and validate the user's subscription config. Returns DEFAULT_CONFIG
    when the file is missing/invalid so the dashboard always renders something."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)

    if not _is_valid_config(data):
        return dict(DEFAULT_CONFIG)
    return data


def _is_valid_config(data):
    if not isinstance(data, dict):
        return False
    if any(data.get(k) is None for k in _REQUIRED_FIELDS):
        return False
    reset = data.get("reset")
    if not isinstance(reset, dict):
        return False
    if any(reset.get(k) is None for k in _REQUIRED_RESET_FIELDS):
        return False
    if reset["day"] not in _VALID_DAYS:
        return False
    try:
        ZoneInfo(reset["timezone"])
    except (KeyError, ValueError):
        return False
    return True


def get_week_window(reset_cfg, now=None):
    """Return (start, end) datetimes for the current billing week.

    The week starts on reset_cfg['day'] at reset_cfg['time'] in the
    configured timezone. If `now` falls exactly on the reset moment,
    it's considered the start of a new week.
    """
    tz = ZoneInfo(reset_cfg["timezone"])
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    hour, minute = map(int, reset_cfg["time"].split(":"))
    target_weekday = _VALID_DAYS.index(reset_cfg["day"])  # Monday=0

    # Find the most recent reset_day at reset_time that is <= now
    # Start from today's date at the reset time
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Walk back to the correct weekday
    days_since = (candidate.weekday() - target_weekday) % 7
    candidate = candidate - timedelta(days=days_since)

    # If candidate is in the future (we haven't reached reset time today
    # and today is the reset day), go back one full week
    if candidate > now:
        candidate = candidate - timedelta(days=7)

    start = candidate
    end = start + timedelta(days=7)
    return start, end


# Elapsed fraction below this threshold (~1 hour of 168-hour week)
# triggers "just started" clamping to avoid infinity/spike in pace ratio.
_JUST_STARTED_THRESHOLD = 1.0 / 168.0  # ~0.00595


def calc_pace_ratio(cost_used, weekly_budget, elapsed_fraction):
    """Return pace ratio: actual_cost / expected_cost at this point in the week.

    Returns 1.0 when elapsed_fraction < ~1 hour (just-started clamp).
    Returns 0.0 when cost_used or weekly_budget is 0.
    """
    if weekly_budget <= 0 or cost_used <= 0:
        return 0.0
    if elapsed_fraction < _JUST_STARTED_THRESHOLD:
        return 1.0
    expected = weekly_budget * elapsed_fraction
    return cost_used / expected


def pace_color(ratio):
    """Return color bucket based on pace ratio."""
    if ratio < 1.2:
        return "green"
    if ratio < 1.5:
        return "yellow"
    return "red"
