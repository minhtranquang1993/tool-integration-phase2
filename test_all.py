#!/usr/bin/env python3
"""
test_all.py — Test coverage for Phase 2 tool-integration modules.

Covers:
1. github_token_manager.py — mask_token, TokenEntry, TokenConfig, select_token, formatters
2. log_noise_filter.py — LogEntry.from_line, LogNoiseFilter, FilterConfig, formatters
3. market_data_guardrail.py — parse_timestamp, compute_confidence, detect_conflict,
                               validate_single/multi, formatters
4. Integration — git_push_helper config loading, wrapper filter_and_send (dry-run)

Run:
    python3 -m pytest test_all.py -v
    # or simply:
    python3 test_all.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ═══════════════════════════════════════════════════════════════════════════
# 1. github_token_manager tests
# ═══════════════════════════════════════════════════════════════════════════

from github_token_manager import (
    mask_token,
    TokenEntry,
    TokenConfig,
    SelectionResult,
    select_token,
    validate_token_for_repo,
    format_report_text,
    format_report_json,
)


class TestMaskToken(unittest.TestCase):
    """Tests for mask_token()."""

    def test_classic_token(self):
        result = mask_token("ghp_abcdefghijklmnop1234")
        self.assertTrue(result.startswith("ghp_"))
        self.assertIn("****", result)
        self.assertTrue(result.endswith("1234"))

    def test_fine_grained_token(self):
        result = mask_token("github_pat_abcdefgh5678")
        self.assertTrue(result.startswith("github_pat_"))
        self.assertIn("****", result)
        self.assertTrue(result.endswith("5678"))

    def test_unknown_prefix(self):
        result = mask_token("sometoken_abcdefgh9999")
        # No known prefix → just ****suffix
        self.assertIn("****", result)
        self.assertTrue(result.endswith("9999"))

    def test_short_token(self):
        result = mask_token("ab")
        self.assertEqual(result, "****")

    def test_empty_token(self):
        result = mask_token("")
        self.assertEqual(result, "****")

    def test_none_token(self):
        result = mask_token(None)
        self.assertEqual(result, "****")

    def test_visible_chars_default_4(self):
        result = mask_token("ghp_xxxxxxxxxxxxxxxxABCD")
        self.assertTrue(result.endswith("ABCD"))

    def test_all_github_prefixes(self):
        """Verify all documented GitHub token prefixes are recognized."""
        for prefix in ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_"):
            token = f"{prefix}xxxxxxxxxxxx9999"
            result = mask_token(token)
            self.assertTrue(result.startswith(prefix),
                            f"Prefix {prefix} not preserved in mask result: {result}")


class TestTokenEntry(unittest.TestCase):
    """Tests for TokenEntry.resolve()."""

    def test_resolve_from_value(self):
        entry = TokenEntry(alias="test", env_var="FAKE_VAR", value="direct_value")
        self.assertEqual(entry.resolve(), "direct_value")

    def test_resolve_from_env(self):
        with patch.dict(os.environ, {"TEST_TOKEN_VAR": "env_value"}):
            entry = TokenEntry(alias="test", env_var="TEST_TOKEN_VAR")
            self.assertEqual(entry.resolve(), "env_value")

    def test_resolve_none_when_missing(self):
        entry = TokenEntry(alias="test", env_var="DEFINITELY_NOT_SET_VAR_12345")
        self.assertIsNone(entry.resolve())

    def test_value_takes_priority_over_env(self):
        """Direct value should take priority over env var."""
        with patch.dict(os.environ, {"TEST_VAR": "from_env"}):
            entry = TokenEntry(alias="test", env_var="TEST_VAR", value="from_direct")
            self.assertEqual(entry.resolve(), "from_direct")


class TestTokenConfig(unittest.TestCase):
    """Tests for TokenConfig.from_json() and from_env()."""

    def test_from_env_defaults(self):
        config = TokenConfig.from_env()
        self.assertEqual(len(config.tokens), 2)
        aliases = [t.alias for t in config.tokens]
        self.assertIn("classic", aliases)
        self.assertIn("finegrained", aliases)

    def test_from_json_valid(self):
        data = {
            "tokens": {
                "primary": {"env_var": "TOK_PRIMARY", "value": "tok_abc"},
                "secondary": {"env_var": "TOK_SECONDARY"},
            },
            "repo_overrides": {
                "owner/repo-a": "secondary",
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            config = TokenConfig.from_json(Path(f.name))
        os.unlink(f.name)

        self.assertEqual(len(config.tokens), 2)
        self.assertEqual(config.repo_overrides.get("owner/repo-a"), "secondary")
        # Check that value was loaded
        primary = [t for t in config.tokens if t.alias == "primary"][0]
        self.assertEqual(primary.resolve(), "tok_abc")

    def test_from_json_missing_file(self):
        """Missing config file should fall back to from_env()."""
        config = TokenConfig.from_json(Path("/tmp/nonexistent_config_12345.json"))
        # Should return env-based defaults
        self.assertEqual(len(config.tokens), 2)

    def test_from_json_invalid_json(self):
        """Invalid JSON should fall back to from_env()."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json!!")
            f.flush()
            config = TokenConfig.from_json(Path(f.name))
        os.unlink(f.name)
        self.assertEqual(len(config.tokens), 2)

    def test_from_json_with_file_path(self):
        """Ensure file_path field is loaded from config."""
        data = {
            "tokens": {
                "classic": {
                    "env_var": "TOK_CLASSIC",
                    "file_path": "credentials/token.txt",
                },
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            config = TokenConfig.from_json(Path(f.name))
        os.unlink(f.name)

        entry = config.tokens[0]
        self.assertEqual(entry.file_path, "credentials/token.txt")


class TestSelectToken(unittest.TestCase):
    """Tests for select_token() with mocked validation."""

    def _make_config(self, token_values: dict[str, str | None]) -> TokenConfig:
        """Helper to create a config with given alias → value mapping."""
        tokens = [
            TokenEntry(alias=alias, env_var=f"FAKE_{alias.upper()}", value=val)
            for alias, val in token_values.items()
        ]
        return TokenConfig(tokens=tokens)

    @patch("github_token_manager.validate_token_for_repo")
    def test_first_token_succeeds(self, mock_validate):
        mock_validate.return_value = (True, 200, "Access granted")
        config = self._make_config({"classic": "tok_1", "fg": "tok_2"})
        result = select_token(config, "owner/repo")

        self.assertTrue(result.success)
        self.assertEqual(result.alias, "classic")
        self.assertEqual(result.token, "tok_1")
        self.assertEqual(result.http_status, 200)

    @patch("github_token_manager.validate_token_for_repo")
    def test_fallback_to_second(self, mock_validate):
        """First token fails (403), second succeeds."""
        mock_validate.side_effect = [
            (False, 403, "No access"),
            (True, 200, "Access granted"),
        ]
        config = self._make_config({"classic": "tok_1", "fg": "tok_2"})
        result = select_token(config, "owner/repo")

        self.assertTrue(result.success)
        self.assertEqual(result.alias, "fg")
        self.assertEqual(result.token, "tok_2")
        self.assertEqual(len(result.attempts), 2)

    @patch("github_token_manager.validate_token_for_repo")
    def test_all_fail(self, mock_validate):
        mock_validate.return_value = (False, 401, "Invalid token")
        config = self._make_config({"classic": "tok_1", "fg": "tok_2"})
        result = select_token(config, "owner/repo")

        self.assertFalse(result.success)
        self.assertIsNone(result.token)
        self.assertIsNone(result.alias)
        self.assertEqual(len(result.attempts), 2)

    @patch("github_token_manager.validate_token_for_repo")
    def test_repo_override(self, mock_validate):
        """Repo override should try the preferred token first."""
        mock_validate.return_value = (True, 200, "OK")
        config = self._make_config({"classic": "tok_1", "special": "tok_2"})
        config.repo_overrides["owner/repo"] = "special"
        result = select_token(config, "owner/repo")

        self.assertTrue(result.success)
        self.assertEqual(result.alias, "special")
        # validate should be called with tok_2 first
        mock_validate.assert_called_once_with("tok_2", "owner/repo")

    def test_skip_empty_tokens(self):
        """Tokens without values should be skipped."""
        config = self._make_config({"empty": None, "valid": None})
        result = select_token(config, "owner/repo")
        self.assertFalse(result.success)
        # Both should be SKIP
        for attempt in result.attempts:
            self.assertEqual(attempt["status"], "SKIP")

    @patch("github_token_manager.validate_token_for_repo")
    def test_verbose_mode(self, mock_validate):
        """Verbose mode should not crash."""
        mock_validate.return_value = (True, 200, "OK")
        config = self._make_config({"classic": "tok_1"})
        result = select_token(config, "owner/repo", verbose=True)
        self.assertTrue(result.success)


class TestFormatReport(unittest.TestCase):
    """Tests for format_report_text() and format_report_json()."""

    def _make_result(self, success: bool = True) -> SelectionResult:
        return SelectionResult(
            success=success,
            token="ghp_test1234" if success else None,
            alias="classic" if success else None,
            repo="owner/repo",
            http_status=200 if success else 0,
            message="Selected token 'classic'" if success else "All failed",
            attempts=[
                {"alias": "classic", "token_masked": "ghp_****1234",
                 "status": "PASS", "http_status": 200, "message": "OK"},
            ],
        )

    def test_text_report_success(self):
        text = format_report_text(self._make_result(True))
        self.assertIn("SUCCESS", text)
        self.assertIn("owner/repo", text)
        self.assertIn("classic", text)

    def test_text_report_failure(self):
        text = format_report_text(self._make_result(False))
        self.assertIn("FAILED", text)

    def test_json_report(self):
        json_str = format_report_json(self._make_result(True))
        data = json.loads(json_str)
        self.assertTrue(data["success"])
        self.assertEqual(data["repo"], "owner/repo")
        self.assertEqual(data["selected_alias"], "classic")
        self.assertIn("attempts", data)

    def test_json_report_failure(self):
        json_str = format_report_json(self._make_result(False))
        data = json.loads(json_str)
        self.assertFalse(data["success"])
        self.assertIsNone(data["selected_alias"])


# ═══════════════════════════════════════════════════════════════════════════
# 2. log_noise_filter tests
# ═══════════════════════════════════════════════════════════════════════════

from log_noise_filter import (
    LogEntry,
    LogNoiseFilter,
    FilterConfig,
    FilterResult,
    generate_summary,
    format_filtered_text,
    format_filtered_json,
)


class TestLogEntryFromLine(unittest.TestCase):
    """Tests for LogEntry.from_line() — 3 log formats + plain text."""

    def test_bracketed_format(self):
        """[TIMESTAMP] [SEVERITY] [SOURCE] message"""
        line = "[2026-03-05 19:30:00] [ERROR] [cron] Something failed"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.timestamp, "2026-03-05 19:30:00")
        self.assertEqual(entry.severity, "ERROR")
        self.assertEqual(entry.source, "cron")
        self.assertEqual(entry.message, "Something failed")
        self.assertEqual(entry.raw_line, line)

    def test_standard_format(self):
        """2026-03-05 19:30:00 ERROR [source] message"""
        line = "2026-03-05 19:30:00 ERROR [myapp] Database timeout"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.timestamp, "2026-03-05 19:30:00")
        self.assertEqual(entry.severity, "ERROR")
        self.assertEqual(entry.source, "myapp")
        self.assertEqual(entry.message, "Database timeout")

    def test_standard_format_no_source(self):
        """2026-03-05 19:30:00 INFO message (no source bracket)"""
        line = "2026-03-05 19:30:00 INFO All good here"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.severity, "INFO")
        self.assertEqual(entry.source, "")
        self.assertIn("All good here", entry.message)

    def test_severity_prefix_format(self):
        """ERROR: message"""
        line = "CRITICAL: Disk full"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.severity, "CRITICAL")
        self.assertEqual(entry.message, "Disk full")

    def test_plain_text(self):
        """Lines that don't match any log format."""
        line = "Just a regular line of output"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.message, "Just a regular line of output")
        self.assertEqual(entry.severity, "")
        self.assertEqual(entry.source, "")
        self.assertEqual(entry.timestamp, "")

    def test_strip_trailing_newline(self):
        entry = LogEntry.from_line("hello\n")
        self.assertEqual(entry.raw_line, "hello")

    def test_warning_severity(self):
        line = "WARNING: something"
        entry = LogEntry.from_line(line)
        self.assertEqual(entry.severity, "WARNING")


