"""Tests for subscription.py — plan-preset resolution, config load/save, week window."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from subscription import (
    DEFAULT_CONFIG, PLAN_BUDGETS, PLAN_LABELS,
    _is_valid_config,
    calc_pace_ratio,
    load_subscription_config,
    pace_color,
    resolve_budget,
    save_subscription_config,
)


class TestResolveBudget(unittest.TestCase):
    def test_known_plans_return_published_rates(self):
        self.assertEqual(resolve_budget("pro"), 25)
        self.assertEqual(resolve_budget("pro-5x"), 125)
        self.assertEqual(resolve_budget("max-5x"), 200)
        self.assertEqual(resolve_budget("max-20x"), 800)

    def test_custom_plan_uses_user_supplied_budget(self):
        self.assertEqual(resolve_budget("custom", 42), 42)
        self.assertEqual(resolve_budget("custom", "150.5"), 150.5)

    def test_custom_with_invalid_budget_returns_none(self):
        self.assertIsNone(resolve_budget("custom", None))
        self.assertIsNone(resolve_budget("custom", "not a number"))

    def test_negative_custom_clamped_to_zero(self):
        self.assertEqual(resolve_budget("custom", -10), 0)

    def test_unknown_plan_returns_none(self):
        self.assertIsNone(resolve_budget("ultra-premium"))


class TestPlanLabelsCoverage(unittest.TestCase):
    def test_every_plan_has_a_label(self):
        for k in PLAN_BUDGETS:
            self.assertIn(k, PLAN_LABELS)


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
        self.assertEqual(cfg["plan"], DEFAULT_CONFIG["plan"])

    def test_load_invalid_json_returns_default(self):
        self.path.write_text("not json {", encoding="utf-8")
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg["plan"], DEFAULT_CONFIG["plan"])

    def test_load_invalid_schema_returns_default(self):
        self.path.write_text(json.dumps({"plan": "max-5x"}), encoding="utf-8")
        cfg = load_subscription_config(path=self.path)
        # Missing required fields -> default
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_save_then_load_roundtrip(self):
        new_cfg = {
            "plan": "pro",
            "weekly_budget_api_equivalent": 25,
            "reset": {"timezone": "Europe/Oslo", "day": "Monday", "time": "00:00"},
        }
        self.assertTrue(save_subscription_config(new_cfg, path=self.path))
        cfg = load_subscription_config(path=self.path)
        self.assertEqual(cfg, new_cfg)


class TestIsValidConfig(unittest.TestCase):
    def test_default_is_valid(self):
        self.assertTrue(_is_valid_config(DEFAULT_CONFIG))

    def test_invalid_timezone(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg["reset"] = dict(DEFAULT_CONFIG["reset"], timezone="Mars/Olympus")
        self.assertFalse(_is_valid_config(cfg))

    def test_invalid_day(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg["reset"] = dict(DEFAULT_CONFIG["reset"], day="Funday")
        self.assertFalse(_is_valid_config(cfg))


class TestPaceRatio(unittest.TestCase):
    def test_zero_when_no_cost(self):
        self.assertEqual(calc_pace_ratio(0, 100, 0.5), 0.0)

    def test_zero_when_no_budget(self):
        self.assertEqual(calc_pace_ratio(50, 0, 0.5), 0.0)

    def test_just_started_clamps_to_one(self):
        # elapsed_fraction below ~1 hour → return 1.0 instead of an infinity spike
        self.assertEqual(calc_pace_ratio(50, 100, 0.0001), 1.0)

    def test_on_pace_returns_one(self):
        self.assertAlmostEqual(calc_pace_ratio(50, 100, 0.5), 1.0)

    def test_overspending_returns_above_one(self):
        # Spent 80 of 100 with only 50% of week elapsed → 80/(100*0.5) = 1.6
        self.assertAlmostEqual(calc_pace_ratio(80, 100, 0.5), 1.6)

    def test_color_buckets(self):
        self.assertEqual(pace_color(0.5), "green")
        self.assertEqual(pace_color(1.1), "green")
        self.assertEqual(pace_color(1.3), "yellow")
        self.assertEqual(pace_color(2.0), "red")


class TestUpdateSubscriptionPlan(unittest.TestCase):
    """Tests for the dashboard.update_subscription_plan write-validation surface."""

    def setUp(self):
        from dashboard import update_subscription_plan
        from subscription import SUBSCRIPTION_PATH
        self.update_plan = update_subscription_plan
        self.original_path = SUBSCRIPTION_PATH
        # Redirect SUBSCRIPTION_PATH to a tempfile so the user's real config is untouched.
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

    def test_rejects_custom_without_budget(self):
        ok, err = self.update_plan(plan="custom")
        self.assertFalse(ok)
        self.assertIn("Invalid custom budget", err)

    def test_rejects_unreasonable_custom_budget(self):
        ok, err = self.update_plan(plan="custom", custom_budget=999999)
        self.assertFalse(ok)
        self.assertIn("unreasonably large", err)

    def test_writes_known_plan(self):
        ok, err = self.update_plan(plan="pro")
        self.assertTrue(ok)
        self.assertIsNone(err)
