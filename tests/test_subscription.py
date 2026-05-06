"""Tests for subscription.py — plan-price resolution, calendar-month window,
config load/save with legacy migration, value-ratio + color buckets."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from subscription import (
    DEFAULT_CONFIG, PLAN_PRICES, PLAN_LABELS,
    _is_valid_config,
    calc_value_ratio,
    get_month_window,
    load_subscription_config,
    resolve_price,
    save_subscription_config,
    value_color,
)


class TestResolvePrice(unittest.TestCase):
    def test_known_plans_return_published_rates(self):
        self.assertEqual(resolve_price("none"), 0)
        self.assertEqual(resolve_price("pro"), 20)
        self.assertEqual(resolve_price("pro-5x"), 100)
        self.assertEqual(resolve_price("max-5x"), 100)
        self.assertEqual(resolve_price("max-20x"), 200)

    def test_unknown_plan_returns_none(self):
        self.assertIsNone(resolve_price("ultra-premium"))
        self.assertIsNone(resolve_price("custom"))  # 'custom' was removed


class TestPlanLabelsCoverage(unittest.TestCase):
    def test_every_plan_has_a_label(self):
        for k in PLAN_PRICES:
            self.assertIn(k, PLAN_LABELS)


class TestGetMonthWindow(unittest.TestCase):
    def test_first_day_at_midnight(self):
        now = datetime(2026, 5, 15, 12, 30, tzinfo=ZoneInfo("UTC"))
        start, end = get_month_window(now=now, tz="UTC")
        self.assertEqual(start, datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(end,   datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo("UTC")))

    def test_december_rolls_into_january(self):
        now = datetime(2026, 12, 25, tzinfo=ZoneInfo("UTC"))
        start, end = get_month_window(now=now, tz="UTC")
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 12)
        self.assertEqual(end.year,  2027)
        self.assertEqual(end.month, 1)

    def test_february_handles_leap_year(self):
        now = datetime(2024, 2, 14, tzinfo=ZoneInfo("UTC"))
        start, end = get_month_window(now=now, tz="UTC")
        self.assertEqual(start, datetime(2024, 2, 1, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(end,   datetime(2024, 3, 1, tzinfo=ZoneInfo("UTC")))

    def test_uses_provided_timezone(self):
        # When tz=Europe/Oslo, "first of month at 00:00" should be in Oslo time.
        now = datetime(2026, 5, 15, 23, 0, tzinfo=ZoneInfo("Europe/Oslo"))
        start, end = get_month_window(now=now, tz="Europe/Oslo")
        self.assertEqual(str(start.tzinfo), "Europe/Oslo")
        self.assertEqual(start.month, 5)
        self.assertEqual(start.day, 1)


class TestSubscriptionConfigRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_load_missing_returns_default(self):
        self.path.unlink()
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_load_invalid_json_returns_default(self):
        self.path.write_text("not json {", encoding="utf-8")
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_load_invalid_schema_returns_default(self):
        self.path.write_text(json.dumps({"plan": "max-5x"}), encoding="utf-8")
        cfg = load_subscription_config(path=self.path)
        # Missing required fields -> default
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_save_then_load_roundtrip(self):
        new_cfg = {"plan": "pro", "monthly_price": 20, "timezone": "Europe/Oslo"}
        self.assertTrue(save_subscription_config(new_cfg, path=self.path))
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg, new_cfg)

    def test_legacy_weekly_schema_migrates_to_monthly(self):
        """A user with the old weekly schema (plan + weekly_budget + reset
        block) should load cleanly under the new schema, with monthly_price
        derived from the plan name."""
        legacy = {
            "plan": "max-20x",
            "weekly_budget_api_equivalent": 800,
            "reset": {"timezone": "Europe/Oslo", "day": "Monday", "time": "00:00"},
        }
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg["plan"], "max-20x")
        self.assertEqual(cfg["monthly_price"], 200)
        self.assertEqual(cfg["timezone"], "Europe/Oslo")
        # Legacy fields are dropped:
        self.assertNotIn("weekly_budget_api_equivalent", cfg)
        self.assertNotIn("reset", cfg)


class TestIsValidConfig(unittest.TestCase):
    def test_default_is_valid(self):
        self.assertTrue(_is_valid_config(DEFAULT_CONFIG))

    def test_invalid_timezone(self):
        cfg = dict(DEFAULT_CONFIG, timezone="Mars/Olympus")
        self.assertFalse(_is_valid_config(cfg))

    def test_negative_price_rejected(self):
        cfg = dict(DEFAULT_CONFIG, monthly_price=-1)
        self.assertFalse(_is_valid_config(cfg))

    def test_unreasonable_price_rejected(self):
        cfg = dict(DEFAULT_CONFIG, monthly_price=999999)
        self.assertFalse(_is_valid_config(cfg))

    def test_non_numeric_price_rejected(self):
        cfg = dict(DEFAULT_CONFIG, monthly_price="not a number")
        self.assertFalse(_is_valid_config(cfg))


class TestValueRatio(unittest.TestCase):
    def test_zero_when_no_price(self):
        self.assertEqual(calc_value_ratio(50, 0), 0.0)

    def test_zero_when_no_cost(self):
        self.assertEqual(calc_value_ratio(0, 100), 0.0)

    def test_one_when_cost_equals_price(self):
        self.assertAlmostEqual(calc_value_ratio(100, 100), 1.0)

    def test_above_one_when_earned_out(self):
        self.assertAlmostEqual(calc_value_ratio(300, 200), 1.5)

    def test_below_one_when_underspent(self):
        self.assertAlmostEqual(calc_value_ratio(50, 200), 0.25)


class TestValueColor(unittest.TestCase):
    def test_green_when_earned_out(self):
        self.assertEqual(value_color(1.0, 0.5), "green")
        self.assertEqual(value_color(2.0, 0.1), "green")

    def test_gray_early_in_month(self):
        self.assertEqual(value_color(0.3, 0.2), "gray")

    def test_yellow_mid_month_behind(self):
        self.assertEqual(value_color(0.6, 0.6), "yellow")

    def test_red_late_month_underspent(self):
        # Past 75% of month with under 75% value → won't earn out
        self.assertEqual(value_color(0.5, 0.9), "red")


class TestUpdateSubscriptionPlan(unittest.TestCase):
    """Tests for dashboard.update_subscription_plan write-validation surface."""

    def setUp(self):
        from dashboard import update_subscription_plan
        self.update_plan = update_subscription_plan
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        import subscription as _sub
        self._orig = _sub.SUBSCRIPTION_PATH
        _sub.SUBSCRIPTION_PATH = Path(self.tmp.name)

    def tearDown(self):
        import subscription as _sub
        _sub.SUBSCRIPTION_PATH = self._orig
        if Path(self.tmp.name).exists():
            os.unlink(self.tmp.name)

    def test_rejects_unknown_plan(self):
        ok, err = self.update_plan(plan="enterprise")
        self.assertFalse(ok)
        self.assertIn("Unknown plan", err)

    def test_rejects_custom_plan_now_that_it_was_removed(self):
        ok, err = self.update_plan(plan="custom")
        self.assertFalse(ok)
        self.assertIn("Unknown plan", err)

    def test_writes_known_plan(self):
        ok, err = self.update_plan(plan="pro")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_writes_none_plan(self):
        ok, err = self.update_plan(plan="none")
        self.assertTrue(ok)
        self.assertIsNone(err)
