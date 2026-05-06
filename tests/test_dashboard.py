"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scanner import get_db, init_db, upsert_sessions, insert_turns
from dashboard import get_dashboard_data, get_session_detail, DashboardHandler, HTML_TEMPLATE

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_truncated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(len(session["session_id"]), 8)

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        self.assertTrue(all(r["day"] == "2026-04-08" for r in rows))

    def test_session_name_field_present(self):
        """sessions_all entries must always include a session_name key (empty by default)."""
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertIn("session_name", session)
        self.assertEqual(session["session_name"], "")

    def test_skill_calls_by_day_present(self):
        """skill_calls_by_day must be in the API response, list type."""
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("skill_calls_by_day", data)
        self.assertIsInstance(data["skill_calls_by_day"], list)
        # Fixture turns have skill_name=None (default) → no rows expected.
        self.assertEqual(data["skill_calls_by_day"], [])

    def test_themes_injected_into_html(self):
        """render_html() must inject all BUNDLED_THEMES so the JS dropdown
        gets populated. Asserting on count + a known theme id covers both
        the data shape and the placeholder substitution."""
        from dashboard import render_html, BUNDLED_THEMES
        html = render_html().decode("utf-8")
        self.assertNotIn("/*__THEMES_JSON__*/", html)  # placeholder substituted
        self.assertEqual(len(BUNDLED_THEMES), 6)
        for theme_id in ("default", "apple", "linear", "vercel", "notion", "stripe"):
            self.assertIn(f'"id": "{theme_id}"', html)

    def test_sortProjectBranch_sorts_globally_not_grouped_by_project(self):
        """Regression: sortProjectBranch used to sort by project name FIRST,
        then by the chosen column — so 'Est. Cost descending' grouped rows
        per project alphabetically and looked completely wrong (a $1230
        MSP/develop row appeared BELOW a $57 MARKETING/main row because
        'GAIN-MARKETING' < 'GAIN-MSP'). The fix: sort by chosen column,
        project as tiebreaker only."""
        body = HTML_TEMPLATE
        # Find function body and assert it does NOT lead with the project comparison.
        import re
        m = re.search(r"function sortProjectBranch\(rows\)\s*\{(.*?)\n\}", body, re.DOTALL)
        self.assertIsNotNone(m)
        fn = m.group(1)
        # The first compare must be on branchSortCol (the chosen column).
        first_av = fn.index("a[branchSortCol]")
        first_pa = fn.index("a.project")
        self.assertLess(
            first_av, first_pa,
            "branchSortCol comparison must come BEFORE project tiebreaker; "
            "otherwise project-grouping dominates and global sort breaks."
        )

    def test_theme_override_style_comes_AFTER_main_style(self):
        """Regression: <style id='theme-override'> MUST come after the main
        <style> block. Otherwise the default :root in the main style wins
        the cascade and theme switches do nothing — exactly the bug the
        user hit on 2026-05-06."""
        # Use the rendered HTML (post-injection) so we test what the browser sees.
        from dashboard import render_html
        html = render_html().decode("utf-8")
        # Find positions of: opening of main <style>, its closing </style>,
        # and the <style id="theme-override">.
        main_open = html.index("<style>\n")
        main_close = html.index("</style>", main_open)
        override_pos = html.index('<style id="theme-override">')
        self.assertGreater(
            override_pos, main_close,
            "theme-override must appear after the main </style>; otherwise "
            ":root overrides cascade WRONG and themes don't apply."
        )