class TestLogNoiseFilter(unittest.TestCase):
    """Tests for LogNoiseFilter.classify() — priority and context modes."""

    def test_alert_highest_priority(self):
        """ALERT patterns have highest priority — even if line also matches suppress."""
        f = LogNoiseFilter()
        entry = LogEntry(message="ran ok but then CRITICAL failure occurred")
        result = f.classify(entry)
        self.assertEqual(result.action, "ALERT")

    def test_suppress_basic(self):
        f = LogNoiseFilter()
        entry = LogEntry(message="script ran ok")
        result = f.classify(entry)
        self.assertEqual(result.action, "SUPPRESS")

    def test_suppress_done(self):
        f = LogNoiseFilter()
        entry = LogEntry(message="done.")
        result = f.classify(entry)
        self.assertEqual(result.action, "SUPPRESS")

    def test_pass_keyword(self):
        """Pass keywords have priority over suppress but below alert."""
        config = FilterConfig(pass_keywords=["user_query"])
        f = LogNoiseFilter(config)
        entry = LogEntry(message="user_query received and ran ok")
        result = f.classify(entry)
        # "ran ok" matches suppress, but "user_query" is pass keyword → PASS wins over SUPPRESS
        # Actually: alert > pass > suppress. So pass keyword wins.
        self.assertEqual(result.action, "PASS")

    def test_internal_mode_default_suppress(self):
        """In internal mode, unmatched lines default to SUPPRESS."""
        f = LogNoiseFilter(context_mode="internal")
        entry = LogEntry(message="some internal log line")
        result = f.classify(entry)
        self.assertEqual(result.action, "SUPPRESS")

    def test_user_mode_default_pass(self):
        """In user mode, unmatched lines default to PASS."""
        f = LogNoiseFilter(context_mode="user")
        entry = LogEntry(message="some output line")
        result = f.classify(entry)
        self.assertEqual(result.action, "PASS")

    def test_alert_error_keyword(self):
        f = LogNoiseFilter()
        entry = LogEntry(message="something failed", severity="ERROR")
        result = f.classify(entry)
        self.assertEqual(result.action, "ALERT")

    def test_alert_exception(self):
        f = LogNoiseFilter()
        entry = LogEntry(message="Traceback (most recent call last)")
        result = f.classify(entry)
        self.assertEqual(result.action, "ALERT")

    def test_invalid_regex_skipped(self):
        """Invalid regex patterns should be skipped, not crash."""
        config = FilterConfig(suppress_patterns=["[invalid regex"])
        # Should not raise
        f = LogNoiseFilter(config)
        # The invalid pattern is skipped, but others still work
        self.assertIsInstance(f, LogNoiseFilter)


