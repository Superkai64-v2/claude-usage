"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path

from pricing import PRICING, calc_cost
from subscription import (
    DEFAULT_CONFIG, PLAN_BUDGETS, PLAN_LABELS,
    calc_pace_ratio, get_week_window, load_subscription_config, pace_color,
    resolve_budget, save_subscription_config, _is_valid_config,
)
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlparse

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(model, 'unknown')                as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, model
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── Tool-call usage per day per model (client filters by range + model) ───
    # Cowork sessions don't carry tool_name (cowork.py stores None) so they're
    # silently excluded — document this in the table caption.
    tool_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            tool_name                  as tool,
            COUNT(*)                   as turns,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output
        FROM turns
        WHERE tool_name IS NOT NULL AND tool_name != ''
        GROUP BY day, model, tool
        ORDER BY day, model, tool
    """).fetchall()

    tool_calls_by_day = [{
        "day":    r["day"],
        "model":  r["model"],
        "tool":   r["tool"],
        "turns":  r["turns"] or 0,
        "input":  r["input"] or 0,
        "output": r["output"] or 0,
    } for r in tool_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    # session_name may be missing on older DB schemas; fall back gracefully.
    try:
        session_rows = conn.execute("""
            SELECT
                session_id, project_name, first_timestamp, last_timestamp,
                total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count,
                git_branch, session_name
            FROM sessions
            ORDER BY last_timestamp DESC
        """).fetchall()
    except sqlite3.OperationalError:
        # Pre-migration DB: synthesise session_name=None
        session_rows = conn.execute("""
            SELECT
                session_id, project_name, first_timestamp, last_timestamp,
                total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count,
                git_branch, NULL AS session_name
            FROM sessions
            ORDER BY last_timestamp DESC
        """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "session_id_full": r["session_id"],
            "session_name":  r["session_name"] or "",
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "first":         (r["first_timestamp"] or "")[:16].replace("T", " "),
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    return {
        "all_models":        all_models,
        "daily_by_model":    daily_by_model,
        "hourly_by_model":   hourly_by_model,
        "tool_calls_by_day": tool_calls_by_day,
        "sessions_all":      sessions_all,
        "generated_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_session_detail(session_id, db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp, git_branch,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
        FROM sessions
        WHERE session_id = ?
    """, (session_id,)).fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    turn_rows = conn.execute("""
        SELECT
            timestamp, model, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, tool_name, cwd
        FROM turns
        WHERE session_id = ?
        ORDER BY timestamp ASC, id ASC
    """, (session_id,)).fetchall()

    turns = []
    tool_usage = {}
    cwd_counts = {}

    for r in turn_rows:
        tool_name = r["tool_name"] or "reply"
        cwd = r["cwd"] or "unknown"
        total_tokens = (
            (r["input_tokens"] or 0) +
            (r["output_tokens"] or 0) +
            (r["cache_read_tokens"] or 0) +
            (r["cache_creation_tokens"] or 0)
        )
        turns.append({
            "timestamp":      r["timestamp"] or "",
            "timestamp_short": (r["timestamp"] or "")[:16].replace("T", " "),
            "model":          r["model"] or "unknown",
            "tool_name":      tool_name,
            "cwd":            cwd,
            "input":          r["input_tokens"] or 0,
            "output":         r["output_tokens"] or 0,
            "cache_read":     r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "total":          total_tokens,
        })

        stats = tool_usage.setdefault(tool_name, {"tool_name": tool_name, "turns": 0, "tokens": 0})
        stats["turns"] += 1
        stats["tokens"] += total_tokens
        cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1

    conn.close()

    try:
        t1 = datetime.fromisoformat((session["first_timestamp"] or "").replace("Z", "+00:00"))
        t2 = datetime.fromisoformat((session["last_timestamp"] or "").replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    return {
        "session_id":       session["session_id"],
        "project":          session["project_name"] or "unknown",
        "branch":           session["git_branch"] or "",
        "first":            (session["first_timestamp"] or "")[:19].replace("T", " "),
        "last":             (session["last_timestamp"] or "")[:19].replace("T", " "),
        "duration_min":     duration_min,
        "model":            session["model"] or "unknown",
        "turns":            session["turn_count"] or 0,
        "input":            session["total_input_tokens"] or 0,
        "output":           session["total_output_tokens"] or 0,
        "cache_read":       session["total_cache_read"] or 0,
        "cache_creation":   session["total_cache_creation"] or 0,
        "tool_usage":       sorted(tool_usage.values(), key=lambda item: (-item["tokens"], item["tool_name"])),
        "cwd_usage":        sorted(
            [{"cwd": c, "turns": n} for c, n in cwd_counts.items()],
            key=lambda item: (-item["turns"], item["cwd"])
        ),
        "turn_history":     turns,
    }


def get_subscription_data(db_path=DB_PATH):
    """Return current weekly budget state for the gauge:
    {plan, plan_label, weekly_budget, cost_used, pace_ratio, color,
     elapsed_fraction, week_start_iso, week_end_iso, reset}.
    Falls back to DEFAULT_CONFIG if no user config exists."""
    cfg = load_subscription_config()
    plan = cfg.get("plan", "max-20x")
    weekly_budget = cfg.get("weekly_budget_api_equivalent", 0) or 0

    week_start, week_end = get_week_window(cfg["reset"])
    now = datetime.now(week_start.tzinfo)
    elapsed_fraction = (now - week_start).total_seconds() / max(
        1, (week_end - week_start).total_seconds()
    )

    cost_used = 0.0
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
            FROM turns
            WHERE timestamp >= ? AND timestamp < ?
        """, (week_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
              week_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
        for r in rows:
            cost_used += calc_cost(
                r["model"],
                r["input_tokens"] or 0,
                r["output_tokens"] or 0,
                r["cache_read_tokens"] or 0,
                r["cache_creation_tokens"] or 0,
            )
        conn.close()

    pace = calc_pace_ratio(cost_used, weekly_budget, elapsed_fraction)
    return {
        "plan":             plan,
        "plan_label":       PLAN_LABELS.get(plan, plan),
        "weekly_budget":    weekly_budget,
        "cost_used":        round(cost_used, 2),
        "pace_ratio":       round(pace, 3),
        "color":            pace_color(pace),
        "elapsed_fraction": round(elapsed_fraction, 4),
        "week_start_iso":   week_start.isoformat(),
        "week_end_iso":     week_end.isoformat(),
        "reset":            cfg["reset"],
    }


# Plan-config write surface — invoked by the GUI plan-switcher. Validates
# input rigorously: only known plan keys, custom budget within sensible
# bounds. The endpoint is localhost-only (HTTPServer binds 127.0.0.1) but
# we still don't want a malformed write to corrupt the config file.
def update_subscription_plan(plan, custom_budget=None, reset=None):
    """Write a new plan to disk. Returns (ok, error_message_or_None)."""
    if plan not in PLAN_BUDGETS:
        return False, f"Unknown plan: {plan}"
    budget = resolve_budget(plan, custom_budget)
    if budget is None:
        return False, "Invalid custom budget (must be a non-negative number)"
    if budget > 100000:  # sanity bound — no plan is anywhere near this
        return False, "Custom budget unreasonably large"
    new_cfg = {
        "plan": plan,
        "weekly_budget_api_equivalent": budget,
        "reset": reset or load_subscription_config().get("reset") or DEFAULT_CONFIG["reset"],
    }
    if not _is_valid_config(new_cfg):
        return False, "Resulting config failed validation"
    if not save_subscription_config(new_cfg):
        return False, "Could not write config file"
    return True, None


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  #error-banner { position: fixed; top: 0; left: 0; right: 0; padding: 12px 24px; background: #7f1d1d; color: #fee2e2; font-size: 13px; z-index: 9999; border-bottom: 1px solid #f87171; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  /* Subscription gauge — full-width card above the stats row */
  .gauge-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; display: flex; align-items: center; gap: 24px; }
  .gauge-svg { flex-shrink: 0; width: 120px; height: 80px; }
  .gauge-info { flex: 1; min-width: 0; }
  .gauge-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .gauge-value { font-size: 22px; font-weight: 700; }
  .gauge-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .gauge-pace { font-size: 13px; font-weight: 600; padding: 2px 8px; border-radius: 4px; display: inline-block; margin-top: 6px; }
  .gauge-pace.green  { background: rgba(74,222,128,0.18); color: #4ade80; }
  .gauge-pace.yellow { background: rgba(250,204,21,0.18); color: #facc15; }
  .gauge-pace.red    { background: rgba(248,113,113,0.18); color: #f87171; }
  .plan-select { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 6px; font-size: 12px; cursor: pointer; }
  .plan-select:hover { border-color: var(--accent); }
  .plan-custom-input { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 6px; font-size: 12px; width: 80px; }
  header .header-controls { display: flex; align-items: center; gap: 8px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .tz-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: rgba(248,113,113,0.8); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  tr.session-row { cursor: pointer; }
  tr.session-row.selected td { background: rgba(217,119,87,0.10); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .session-name { color: var(--text); font-weight: 600; }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }
  /* grid-template-rows: minmax(0, 70vh) caps the row height (max-height
     on the grid container alone is ignored — auto rows grow to content). */
  .detail-grid { display: grid; grid-template-columns: 1.2fr 0.8fr; grid-template-rows: minmax(0, 70vh); gap: 16px; align-items: stretch; }
  .detail-card { background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; }
  .detail-card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 12px; }
  .detail-grid > .detail-card { /* turn-history (left) — fill its grid track */ height: 100%; min-height: 0; }
  /* Right column: stacked tool-usage + cwds. Make it a flex column that
     fills the grid row, with the tool-usage card allowed to scroll its
     pill list when the list gets long. */
  .detail-grid > div { display: flex; flex-direction: column; gap: 16px; min-height: 0; height: 100%; }
  .detail-grid > div > .detail-card:first-child { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
  .detail-grid > div > .detail-card:last-child { flex: 0 0 auto; }
  .detail-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .detail-meta .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .detail-meta .value { font-size: 13px; }
  .pill-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .pill { border: 1px solid var(--border); border-radius: 999px; padding: 5px 10px; font-size: 12px; color: var(--text); background: rgba(255,255,255,0.02); }
  /* min-height: 0 lets the flex child shrink below its content size so the
     parent's max-height can clip it and inner scroll kicks in. */
  .detail-table-wrap { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; border: 1px solid var(--border); border-radius: 8px; }
  .detail-table-wrap table { font-size: 12px; }
  .detail-table-wrap table th, .detail-table-wrap table td { padding: 6px 8px; }
  .detail-table-wrap table th { position: sticky; top: 0; background: var(--card); }
  .detail-table-wrap td.cache { color: var(--muted); font-size: 11px; white-space: nowrap; }
  /* Tool-usage pills scale with usage: the top-used tool is largest, the rest taper. */
  .pill.heavy { font-size: 14px; padding: 8px 14px; font-weight: 600; }
  .pill.medium { font-size: 13px; padding: 6px 12px; }
  .pill.light { font-size: 11px; padding: 4px 9px; opacity: 0.8; }
  .hint { color: var(--muted); font-size: 12px; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) {
    .charts-grid { grid-template-columns: 1fr; }
    .chart-card.wide { grid-column: 1; }
    .detail-grid { grid-template-columns: 1fr; }
  }
</style>
<style id="theme-override"></style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <div class="header-controls">
    <select id="theme-select" class="plan-select" onchange="onThemeChange()" title="UI theme — persists in localStorage"></select>
    <select id="plan-select" class="plan-select" onchange="onPlanChange()" title="Subscription plan — sets the weekly budget for the gauge below"></select>
    <input id="plan-custom-input" class="plan-custom-input" type="number" min="0" step="1" placeholder="$/wk" onchange="onCustomBudgetChange()" style="display:none">
    <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
  </div>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="week" onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
</div>

<div class="container">
  <div class="gauge-card" id="gauge-card" style="display:none">
    <svg class="gauge-svg" viewBox="0 0 120 80" id="gauge-svg">
      <path d="M 10 70 A 50 50 0 0 1 110 70" stroke="var(--border)" stroke-width="10" fill="none"/>
      <path id="gauge-arc" d="M 10 70 A 50 50 0 0 1 110 70" stroke="#4ade80" stroke-width="10" fill="none" stroke-dasharray="0 200"/>
    </svg>
    <div class="gauge-info">
      <div class="gauge-label">Weekly subscription budget</div>
      <div class="gauge-value"><span id="gauge-cost">$0</span> / <span id="gauge-budget">$0</span></div>
      <div class="gauge-sub"><span id="gauge-plan">—</span> · resets <span id="gauge-reset">—</span> · <span id="gauge-elapsed">—</span> through the week</div>
      <span class="gauge-pace green" id="gauge-pace">on pace</span>
    </div>
  </div>
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <div class="chart-header">
        <h2 id="hourly-chart-title">Average Hourly Distribution</h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">Input <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">Cache Creation <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <div class="hint" style="margin-bottom:12px;">Click a session row for branch, tool, cwd, and turn history detail.</div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card" id="session-detail-card" style="display:none;">
    <div class="section-title">Session Detail</div>
    <div id="session-detail"></div>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th>Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')">Sessions <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')">Turns <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')">Input <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')">Output <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')">Est. Cost <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Tool Calls (across selected range)</div><button class="export-btn" onclick="exportToolCallsCSV()" title="Export tool-call breakdown to CSV">&#x2913; CSV</button></div>
    <p class="muted" style="font-size:11px;margin:0 0 8px 0">Aggregated across all sessions in the selected time window. Cowork sessions are excluded (audit logs don't carry tool data).</p>
    <table>
      <thead><tr>
        <th>Tool</th>
        <th>Model</th>
        <th class="sortable" onclick="setToolCallSort('turns')">Turns <span class="sort-icon" id="tcsort-turns"></span></th>
        <th class="sortable" onclick="setToolCallSort('input')">Input <span class="sort-icon" id="tcsort-input"></span></th>
        <th class="sortable" onclick="setToolCallSort('output')">Output <span class="sort-icon" id="tcsort-output"></span></th>
      </tr></thead>
      <tbody id="tool-calls-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let selectedSessionId = null;
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastToolCalls = [];
let toolCallSortCol = 'turns';
let toolCallSortDir = 'desc';
let sessionSortDir = 'desc';
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = /*__PRICING_JSON__*/;

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-7'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  // The DB stores timestamps as UTC ISO; the SQL groups by substr(timestamp,1,10)
  // which is the UTC date. Stay in UTC throughout this function so range
  // boundaries align with the data — using local-TZ math (getDate, getMonth,
  // Date(year,month,1) constructors) and then toISOString() shifts month/week
  // boundaries by ±1 day for any user not in UTC.
  if (range === 'all') return { start: null, end: null };
  const now = new Date();
  const todayUTC = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'week') {
    const day = todayUTC.getUTCDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(todayUTC); mon.setUTCDate(todayUTC.getUTCDate() - diffToMon);
    const sun = new Date(mon); sun.setUTCDate(mon.getUTCDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(Date.UTC(todayUTC.getUTCFullYear(), todayUTC.getUTCMonth(), 1));
    const end   = new Date(Date.UTC(todayUTC.getUTCFullYear(), todayUTC.getUTCMonth() + 1, 0));
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(Date.UTC(todayUTC.getUTCFullYear(), todayUTC.getUTCMonth() - 1, 1));
    const end   = new Date(Date.UTC(todayUTC.getUTCFullYear(), todayUTC.getUTCMonth(), 0));
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date(todayUTC); d.setUTCDate(todayUTC.getUTCDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    // Default = the billable subset, but fall back to all models when no model
    // matches opus/sonnet/haiku (empty-string model values, "unknown", legacy
    // IDs, third-party Claude proxies). Otherwise the dashboard renders blank
    // because every filter predicate uses selectedModels.has(r.model).
    const billable = allModels.filter(m => isBillable(m));
    return new Set(billable.length > 0 ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  // Mirror the readURLModels fallback so the URL serializer doesn't write
  // ?models=... when the rendered selection IS the implicit default.
  const billable = allModels.filter(m => isBillable(m));
  const defaultSet = billable.length > 0 ? billable : allModels;
  if (selectedModels.size !== defaultSet.length) return false;
  return defaultSet.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function selectSession(sessionId) {
  selectedSessionId = sessionId;
  document.querySelectorAll('tr.session-row').forEach(row =>
    row.classList.toggle('selected', row.dataset.sessionId === sessionId)
  );
  loadSessionDetail(sessionId);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Tool-call aggregation (filtered by model + range, summed across days)
  const toolMap = {};
  for (const r of (rawData.tool_calls_by_day || [])) {
    if (!selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    const key = r.tool + '|' + r.model;
    if (!toolMap[key]) toolMap[key] = { tool: r.tool, model: r.model, turns: 0, input: 0, output: 0 };
    toolMap[key].turns  += r.turns;
    toolMap[key].input  += r.input;
    toolMap[key].output += r.output;
  }
  const toolCalls = Object.values(toolMap);

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
  renderProjectCostTable(lastByProject.slice(0, 20));
  renderProjectBranchCostTable(lastByProjectBranch.slice(0, 20));
  lastToolCalls = sortToolCalls(toolCalls);
  renderToolCallsTable(lastToolCalls);

  const visibleSessions = lastFilteredSessions.slice(0, 20);
  if (!visibleSessions.length) {
    selectedSessionId = null;
    document.getElementById('session-detail-card').style.display = 'none';
  } else {
    if (!selectedSessionId || !visibleSessions.some(s => s.session_id_full === selectedSessionId)) {
      selectedSessionId = visibleSessions[0].session_id_full;
    }
    selectSession(selectedSessionId);
  }
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Apr 2026', color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => (h.peak ? '⚡ ' : '') + formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors = agg.hours.map(h => h.peak ? 'rgba(248,113,113,0.8)' : TOKEN_COLORS.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          data: turns,
          backgroundColor: barColors,
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(167,139,250,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: '#8892a4', callback: v => v.toFixed(1) },     grid: { color: '#2a2d3a' }, title: { display: true, text: 'Avg turns / hour',         color: '#8892a4', font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: '#8892a4', font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  ticks: { color: '#74de80', callback: v => fmt(v) }, grid: { color: '#2a2d3a' }, title: { display: true, text: 'Cache', color: '#74de80' } },
        y1: { position: 'right', ticks: { color: '#4f8ef7', callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: '#4f8ef7' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#1a1d27' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const sessionCell = s.session_name
      ? `<td><span class="session-name">${esc(s.session_name)}</span> <span class="muted" style="font-family:monospace">(${esc(s.session_id)}&hellip;)</span></td>`
      : `<td class="muted" style="font-family:monospace">${esc(s.session_id)}&hellip;</td>`;
    return `<tr class="session-row ${selectedSessionId === s.session_id_full ? 'selected' : ''}" data-session-id="${esc(s.session_id_full)}">
      ${sessionCell}
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  document.getElementById('sessions-body').addEventListener('click', function(e) {
    const row = e.target.closest('tr.session-row');
    if (row) selectSession(row.dataset.sessionId);
  });
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

async function loadSessionDetail(sessionId) {
  if (!sessionId) return;
  try {
    const resp = await fetch('/api/session?session_id=' + encodeURIComponent(sessionId));
    if (selectedSessionId !== sessionId) return;
    const detail = await resp.json();
    if (detail.error) return;
    renderSessionDetail(detail);
  } catch (e) {
    console.error(e);
  }
}

function renderSessionDetail(detail) {
  const detailCard = document.getElementById('session-detail-card');
  detailCard.style.display = '';

  // Scale pill weight by usage share. The top tool gets "heavy", the next
  // ~20% get "medium", the rest are "light" so the eye is drawn to the
  // dominant tools without losing the long tail.
  const maxToolTokens = Math.max(1, ...detail.tool_usage.map(t => t.tokens || 0));
  const toolPills = detail.tool_usage.length
    ? detail.tool_usage.map(t => {
        const share = (t.tokens || 0) / maxToolTokens;
        const cls = share >= 0.6 ? 'heavy' : share >= 0.2 ? 'medium' : 'light';
        return `<span class="pill ${cls}">${esc(t.tool_name)} · ${fmt(t.tokens)} tok · ${fmt(t.turns)}t</span>`;
      }).join('')
    : '<div class="hint">No tool usage recorded.</div>';

  const maxCwdTurns = Math.max(1, ...detail.cwd_usage.map(c => c.turns || 0));
  const cwdPills = detail.cwd_usage.length
    ? detail.cwd_usage.map(c => {
        const share = (c.turns || 0) / maxCwdTurns;
        const cls = share >= 0.6 ? 'heavy' : share >= 0.2 ? 'medium' : 'light';
        return `<span class="pill ${cls}">${esc(c.cwd)} · ${fmt(c.turns)}t</span>`;
      }).join('')
    : '<div class="hint">No working directory recorded.</div>';

  document.getElementById('session-detail').innerHTML = `
    <div class="detail-meta">
      <div><div class="label">Session</div><div class="value" style="font-family:monospace">${esc(detail.session_id)}</div></div>
      <div><div class="label">Project</div><div class="value">${esc(detail.project)}</div></div>
      <div><div class="label">Branch</div><div class="value">${esc(detail.branch || 'n/a')}</div></div>
      <div><div class="label">Model</div><div class="value">${esc(detail.model)}</div></div>
      <div><div class="label">First Seen</div><div class="value">${esc(detail.first)}</div></div>
      <div><div class="label">Last Seen</div><div class="value">${esc(detail.last)}</div></div>
      <div><div class="label">Duration</div><div class="value">${esc(detail.duration_min)}m</div></div>
      <div><div class="label">Tokens</div><div class="value">${fmt(detail.input + detail.output + detail.cache_read + detail.cache_creation)}</div></div>
    </div>
    <div class="detail-grid">
      <div class="detail-card">
        <h3>Turn History</h3>
        <div class="detail-table-wrap">
          <table>
            <thead><tr>
              <th>Time</th><th>Tool</th><th>In</th><th>Out</th><th>Cache (R/W)</th><th>Total</th>
            </tr></thead>
            <tbody>
              ${detail.turn_history.map(turn => `
                <tr>
                  <td class="muted">${esc((turn.timestamp_short || '').slice(5, 16))}</td>
                  <td>${esc(turn.tool_name)}</td>
                  <td class="num">${fmt(turn.input)}</td>
                  <td class="num">${fmt(turn.output)}</td>
                  <td class="num cache">${fmt(turn.cache_read)} / ${fmt(turn.cache_creation)}</td>
                  <td class="num">${fmt(turn.total)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="detail-card" style="margin-bottom:16px;">
          <h3>Tool Usage</h3>
          <div class="pill-list">${toolPills}</div>
        </div>
        <div class="detail-card">
          <h3>Working Directories</h3>
          <div class="pill-list">${cwdPills}</div>
        </div>
      </div>
    </div>
  `;
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = sortModels(byModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  document.getElementById('project-cost-body').innerHTML = sortProjects(byProject).map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  return [...rows].sort((a, b) => {
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    if (pa < pb) return -1;
    if (pa > pb) return 1;
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectBranchCostTable(rows) {
  document.getElementById('project-branch-cost-body').innerHTML = sortProjectBranch(rows).map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
}

function sortToolCalls(rows) {
  return [...rows].sort((a, b) => {
    const av = a[toolCallSortCol] ?? 0;
    const bv = b[toolCallSortCol] ?? 0;
    if (av < bv) return toolCallSortDir === 'desc' ? 1 : -1;
    if (av > bv) return toolCallSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function setToolCallSort(col) {
  if (toolCallSortCol === col) {
    toolCallSortDir = toolCallSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    toolCallSortCol = col;
    toolCallSortDir = 'desc';
  }
  document.querySelectorAll('[id^="tcsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('tcsort-' + toolCallSortCol);
  if (icon) icon.textContent = toolCallSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
  renderToolCallsTable(sortToolCalls(lastToolCalls));
}

function renderToolCallsTable(rows) {
  const body = document.getElementById('tool-calls-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">No tool calls in selected range.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `<tr>
    <td><span class="tool-tag" style="font-family:monospace">${esc(r.tool)}</span></td>
    <td><span class="model-tag">${esc(r.model)}</span></td>
    <td class="num">${fmt(r.turns)}</td>
    <td class="num">${fmt(r.input)}</td>
    <td class="num">${fmt(r.output)}</td>
  </tr>`).join('');
}

function exportToolCallsCSV() {
  const headers = ['tool','model','turns','input_tokens','output_tokens'];
  const rows = lastToolCalls.map(r => [r.tool, r.model, r.turns, r.input, r.output]);
  const csv = [headers, ...rows].map(row => row.map(csvField).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'tool-calls-' + csvTimestamp() + '.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportSessionsCSV() {
  const header = ['Session ID', 'Session Name', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.session_name || '', s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + esc(d.error) + '</div>';
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? ' \u00b7 Auto-refresh in 30s' : '';
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
  } catch(e) {
    // Surface the error in the DOM instead of console-only — every
    // "blank dashboard" report on the upstream issue tracker (#74/76/88
    // /90/92/93/99/106) was harder to diagnose because exceptions thrown
    // in applyFilter() got silently swallowed here.
    console.error(e);
    showErrorBanner(e);
  }
}

function showErrorBanner(e) {
  let banner = document.getElementById('error-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'error-banner';
    document.body.prepend(banner);
  }
  banner.textContent = 'Dashboard error: ' + (e?.message || String(e)) + ' (see browser console for stack trace)';
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

// ── Theme switcher ─────────────────────────────────────────────────────────
// Themes injected from Python BUNDLED_THEMES at render time. Selection
// persists in localStorage; applied via a <style id="theme-override"> tag
// that follows the main <style> block so its :root overrides cascade.
const BUNDLED_THEMES = /*__THEMES_JSON__*/;

function applyTheme(id) {
  const t = BUNDLED_THEMES.find(x => x.id === id) || BUNDLED_THEMES[0];
  document.getElementById('theme-override').textContent = t.css;
  localStorage.setItem('dashboard-theme-id', t.id);
  document.getElementById('theme-select').value = t.id;
}

function initThemeSwitcher() {
  const sel = document.getElementById('theme-select');
  sel.innerHTML = BUNDLED_THEMES.map(t =>
    `<option value="${esc(t.id)}">${esc(t.name)}</option>`
  ).join('');
  applyTheme(localStorage.getItem('dashboard-theme-id') || 'default');
}

function onThemeChange() {
  applyTheme(document.getElementById('theme-select').value);
}

// ── Subscription budget gauge + plan-switcher ──────────────────────────────
let subscriptionPlans = [];

async function loadSubscriptionConfig() {
  try {
    const resp = await fetch('/api/subscription/config');
    const cfg = await resp.json();
    subscriptionPlans = cfg.available;
    const sel = document.getElementById('plan-select');
    sel.innerHTML = subscriptionPlans.map(p =>
      `<option value="${esc(p.value)}">${esc(p.label)}${p.budget !== null ? ' ($' + p.budget + '/wk)' : ''}</option>`
    ).join('');
    sel.value = cfg.current.plan;
    document.getElementById('plan-custom-input').style.display =
      cfg.current.plan === 'custom' ? 'inline-block' : 'none';
    if (cfg.current.plan === 'custom') {
      document.getElementById('plan-custom-input').value = cfg.current.weekly_budget_api_equivalent || '';
    }
  } catch (e) { console.error('subscription/config failed', e); }
}

async function loadSubscriptionGauge() {
  try {
    const resp = await fetch('/api/subscription');
    const d = await resp.json();
    if (!d.weekly_budget) {
      document.getElementById('gauge-card').style.display = 'none';
      return;
    }
    document.getElementById('gauge-card').style.display = 'flex';
    document.getElementById('gauge-cost').textContent = '$' + Number(d.cost_used).toLocaleString(undefined, {maximumFractionDigits:2});
    document.getElementById('gauge-budget').textContent = '$' + Number(d.weekly_budget).toLocaleString();
    document.getElementById('gauge-plan').textContent = d.plan_label;
    document.getElementById('gauge-reset').textContent = d.reset.day + ' ' + d.reset.time + ' ' + d.reset.timezone;
    document.getElementById('gauge-elapsed').textContent = Math.round(d.elapsed_fraction * 100) + '%';
    // Arc: 200 unit perimeter (matching half-circle), pace ratio capped at 1.5 for visual.
    const fillFraction = Math.min(1, d.cost_used / d.weekly_budget);
    const arc = document.getElementById('gauge-arc');
    arc.setAttribute('stroke-dasharray', (fillFraction * 158) + ' 200');
    const colorMap = { green: '#4ade80', yellow: '#facc15', red: '#f87171' };
    arc.setAttribute('stroke', colorMap[d.color] || '#4ade80');
    const paceEl = document.getElementById('gauge-pace');
    paceEl.className = 'gauge-pace ' + d.color;
    paceEl.textContent = d.pace_ratio < 0.001 ? 'no spend yet'
      : d.pace_ratio < 1.0 ? 'under pace (' + d.pace_ratio.toFixed(2) + '×)'
      : d.pace_ratio < 1.2 ? 'on pace (' + d.pace_ratio.toFixed(2) + '×)'
      : d.pace_ratio < 1.5 ? 'fast (' + d.pace_ratio.toFixed(2) + '×)'
      : 'over budget (' + d.pace_ratio.toFixed(2) + '×)';
  } catch (e) { console.error('subscription gauge failed', e); }
}

async function onPlanChange() {
  const plan = document.getElementById('plan-select').value;
  document.getElementById('plan-custom-input').style.display = plan === 'custom' ? 'inline-block' : 'none';
  if (plan === 'custom') return;  // wait for the custom input
  await postPlan(plan);
}

async function onCustomBudgetChange() {
  const v = parseFloat(document.getElementById('plan-custom-input').value);
  if (!isFinite(v) || v < 0) return;
  await postPlan('custom', v);
}

async function postPlan(plan, customBudget) {
  try {
    const body = customBudget !== undefined ? { plan, custom_budget: customBudget } : { plan };
    const resp = await fetch('/api/subscription/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await resp.json();
    if (!d.ok) {
      showErrorBanner(new Error('Plan update failed: ' + (d.error || 'unknown')));
      return;
    }
    await loadSubscriptionGauge();
  } catch (e) { showErrorBanner(e); }
}

initThemeSwitcher();
loadData();
loadSubscriptionConfig().then(loadSubscriptionGauge);
scheduleAutoRefresh();
</script>
</body>
</html>
"""


# Bundled themes — extracted from josepe98's PR #63 BUNDLED_THEMES data
# (the data only, not their dashboard-wide refactor). The "default" entry
# captures the current built-in dark palette so users can revert to it.
BUNDLED_THEMES = [
    {"id": "default", "name": "Default Dark", "category": "Built-in",
     "css": ":root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;--muted:#8892a4;--accent:#d97757;--blue:#4f8ef7;--green:#4ade80;}"},
    {"id": "apple", "name": "Apple", "category": "Enterprise & Consumer",
     "css": ":root{--bg:#f5f5f7;--card:#ffffff;--border:rgba(0,0,0,0.08);--text:#1d1d1f;--muted:rgba(0,0,0,0.48);--accent:#0071e3;--green:#1c7a3a;--blue:#0071e3;}"},
    {"id": "linear", "name": "Linear", "category": "Developer Tools",
     "css": ":root{--bg:#0f0f10;--card:#1a1a1b;--border:rgba(255,255,255,0.08);--text:#e8e8e8;--muted:rgba(255,255,255,0.4);--accent:#5e6ad2;--green:#4ade80;--blue:#5e6ad2;}"},
    {"id": "vercel", "name": "Vercel", "category": "Developer Tools",
     "css": ":root{--bg:#000000;--card:#111111;--border:rgba(255,255,255,0.1);--text:#ffffff;--muted:rgba(255,255,255,0.4);--accent:#50e3c2;--green:#50e3c2;--blue:#aaaaaa;}"},
    {"id": "notion", "name": "Notion", "category": "Design & Productivity",
     "css": ":root{--bg:#ffffff;--card:#f7f7f5;--border:rgba(55,53,47,0.09);--text:#37352f;--muted:rgba(55,53,47,0.5);--accent:#2eaadc;--green:#0f7b6c;--blue:#2eaadc;}"},
    {"id": "stripe", "name": "Stripe", "category": "Infrastructure & Cloud",
     "css": ":root{--bg:#f6f9fc;--card:#ffffff;--border:rgba(0,0,0,0.1);--text:#0a2540;--muted:rgba(10,37,64,0.5);--accent:#635bff;--green:#09825d;--blue:#0070f3;}"},
]


# Inject the Python PRICING + THEMES tables into the HTML once at import time
# so the JS copies can never drift from the Python ones — and so each / request
# under the threaded server doesn't re-template a 50KB string.
_RENDERED_HTML = HTML_TEMPLATE.replace(
    "/*__PRICING_JSON__*/",
    json.dumps(PRICING),
).replace(
    "/*__THEMES_JSON__*/",
    json.dumps(BUNDLED_THEMES),
).encode("utf-8")


def render_html():
    return _RENDERED_HTML


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404. The full parsed object is also
        # needed for /api/session which reads ?session_id=...
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_html())

        elif parsed.path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/session":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            data = get_session_detail(session_id)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200 if "error" not in data else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/subscription":
            data = get_subscription_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/subscription/config":
            cfg = load_subscription_config()
            body = json.dumps({
                "current": cfg,
                "available": [{"value": k, "label": PLAN_LABELS[k], "budget": v}
                              for k, v in PLAN_BUDGETS.items()],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/subscription/config":
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 1024:  # plan-config is tiny — bound the body
                self.send_response(413); self.end_headers(); return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_response(400); self.end_headers(); return
            ok, err = update_subscription_plan(
                plan=payload.get("plan"),
                custom_budget=payload.get("custom_budget"),
                reset=payload.get("reset"),
            )
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            if db_path.exists():
                db_path.unlink()
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
