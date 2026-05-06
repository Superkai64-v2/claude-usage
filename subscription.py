"""
subscription.py — Monthly value tracking for subscription plans.

Tracks the question: "Has my API-equivalent usage this calendar month
exceeded what I pay for the subscription?" If yes → the subscription is
paying for itself; if no → I'd save money on the API directly.

Config lives in ~/.claude/usage-subscription.json (user-data dir, NOT
in the repo) so plan changes don't show up as commit noise.
"""

import calendar
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SUBSCRIPTION_PATH = Path.home() / ".claude" / "usage-subscription.json"

# Anthropic plan → published USD/month subscription price. Rates as of
# 2026-05-06. If Anthropic changes plan pricing, update these.
PLAN_PRICES = {
    "pro":      20,    # Claude Pro
    "pro-5x":   100,   # Claude Pro 5x (legacy / promo tier)
    "max-5x":   100,   # Claude Max 5x
    "max-20x":  200,   # Claude Max 20x
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
    "monthly_price": 200,
    "timezone": "UTC",
}

_REQUIRED_FIELDS = ("plan", "monthly_price", "timezone")


def resolve_price(plan, custom_price=None):
    """Resolve a plan name to its USD/month price. `custom_price` overrides
    when plan == 'custom'. Returns None for unknown plans / invalid custom."""
    if plan == "custom":
        try:
            return max(0, float(custom_price))
        except (TypeError, ValueError):
            return None
    return PLAN_PRICES.get(plan)


def save_subscription_config(data, path=None):
    """Write config to disk. Creates parent dir if missing. Returns True on success.

    Resolves SUBSCRIPTION_PATH at call time (not def time) so tests that
    monkey-patch the module attribute are honoured."""
    if path is None:
        path = SUBSCRIPTION_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def load_subscription_config(path=None):
    """Load and validate the user's subscription config. Returns DEFAULT_CONFIG
    when the file is missing/invalid so the dashboard always renders something.

    Migrates legacy weekly schema (plan + weekly_budget_api_equivalent + reset)
    to the monthly schema by keeping the plan name and resolving its
    monthly_price from PLAN_PRICES.

    Resolves SUBSCRIPTION_PATH at call time so tests can monkey-patch."""
    if path is None:
        path = SUBSCRIPTION_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)

    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)

    # Legacy migration: if 'weekly_budget_api_equivalent' or 'reset' are present
    # but the new fields aren't, infer the new schema from the plan name.
    if ("weekly_budget_api_equivalent" in data or "reset" in data) and "monthly_price" not in data:
        plan = data.get("plan") or DEFAULT_CONFIG["plan"]
        price = PLAN_PRICES.get(plan)
        if price is None:
            # Unknown plan in legacy data — fall back to default
            return dict(DEFAULT_CONFIG)
        # Preserve timezone if it was set, else use UTC
        tz = (data.get("reset") or {}).get("timezone") or "UTC"
        data = {"plan": plan, "monthly_price": price, "timezone": tz}

    if not _is_valid_config(data):
        return dict(DEFAULT_CONFIG)
    return data


def _is_valid_config(data):
    if not isinstance(data, dict):
        return False
    if any(data.get(k) is None for k in _REQUIRED_FIELDS):
        return False
    try:
        ZoneInfo(data["timezone"])
    except (KeyError, ValueError):
        return False
    try:
        price = float(data["monthly_price"])
        if price < 0 or price > 100000:
            return False
    except (ValueError, TypeError):
        return False
    return True


def get_month_window(now=None, tz="UTC"):
    """Return (start, end) datetimes for the current calendar month in
    the given timezone. start = first of month at 00:00, end = first of
    next month at 00:00 (exclusive)."""
    zone = ZoneInfo(tz)
    if now is None:
        now = datetime.now(zone)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=zone)

    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Roll into next month
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)
    return start, end


def calc_value_ratio(cost_used, monthly_price):
    """Return value ratio: API-equivalent cost / subscription price.
    >= 1.0 = subscription is paying for itself; < 1.0 = not yet.
    Returns 0.0 when price <= 0 (custom misconfigured)."""
    if monthly_price <= 0:
        return 0.0
    return cost_used / monthly_price


def value_color(value_ratio, elapsed_fraction):
    """Color bucket for the value gauge.

    - green:  earned out (ratio >= 1.0)
    - gray:   too early to call (month < 50% elapsed AND ratio < 1.0)
    - yellow: behind pace (ratio < 1.0, month 50-75% elapsed)
    - red:    will not earn out (ratio < 0.75 AND month > 75% elapsed)
    """
    if value_ratio >= 1.0:
        return "green"
    if elapsed_fraction < 0.5:
        return "gray"
    if elapsed_fraction > 0.75 and value_ratio < 0.75:
        return "red"
    return "yellow"