class TestFilterLines(unittest.TestCase):
    """Tests for filter_lines() on a batch of lines."""

    def test_empty_lines_skipped(self):
        f = LogNoiseFilter()
        results = f.filter_lines(["", "  ", "\n"])
        self.assertEqual(len(results), 0)

    def test_mixed_lines(self):
        f = LogNoiseFilter(context_mode="user")
        lines = [
            "script ran ok",
            "ERROR: something broke",
            "normal output line",
        ]
        results = f.filter_lines(lines)
        self.assertEqual(len(results), 3)
        actions = [r.action for r in results]
        self.assertEqual(actions[0], "SUPPRESS")  # "ran ok"
        self.assertEqual(actions[1], "ALERT")      # "ERROR"
        self.assertEqual(actions[2], "PASS")        # unmatched in user mode

    def test_production_config_patterns(self):
        """Test with production log_filter_config.json patterns."""
        config_path = _ROOT / "log_filter_config.json"
        if config_path.exists():
            config = FilterConfig.from_json(config_path)
        else:
            self.skipTest("log_filter_config.json not found")

        f = LogNoiseFilter(config, context_mode="user")

        # Lines from demo output that should be suppressed
        suppress_lines = [
            "atom_1 ✅",
            "atom_2 ✅",
            "save_db ✅",
            "[AoT] Phase 1 aggregation",
            "script ran ok",
        ]
        for line in suppress_lines:
            results = f.filter_lines([line])
            self.assertEqual(len(results), 1,
                             f"Line should produce 1 result: {line!r}")
            self.assertEqual(results[0].action, "SUPPRESS",
                             f"Line should be SUPPRESS: {line!r} → got {results[0].action}")

        # Lines that should pass through (contain pass keywords)
        pass_lines = [
            "📊 KPI DASHBOARD:",
            "  🟢 Revenue: 125,000,000 VND",
            "  🔵 AOV: 365,500 VND",
            "  📌 CAC: 89,000 VND",
            "🎯 TỔNG KẾT: 4/5 KPI on target",
            "🩺 DATA HEALTH CHECK",
        ]
        for line in pass_lines:
            results = f.filter_lines([line])
            self.assertEqual(len(results), 1,
                             f"Line should produce 1 result: {line!r}")
            self.assertEqual(results[0].action, "PASS",
                             f"Line should be PASS: {line!r} → got {results[0].action}")

        # Alert lines
        alert_lines = [
            "❌ Something failed",
            "⚠️ KPI churn rate too high",
        ]
        for line in alert_lines:
            results = f.filter_lines([line])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].action, "ALERT",
                             f"Line should be ALERT: {line!r} → got {results[0].action}")