class TestSessionNameInDashboard(unittest.TestCase):
    """Verify session_name from the sessions table surfaces in dashboard output."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        conn.execute("""
            INSERT INTO sessions
                (session_id, project_name, first_timestamp, last_timestamp,
                 git_branch, total_input_tokens, total_output_tokens,
                 total_cache_read, total_cache_creation, model, turn_count,
                 session_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "named-session-xyz", "user/proj",
            "2026-04-08T09:00:00Z", "2026-04-08T10:00:00Z",
            "main", 100, 50, 0, 0, "claude-sonnet-4-6", 1, "clip-research",
        ))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_session_name_returned_in_api(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(data["sessions_all"][0]["session_name"], "clip-research")

    def test_session_id_still_truncated_alongside_name(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(len(session["session_id"]), 8)
        self.assertEqual(session["session_id"], "named-se")


class TestSessionDetail(unittest.TestCase):
    """get_session_detail must return turn history, tool usage, and branch info."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 500, "total_output_tokens": 200,
            "total_cache_read": 50, "total_cache_creation": 20,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 500,
            "output_tokens": 200, "cache_read_tokens": 50,
            "cache_creation_tokens": 20, "tool_name": "reply", "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_session_detail_includes_tools_and_cwds(self):
        from dashboard import get_session_detail
        detail = get_session_detail("sess-abc123", db_path=self.db_path)
        self.assertEqual(detail["project"], "user/myproject")
        self.assertEqual(detail["branch"], "main")
        self.assertEqual(detail["tool_usage"][0]["tool_name"], "reply")
        self.assertEqual(detail["cwd_usage"][0]["cwd"], "/tmp")
        self.assertEqual(len(detail["turn_history"]), 1)

    def test_skill_calls_by_day_excludes_non_skill_tools(self):
        """The fixture turn has tool_name='reply' (not a Skill invocation),
        so skill_calls_by_day stays empty even though tool_name is set."""
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(data["skill_calls_by_day"], [])

    def test_session_skill_breakdown_attached_to_sessions_all(self):
        """Each sessions_all entry should carry a skill_breakdown list of
        top-5 skills by token count, used by the Sessions-table 'Top Skills'
        column and the Cost-by-Project aggregation. Empty for the fixture
        because skill_name=None on all turns (no Skill tool invoked)."""
        data = get_dashboard_data(db_path=self.db_path)
        s = data["sessions_all"][0]
        self.assertIn("skill_breakdown", s)
        self.assertEqual(s["skill_breakdown"], [])

    def test_session_detail_token_values(self):
        detail = get_session_detail("sess-abc123", db_path=self.db_path)
        turn = detail["turn_history"][0]
        self.assertEqual(turn["input"], 500)
        self.assertEqual(turn["output"], 200)
        self.assertEqual(turn["cache_read"], 50)
        self.assertEqual(turn["cache_creation"], 20)
        self.assertEqual(turn["total"], 770)
        tool = detail["tool_usage"][0]
        self.assertEqual(tool["tokens"], 770)
        self.assertEqual(tool["turns"], 1)

    def test_session_detail_missing_db(self):
        data = get_session_detail("any-id", db_path=Path("/nonexistent/usage.db"))
        self.assertIn("error", data)
        self.assertIn("Database not found", data["error"])

    def test_session_detail_not_found(self):
        detail = get_session_detail("nonexistent-session", db_path=self.db_path)
        self.assertIn("error", detail)
        self.assertEqual(detail["error"], "Session not found")

    def test_session_detail_empty_session_id(self):
        detail = get_session_detail("", db_path=self.db_path)
        self.assertIn("error", detail)
        self.assertEqual(detail["error"], "Session not found")

    def test_session_detail_zero_turns(self):
        conn = get_db(self.db_path)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "proj",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:00:00Z",
            "git_branch": None, "model": "claude-sonnet-4-6",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 0,
        }])
        conn.commit()
        conn.close()
        detail = get_session_detail("sess-empty", db_path=self.db_path)
        self.assertEqual(detail["tool_usage"], [])
        self.assertEqual(detail["cwd_usage"], [])
        self.assertEqual(detail["turn_history"], [])

    def test_sessions_include_new_fields(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id_full"], "sess-abc123")
        self.assertEqual(session["branch"], "main")
        self.assertIn("2026-04-08", session["first"])


class TestSkillBreakdownTopFive(unittest.TestCase):
    """skill_breakdown is trimmed to top 5 skills per session by tokens."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-multi", "project_name": "p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 7,
        }])
        # 7 different skills, each with distinct token counts so ordering is unambiguous
        turns = []
        for i, (skill, tokens) in enumerate([
            ("simplify", 1000), ("browser-test-playwright", 800),
            ("massive-parallel-planning", 600), ("review", 400),
            ("security-review", 200), ("loop", 50), ("schedule", 25),
        ]):
            turns.append({
                "session_id": "sess-multi",
                "timestamp": f"2026-04-08T09:{i:02d}:00Z",
                "model": "claude-sonnet-4-6",
                "input_tokens": tokens, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "tool_name": "Skill", "skill_name": skill, "cwd": "/tmp",
            })
        insert_turns(conn, turns)
        conn.commit(); conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_breakdown_capped_at_5_and_sorted_descending(self):
        data = get_dashboard_data(db_path=self.db_path)
        s = data["sessions_all"][0]
        bd = s["skill_breakdown"]
        self.assertEqual(len(bd), 5)
        self.assertEqual(
            [t["skill"] for t in bd],
            ["simplify", "browser-test-playwright", "massive-parallel-planning", "review", "security-review"],
        )
        # loop (50) and schedule (25) are correctly excluded.


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # doesn't unlink the user's real ~/.claude/usage.db or scan their
        # real transcript directory during tests.
        import dashboard as _d
        import scanner as _s
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"Claude Code Usage Dashboard", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?_=cachebust"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_session_unknown_id_returns_404(self):
        url = f"http://127.0.0.1:{self.port}/api/session?session_id=nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            data = json.loads(e.read())
            self.assertIn("error", data)
            e.close()

    def test_api_session_missing_param_returns_404(self):
        url = f"http://127.0.0.1:{self.port}/api/session"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            e.close()

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_renders_session_name_when_set(self):
        """The sessions-table renderer must branch on session_name presence."""
        self.assertIn("s.session_name", HTML_TEMPLATE)
        self.assertIn("session-name", HTML_TEMPLATE)

    def test_csv_export_includes_session_name(self):
        self.assertIn("Session Name", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn("chart.js", HTML_TEMPLATE.lower())

    def test_template_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models."""
        self.assertIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', HTML_TEMPLATE)
        self.assertIn('data-tz="local"', HTML_TEMPLATE)
        self.assertIn('data-tz="utc"', HTML_TEMPLATE)

    def test_hourly_peak_hour_constants(self):
        """Peak-hour set covers UTC 12–17 (Mon–Fri 05:00–11:00 PT)."""
        self.assertIn('PEAK_HOURS_UTC', HTML_TEMPLATE)
        self.assertIn('[12, 13, 14, 15, 16, 17]', HTML_TEMPLATE)

    def test_readURLModels_falls_back_to_all_when_billable_empty(self):
        """Regression for #76: when no model name matches opus/sonnet/haiku
        (empty-string, 'unknown', legacy IDs, third-party proxies), the
        default selection must fall back to all models so the dashboard
        does not silently render blank."""
        self.assertIn("billable.length > 0 ? billable : allModels", HTML_TEMPLATE)

    def test_loadData_surfaces_errors_via_banner(self):
        """Regression for the diagnosis-difficulty pattern behind #74/76/88
        /90/92/93/99/106: exceptions in loadData/applyFilter must be visible
        in the DOM (an error-banner element), not console-only."""
        self.assertIn("error-banner", HTML_TEMPLATE)
        self.assertIn("showErrorBanner", HTML_TEMPLATE)

    def test_getRangeBounds_uses_utc_throughout(self):
        """Regression: getRangeBounds must use UTC arithmetic (getUTC*,
        Date.UTC) so range boundaries align with the UTC dates the SQL
        query emits. Using local-TZ getters (getDate, getMonth) inside the
        function would shift month/week boundaries ±1 day for non-UTC users.

        Hard regression guard: the bare local getters MUST NOT appear in
        getRangeBounds — pull the function body out of the template and
        substring-check it."""
        # Grab just the getRangeBounds body. Anchor the end on the next
        # `function ` keyword (or `// ──` divider) rather than `^\}`, which
        # would match the first column-0 closing brace and break under any
        # reformatter that re-indents the inner blocks.
        import re
        m = re.search(
            r"function getRangeBounds\(range\)\s*\{(.*?)\n\}\s*\n+function ",
            HTML_TEMPLATE,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "getRangeBounds function not found in template")
        body = m.group(1)
        # Must use UTC accessors:
        self.assertIn("getUTCDate", body)
        self.assertIn("getUTCMonth", body)
        self.assertIn("Date.UTC", body)
        # Must NOT use local-TZ accessors (the bug):
        self.assertNotIn("today.getDate()", body)
        self.assertNotIn("today.getMonth()", body)
        self.assertNotIn("today.getFullYear()", body)
        self.assertNotIn("today.getDay()", body)


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Decode the JSON-injected PRICING table from the rendered HTML.
        The HTML_TEMPLATE now carries a placeholder; the real table is
        injected at request time by render_html()."""
        import re, json
        from dashboard import render_html
        html = render_html().decode("utf-8")
        m = re.search(r"const PRICING = (\{.*?\});", html, re.DOTALL)
        if not m:
            return {}
        return json.loads(m.group(1))

    def test_all_cli_models_in_dashboard(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertIn(model, js_prices, f"{model} missing from dashboard JS")

    def test_prices_match(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertAlmostEqual(
                CLI_PRICING[model]["input"], js_prices[model]["input"],
                msg=f"{model} input price mismatch"
            )
            self.assertAlmostEqual(
                CLI_PRICING[model]["output"], js_prices[model]["output"],
                msg=f"{model} output price mismatch"
            )


if __name__ == "__main__":
    unittest.main()