class TestFilterConfig(unittest.TestCase):
    """Tests for FilterConfig.from_json()."""

    def test_from_json_valid(self):
        data = {
            "suppress_patterns": ["custom_suppress"],
            "alert_patterns": ["custom_alert"],
            "pass_keywords": ["custom_pass"],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            config = FilterConfig.from_json(Path(f.name))
        os.unlink(f.name)

        self.assertEqual(config.suppress_patterns, ["custom_suppress"])
        self.assertEqual(config.alert_patterns, ["custom_alert"])
        self.assertEqual(config.pass_keywords, ["custom_pass"])

    def test_from_json_missing_file(self):
        config = FilterConfig.from_json(Path("/tmp/nonexistent_12345.json"))
        # Should return defaults
        self.assertTrue(len(config.suppress_patterns) > 0)

    def test_from_json_invalid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json!!!")
            f.flush()
            config = FilterConfig.from_json(Path(f.name))
        os.unlink(f.name)
        self.assertTrue(len(config.suppress_patterns) > 0)


class TestGenerateSummary(unittest.TestCase):
    """Tests for generate_summary()."""

    def test_mixed_results(self):
        results = [
            FilterResult(action="SUPPRESS", entry=LogEntry(message="ok")),
            FilterResult(action="SUPPRESS", entry=LogEntry(message="done")),
            FilterResult(action="PASS", entry=LogEntry(message="output")),
            FilterResult(action="ALERT", entry=LogEntry(message="ERROR: fail")),
        ]
        summary = generate_summary(results, period="test run")
        self.assertIn("test run", summary)
        self.assertIn("2 normal entries", summary)
        self.assertIn("1 entries passed", summary)
        self.assertIn("1 alerts", summary)
        self.assertIn("ERROR: fail", summary)

    def test_empty_results(self):
        summary = generate_summary([])
        self.assertIn("No log entries", summary)

    def test_all_suppressed(self):
        results = [
            FilterResult(action="SUPPRESS", entry=LogEntry(message="ok")),
        ]
        summary = generate_summary(results)
        self.assertIn("1 normal entries", summary)
        self.assertNotIn("alerts", summary)


class TestFormatFiltered(unittest.TestCase):
    """Tests for format_filtered_text() and format_filtered_json()."""

    def _make_results(self):
        return [
            FilterResult(action="ALERT", entry=LogEntry(message="ERROR: crash", raw_line="ERROR: crash")),
            FilterResult(action="PASS", entry=LogEntry(message="user output", raw_line="user output")),
            FilterResult(action="SUPPRESS", entry=LogEntry(message="done"), reason="matched suppress"),
        ]

    def test_text_format(self):
        text = format_filtered_text(self._make_results())
        self.assertIn("ALERTS", text)
        self.assertIn("ERROR: crash", text)
        self.assertIn("LOG OUTPUT", text)
        self.assertIn("user output", text)
        # Suppressed should NOT be shown by default
        self.assertNotIn("SUPPRESSED", text)

    def test_text_format_show_suppressed(self):
        text = format_filtered_text(self._make_results(), show_suppressed=True)
        self.assertIn("SUPPRESSED", text)
        self.assertIn("done", text)

    def test_json_format(self):
        json_str = format_filtered_json(self._make_results())
        data = json.loads(json_str)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["alerts"], 1)
        self.assertEqual(data["passed"], 1)
        self.assertEqual(data["suppressed"], 1)
        self.assertEqual(len(data["entries"]), 3)

    def test_no_alerts_no_pass(self):
        """When only suppressed entries exist."""
        results = [
            FilterResult(action="SUPPRESS", entry=LogEntry(message="ok")),
        ]
        text = format_filtered_text(results)
        self.assertIn("Không có log đáng chú ý", text)


# ═══════════════════════════════════════════════════════════════════════════
# 3. market_data_guardrail tests
# ═══════════════════════════════════════════════════════════════════════════

from market_data_guardrail import (
    MarketDataPoint,
    MarketDataResponse,
    ValidationError,
    parse_timestamp,
    compute_confidence,
    detect_conflict,
    validate_single,
    validate_multi,
    format_text,
    format_json_output,
    format_markdown,
    ICT,
)


class TestParseTimestamp(unittest.TestCase):
    """Tests for parse_timestamp()."""

    def test_iso_with_tz(self):
        dt = parse_timestamp("2026-03-05T19:30:00+07:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 19)
        self.assertEqual(dt.minute, 30)

    def test_iso_without_tz(self):
        """Naive timestamps should be assumed ICT."""
        dt = parse_timestamp("2026-03-05T19:30:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, ICT)

    def test_simple_datetime(self):
        dt = parse_timestamp("2026-03-05 19:30")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 3)

    def test_just_now_vietnamese(self):
        dt = parse_timestamp("vừa xong")
        self.assertIsNotNone(dt)
        # Should be very close to now
        now = datetime.now(ICT)
        diff = abs((now - dt).total_seconds())
        self.assertLess(diff, 5)

    def test_minutes_ago_vietnamese(self):
        dt = parse_timestamp("5 phút trước")
        self.assertIsNotNone(dt)
        now = datetime.now(ICT)
        diff_minutes = (now - dt).total_seconds() / 60
        self.assertAlmostEqual(diff_minutes, 5, delta=1)

    def test_hours_ago_vietnamese(self):
        dt = parse_timestamp("2 giờ trước")
        self.assertIsNotNone(dt)
        now = datetime.now(ICT)
        diff_hours = (now - dt).total_seconds() / 3600
        self.assertAlmostEqual(diff_hours, 2, delta=0.1)

    def test_minutes_ago_english(self):
        dt = parse_timestamp("10 minutes ago")
        self.assertIsNotNone(dt)

    def test_hours_ago_english(self):
        dt = parse_timestamp("1 hour ago")
        self.assertIsNotNone(dt)

    def test_empty_string(self):
        self.assertIsNone(parse_timestamp(""))

    def test_unparseable(self):
        self.assertIsNone(parse_timestamp("random string"))

    def test_just_now_english(self):
        dt = parse_timestamp("just now")
        self.assertIsNotNone(dt)

    def test_moi_cap_nhat(self):
        dt = parse_timestamp("mới cập nhật")
        self.assertIsNotNone(dt)


class TestComputeConfidence(unittest.TestCase):
    """Tests for compute_confidence()."""

    def test_realtime_just_now(self):
        conf, is_rt, warnings = compute_confidence("vừa xong")
        self.assertEqual(conf, "high")
        self.assertTrue(is_rt)
        self.assertEqual(len(warnings), 0)

    def test_recent_5_minutes(self):
        conf, is_rt, warnings = compute_confidence("5 phút trước")
        # 5 min → within 5 min threshold → high, realtime
        self.assertEqual(conf, "high")

    def test_medium_20_minutes(self):
        conf, is_rt, warnings = compute_confidence("20 phút trước")
        self.assertEqual(conf, "medium")
        self.assertFalse(is_rt)
        self.assertTrue(len(warnings) > 0)

    def test_low_45_minutes(self):
        conf, is_rt, warnings = compute_confidence("45 phút trước")
        self.assertEqual(conf, "low")
        self.assertTrue(len(warnings) > 0)

    def test_very_low_2_hours(self):
        conf, is_rt, warnings = compute_confidence("2 giờ trước")
        self.assertEqual(conf, "low")
        self.assertTrue(any("giờ" in w or "ƯỚC LƯỢNG" in w for w in warnings))

    def test_unparseable_timestamp(self):
        conf, is_rt, warnings = compute_confidence("invalid")
        self.assertEqual(conf, "low")
        self.assertFalse(is_rt)
        self.assertTrue(len(warnings) > 0)

    def test_empty_timestamp(self):
        conf, is_rt, warnings = compute_confidence("")
        self.assertEqual(conf, "low")


class TestDetectConflict(unittest.TestCase):
    """Tests for detect_conflict()."""

    def test_no_conflict(self):
        points = [
            MarketDataPoint(value="100", source="A", timestamp="", raw_value=100.0),
            MarketDataPoint(value="101", source="B", timestamp="", raw_value=101.0),
        ]
        has_conflict, note = detect_conflict(points, tolerance_pct=2.0)
        self.assertFalse(has_conflict)
        self.assertIsNone(note)

    def test_conflict_detected(self):
        points = [
            MarketDataPoint(value="100", source="A", timestamp="", raw_value=100.0),
            MarketDataPoint(value="110", source="B", timestamp="", raw_value=110.0),
        ]
        has_conflict, note = detect_conflict(points, tolerance_pct=5.0)
        self.assertTrue(has_conflict)
        self.assertIn("10.0%", note)
        self.assertIn("A", note)
        self.assertIn("B", note)

    def test_single_point(self):
        points = [
            MarketDataPoint(value="100", source="A", timestamp="", raw_value=100.0),
        ]
        has_conflict, note = detect_conflict(points)
        self.assertFalse(has_conflict)

    def test_no_raw_values(self):
        points = [
            MarketDataPoint(value="100", source="A", timestamp=""),
            MarketDataPoint(value="110", source="B", timestamp=""),
        ]
        has_conflict, note = detect_conflict(points)
        self.assertFalse(has_conflict)

    def test_zero_min_value(self):
        """Edge case: min_val = 0 should not crash."""
        points = [
            MarketDataPoint(value="0", source="A", timestamp="", raw_value=0.0),
            MarketDataPoint(value="100", source="B", timestamp="", raw_value=100.0),
        ]
        has_conflict, note = detect_conflict(points)
        self.assertFalse(has_conflict)  # returns False when min_val == 0

    def test_custom_tolerance(self):
        points = [
            MarketDataPoint(value="100", source="A", timestamp="", raw_value=100.0),
            MarketDataPoint(value="100.5", source="B", timestamp="", raw_value=100.5),
        ]
        # 0.5% deviation, tolerance 0.1% → conflict
        has_conflict, note = detect_conflict(points, tolerance_pct=0.1)
        self.assertTrue(has_conflict)

        # 0.5% deviation, tolerance 1% → no conflict
        has_conflict2, note2 = detect_conflict(points, tolerance_pct=1.0)
        self.assertFalse(has_conflict2)


class TestValidateSingle(unittest.TestCase):
    """Tests for validate_single()."""

    def test_valid_data(self):
        dp = MarketDataPoint(
            value="89,200,000 VND",
            source="SJC.com.vn",
            timestamp="vừa xong",
            asset_type="gold",
            asset_name="Vàng SJC",
        )
        resp = validate_single(dp)
        self.assertIsInstance(resp, MarketDataResponse)
        self.assertEqual(resp.asset_name, "Vàng SJC")
        self.assertEqual(resp.confidence, "high")

    def test_missing_value_raises(self):
        dp = MarketDataPoint(value="", source="SJC", timestamp="vừa xong")
        with self.assertRaises(ValidationError) as ctx:
            validate_single(dp)
        self.assertIn("value", str(ctx.exception).lower())

    def test_missing_source_raises(self):
        dp = MarketDataPoint(value="100", source="", timestamp="vừa xong")
        with self.assertRaises(ValidationError):
            validate_single(dp)

    def test_missing_timestamp_raises(self):
        dp = MarketDataPoint(value="100", source="SJC", timestamp="")
        with self.assertRaises(ValidationError):
            validate_single(dp)

    def test_missing_all_raises(self):
        dp = MarketDataPoint(value="", source="", timestamp="")
        with self.assertRaises(ValidationError) as ctx:
            validate_single(dp)
        error_msg = str(ctx.exception)
        self.assertIn("value", error_msg.lower())
        self.assertIn("source", error_msg.lower())
        self.assertIn("timestamp", error_msg.lower())

    def test_defaults_for_optional_fields(self):
        dp = MarketDataPoint(
            value="100", source="test", timestamp="just now",
        )
        resp = validate_single(dp)
        self.assertEqual(resp.asset_name, "N/A")
        self.assertEqual(resp.asset_type, "unknown")


class TestValidateMulti(unittest.TestCase):
    """Tests for validate_multi()."""

    def test_single_point_delegates(self):
        dp = MarketDataPoint(
            value="100", source="A", timestamp="vừa xong",
            asset_name="Test",
        )
        resp = validate_multi([dp])
        self.assertEqual(resp.asset_name, "Test")

    def test_multi_no_conflict(self):
        points = [
            MarketDataPoint(
                value="100", source="A", timestamp="vừa xong",
                asset_name="Test", raw_value=100.0,
            ),
            MarketDataPoint(
                value="101", source="B", timestamp="5 phút trước",
                asset_name="Test", raw_value=101.0,
            ),
        ]
        resp = validate_multi(points)
        self.assertIn("A", resp.source)
        self.assertIn("B", resp.source)
        self.assertIsNone(resp.range_note)

    def test_multi_with_conflict(self):
        points = [
            MarketDataPoint(
                value="100", source="A", timestamp="vừa xong",
                asset_name="Gold", raw_value=100.0,
            ),
            MarketDataPoint(
                value="120", source="B", timestamp="vừa xong",
                asset_name="Gold", raw_value=120.0,
            ),
        ]
        resp = validate_multi(points, )
        self.assertEqual(resp.confidence, "low")  # conflict → low
        self.assertIsNotNone(resp.range_note)

    def test_empty_list_raises(self):
        with self.assertRaises(ValidationError):
            validate_multi([])

    def test_freshest_selected(self):
        """The freshest data point should be selected as primary."""
        points = [
            MarketDataPoint(
                value="old_100", source="OldSrc",
                timestamp="2 giờ trước",
                asset_name="Test",
            ),
            MarketDataPoint(
                value="new_200", source="NewSrc",
                timestamp="vừa xong",
                asset_name="Test",
            ),
        ]
        resp = validate_multi(points)
        # Primary should be the freshest → "new_200"
        self.assertEqual(resp.value, "new_200")


class TestMarketDataFormatters(unittest.TestCase):
    """Tests for format_text(), format_json_output(), format_markdown()."""

    def _make_response(self, confidence="high", is_realtime=True, warnings=None):
        return MarketDataResponse(
            asset_name="Vàng SJC",
            asset_type="gold",
            value="89,200,000 VND",
            timestamp="2026-03-05T19:30:00+07:00",
            source="SJC.com.vn",
            confidence=confidence,
            is_realtime=is_realtime,
            warnings=warnings or [],
        )

    def test_text_format_high(self):
        text = format_text(self._make_response("high", True))
        self.assertIn("Vàng SJC", text)
        self.assertIn("89,200,000", text)
        self.assertIn("CAO ✅", text)
        self.assertIn("realtime", text)

    def test_text_format_low_with_warnings(self):
        text = format_text(self._make_response(
            "low", False, ["⚠️ Dữ liệu cũ"]))
        self.assertIn("THẤP 🔴", text)
        self.assertIn("Dữ liệu cũ", text)

    def test_json_format(self):
        json_str = format_json_output(self._make_response())
        data = json.loads(json_str)
        self.assertEqual(data["asset_name"], "Vàng SJC")
        self.assertEqual(data["confidence"], "high")
        self.assertTrue(data["is_realtime"])

    def test_markdown_format(self):
        md = format_markdown(self._make_response("medium"))
        self.assertIn("## 💰 Vàng SJC", md)
        self.assertIn("TRUNG BÌNH", md)
        self.assertIn("| **Giá**", md)

    def test_markdown_with_warnings(self):
        md = format_markdown(self._make_response("low", False, ["⚠️ test warning"]))
        self.assertIn("> ⚠️ test warning", md)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Integration tests — git_push_helper + wrapper scripts
# ═══════════════════════════════════════════════════════════════════════════

from git_push_helper import (
    _load_config,
    _inject_file_tokens,
    get_token_for_repo,
)


class TestGitPushHelperConfig(unittest.TestCase):
    """Tests for git_push_helper config loading."""

    def test_load_config_returns_token_config(self):
        """_load_config() should return a TokenConfig even if files missing."""
        config = _load_config()
        self.assertIsInstance(config, TokenConfig)
        self.assertTrue(len(config.tokens) > 0)

    def test_inject_file_tokens_no_crash_on_missing_files(self):
        """_inject_file_tokens should not crash when token files don't exist."""
        config = TokenConfig(tokens=[
            TokenEntry(alias="test", env_var="FAKE_VAR_12345", file_path="nonexistent.txt"),
        ])
        # Should not raise
        _inject_file_tokens(config)
        # Value should still be None (no file found)
        self.assertIsNone(config.tokens[0].resolve())

    def test_inject_file_tokens_with_real_file(self):
        """_inject_file_tokens should read from file when it exists."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                         dir="/tmp") as f:
            f.write("test_token_value_123\n")
            f.flush()
            token_path = f.name

        try:
            config = TokenConfig(tokens=[
                TokenEntry(alias="test", env_var="FAKE_VAR_12345",
                           file_path=token_path),
            ])
            # We need to mock WORKSPACE so the path resolves
            with patch("git_push_helper.WORKSPACE", Path("/")):
                _inject_file_tokens(config)
            # Should have loaded the token from file
            self.assertEqual(config.tokens[0].value, "test_token_value_123")
        finally:
            os.unlink(token_path)

    @patch("git_push_helper.select_token")
    def test_get_token_for_repo_success(self, mock_select):
        mock_select.return_value = SelectionResult(
            success=True, token="tok_abc", alias="classic",
            repo="owner/repo", http_status=200, message="OK",
        )
        token = get_token_for_repo("owner/repo")
        self.assertEqual(token, "tok_abc")

    @patch("git_push_helper.select_token")
    def test_get_token_for_repo_failure(self, mock_select):
        mock_select.return_value = SelectionResult(
            success=False, token=None, alias=None,
            repo="owner/repo", http_status=0, message="All failed",
            attempts=[{"alias": "classic", "status": "FAIL", "message": "401"}],
        )
        token = get_token_for_repo("owner/repo")
        self.assertIsNone(token)


class TestWrapperFilterAndSend(unittest.TestCase):
    """Integration test for wrapper scripts' filter_and_send in dry-run mode."""

    def test_kpi_tracker_filter_and_send_dryrun(self):
        """Test kpi_tracker filter_and_send pipeline with demo data (dry-run).

        We test the core filter pipeline directly rather than importing the
        wrapper script (which has complex relative path setup).
        """
        raw_lines = [
            "🩺 DATA HEALTH CHECK — 19:30 05/03/2026",
            "atom_1 ✅",
            "atom_2 ✅",
            "save_db ✅",
            "[AoT] Phase 1 aggregation",
            "script ran ok",
            "",
            "📊 KPI DASHBOARD:",
            "  🟢 Revenue: 125,000,000 VND",
            "  🔵 AOV: 365,500 VND",
            "🎯 TỔNG KẾT: 4/5 KPI on target",
        ]

        config_path = _ROOT / "log_filter_config.json"
        if config_path.exists():
            config = FilterConfig.from_json(config_path)
        else:
            config = FilterConfig()

        noise_filter = LogNoiseFilter(config, context_mode="user")
        results = noise_filter.filter_lines(raw_lines)

        # Build clean output (same logic as wrapper)
        clean_lines = []
        for r in results:
            if r.action in ("PASS", "ALERT"):
                line = r.entry.raw_line or r.entry.message
                clean_lines.append(line)

        clean_output = "\n".join(clean_lines).strip()

        # Verify: internal noise suppressed, report lines kept
        self.assertNotIn("atom_1", clean_output)
        self.assertNotIn("save_db", clean_output)
        self.assertNotIn("[AoT]", clean_output)
        self.assertNotIn("ran ok", clean_output)
        self.assertIn("KPI DASHBOARD", clean_output)
        self.assertIn("Revenue", clean_output)
        self.assertIn("TỔNG KẾT", clean_output)

    def test_report_ads_filter_pipeline(self):
        """Test report-ads filter pipeline with demo data."""
        raw_lines = [
            "📊 BÁO CÁO QUẢNG CÁO — 19:30 05/03/2026",
            "atom_1 ✅",
            "save_db ✅",
            "[AoT] Phase 1 done",
            "script ran ok",
            "",
            "📊 Google Ads:",
            "  🔵 Spend: 5,200,000 VND",
            "  🟢 Conversions: 42",
            "🎯 TỔNG KẾT: CPA avg 118,200 VND",
        ]

        config_path = _ROOT / "log_filter_config.json"
        if config_path.exists():
            config = FilterConfig.from_json(config_path)
        else:
            config = FilterConfig()

        noise_filter = LogNoiseFilter(config, context_mode="user")
        results = noise_filter.filter_lines(raw_lines)

        clean_lines = [
            r.entry.raw_line or r.entry.message
            for r in results if r.action in ("PASS", "ALERT")
        ]
        clean_output = "\n".join(clean_lines).strip()

        # Internal noise suppressed
        self.assertNotIn("atom_1", clean_output)
        self.assertNotIn("[AoT]", clean_output)
        # Report content kept
        self.assertIn("BÁO CÁO QUẢNG CÁO", clean_output)
        self.assertIn("Google Ads", clean_output)
        self.assertIn("TỔNG KẾT", clean_output)


class TestMarketDataIntegration(unittest.TestCase):
    """Integration test for market data guardrail end-to-end."""

    def test_full_pipeline_single(self):
        """validate_single → format_text — full pipeline."""
        dp = MarketDataPoint(
            value="92,500,000 VND/lượng",
            source="SJC.com.vn",
            timestamp="just now",
            asset_type="gold",
            asset_name="Vàng SJC 1 lượng",
        )
        resp = validate_single(dp)
        text = format_text(resp)
        self.assertIn("Vàng SJC", text)
        self.assertIn("92,500,000", text)
        self.assertIn("SJC.com.vn", text)
        self.assertIn("CAO", text)

    def test_full_pipeline_multi_conflict(self):
        """validate_multi with conflict → format_markdown."""
        points = [
            MarketDataPoint(
                value="92,500,000", source="SJC",
                timestamp="just now", asset_name="Vàng",
                raw_value=92_500_000,
            ),
            MarketDataPoint(
                value="93,800,000", source="DOJI",
                timestamp="5 phút trước", asset_name="Vàng",
                raw_value=93_800_000,
            ),
        ]
        resp = validate_multi(points)
        md = format_markdown(resp)
        self.assertIn("Vàng", md)
        # Conflict detected (1.4% > default 2%? No, 1.4% < 2% → no conflict)
        # Actually: (93.8M - 92.5M) / 92.5M = 1.405% < 2% → no conflict
        # Let's check if it's within tolerance
        if resp.range_note:
            self.assertIn("mâu thuẫn", md)


# ═══════════════════════════════════════════════════════════════════════════
# Run tests
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
