#!/usr/bin/env python3
"""
kpi_tracker.py — Wrapper that runs the real KPI pipeline and filters output
before sending to Telegram.

This script:
1. Runs the actual KPI tracker script (kpi-tracker/scripts/kpi_tracker.py at repo root)
2. Captures its stdout/stderr
3. Filters output through LogNoiseFilter (suppress internal noise, keep report + alerts)
4. Sends clean output to Telegram

This does NOT replace the real KPI logic — it wraps it with noise filtering.

Usage:
    python3 kpi_tracker.py
    python3 kpi_tracker.py --dry-run
    python3 kpi_tracker.py --verbose
    python3 kpi_tracker.py --script /path/to/real_kpi_tracker.py
    python3 kpi_tracker.py --date 2026-03-15  # passthrough args to real script
"""

import argparse
import logging
import os
import subprocess
import sys
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add workspace root to path so we can import log_noise_filter
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE = _SCRIPT_DIR.parents[2]  # skills/kpi-tracker/scripts → kpi-tracker → skills → workspace
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from log_noise_filter import (
    LogNoiseFilter,
    FilterConfig,
    format_filtered_text,
    generate_summary,
)

logger = logging.getLogger("kpi-tracker")

# Vietnam timezone
ICT = timezone(timedelta(hours=7))

# Config paths
LOG_FILTER_CONFIG = _WORKSPACE / "log_filter_config.json"

# Default real script location (searches within workspace)
# Can be overridden with --script flag
DEFAULT_REAL_SCRIPT_CANDIDATES = [
    _WORKSPACE / "kpi-tracker" / "scripts" / "kpi_tracker.py",              # workspace/kpi-tracker/
    _WORKSPACE / "skills" / "kpi-tracker" / "scripts" / "kpi_tracker_real.py",  # explicit real script
]


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

def send_telegram(text: str, bot_token: str | None = None, chat_id: str | None = None) -> bool:
    """Send a message to Telegram (plain text, no parse_mode to avoid escaping issues).

    Args:
        text: Message text (plain text with Unicode emoji).
        bot_token: Telegram bot token (default: env TELEGRAM_BOT_TOKEN).
        chat_id: Telegram chat ID (default: env TELEGRAM_CHAT_ID).

    Returns:
        True if sent successfully.
    """
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skip send")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.getcode() == 200:
                logger.info("✅ Telegram message sent")
                return True
            logger.warning("⚠️ Telegram API returned %s", resp.getcode())
            return False
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error("❌ Telegram send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Run real KPI script
# ---------------------------------------------------------------------------

def run_real_script(
    script_path: Path | None,
    passthrough_args: list[str] | None = None,
    allow_demo: bool = False,
) -> tuple[list[str], int]:
    """Run the actual KPI tracker script and capture its output.

    Args:
        script_path: Path to the real KPI tracker script (None = auto-detect).
        passthrough_args: Extra args to pass to the script (e.g. --date).
        allow_demo: If True, fall back to demo data when real script not found.
                    If False, raise an error instead.

    Returns:
        Tuple of (output_lines, returncode). returncode=0 for demo data.

    Raises:
        FileNotFoundError: When real script not found and allow_demo is False.
    """
    # Auto-detect if no explicit path
    if script_path is None:
        for candidate in DEFAULT_REAL_SCRIPT_CANDIDATES:
            if candidate.exists():
                script_path = candidate
                break

    if script_path is None or not script_path.exists():
        if allow_demo:
            logger.warning("Real script not found — using fallback demo data")
            return _fallback_demo_output(), 0
        else:
            candidates_str = ", ".join(str(c) for c in DEFAULT_REAL_SCRIPT_CANDIDATES)
            raise FileNotFoundError(
                f"❌ Real KPI script not found. Searched: {candidates_str}. "
                f"Use --script to specify path, or --allow-demo for testing."
            )

    cmd = [sys.executable, str(script_path)]
    if passthrough_args:
        cmd.extend(passthrough_args)

    logger.info("Running real script: %s", script_path.name)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(script_path.parent),
        )
        lines = []
        if result.stdout:
            lines.extend(result.stdout.splitlines())
        if result.stderr:
            lines.extend(result.stderr.splitlines())

        if result.returncode != 0:
            lines.append(f"⚠️ Script exited with code {result.returncode}")

        return lines, result.returncode

    except subprocess.TimeoutExpired:
        return [f"❌ Script timed out after 120s: {script_path.name}"], 1
    except Exception as e:
        return [f"❌ Failed to run script: {e}"], 1


def _fallback_demo_output() -> list[str]:
    """Fallback demo output when real script is not available.

    This allows dry-run testing of the filter pipeline without the real script.
    """
    now = datetime.now(ICT).strftime("%H:%M %d/%m/%Y")
    return [
        f"🩺 DATA HEALTH CHECK — {now}",
        "atom_1 ✅",
        "atom_2 ✅",
        "atom_3 ✅",
        "save_db ✅",
        "[AoT] Phase 1 aggregation",
        "[AoT] Phase 2 comparison",
        "[AoT] Phase 3 scoring",
        "script ran ok",
        "",
        "📊 KPI DASHBOARD (demo):",
        "  🟢 Revenue: 125,000,000 VND (+8.2% vs target)",
        "  🟢 Orders: 342 (+5.1%)",
        "  🔵 AOV: 365,500 VND",
        "  🔵 Conversion Rate: 3.2%",
        "  📌 CAC: 89,000 VND (-12% MoM)",
        "",
        "🎯 TỔNG KẾT: 4/5 KPI on target, 1 cần theo dõi",
        "  ▸ Churn rate: 4.1% (target < 3.5%) — cần action",
    ]


# ---------------------------------------------------------------------------
# Filter + send pipeline
# ---------------------------------------------------------------------------

def filter_and_send(
    raw_lines: list[str],
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Filter raw output through LogNoiseFilter, then send clean result to Telegram.

    Args:
        raw_lines: Raw output lines from KPI logic.
        dry_run: If True, print output but don't send to Telegram.
        verbose: If True, also show suppressed entries.

    Returns:
        True if successful (or dry run).
    """
    # Load filter config
    if LOG_FILTER_CONFIG.exists():
        config = FilterConfig.from_json(LOG_FILTER_CONFIG)
        logger.info("Loaded filter config from %s", LOG_FILTER_CONFIG.name)
    else:
        config = FilterConfig()
        logger.warning("Filter config not found, using defaults")

    # Create filter in "user" context mode:
    # - Lines matching pass_keywords (KPI emojis) → PASS
    # - Lines matching alert_patterns → ALERT (always shown)
    # - Lines matching suppress_patterns → SUPPRESS (hidden from Telegram)
    # - Unmatched lines → PASS (safe default for user-facing output)
    noise_filter = LogNoiseFilter(config, context_mode="user")

    results = noise_filter.filter_lines(raw_lines)

    # Build clean output: only PASS + ALERT lines
    clean_lines = []
    for r in results:
        if r.action in ("PASS", "ALERT"):
            line = r.entry.raw_line or r.entry.message
            if r.action == "ALERT":
                line = f"🚨 {line}" if not line.startswith("🚨") else line
            clean_lines.append(line)

    clean_output = "\n".join(clean_lines).strip()

    # Append summary
    summary = generate_summary(results, period="KPI check")
    if summary:
        clean_output += f"\n\n{summary}"

    if verbose:
        # Show full debug view
        debug_output = format_filtered_text(results, show_suppressed=True)
        print("=== DEBUG VIEW ===")
        print(debug_output)
        print("=== END DEBUG ===\n")

    print(clean_output)

    if dry_run:
        logger.info("🔍 Dry run — not sending to Telegram")
        return True

    return send_telegram(clean_output)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Track KPIs with log noise filtering → send alerts to Telegram",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print filtered output but don't send to Telegram",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show suppressed entries for debugging",
    )
    parser.add_argument(
        "--script", type=str, default=None,
        help="Path to real KPI tracker script (default: auto-detect)",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Passthrough --date to real KPI script (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--allow-demo", action="store_true",
        help="Allow fallback to demo data if real script not found (for testing only)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("🚀 Running KPI tracker...")

    # Determine real script path
    script_path = Path(args.script) if args.script else None

    # Build passthrough args for the real script
    passthrough_args = []
    if args.date:
        passthrough_args.extend(["--date", args.date])

    # Step 1: Run real KPI script (collect raw output)
    # allow_demo: True if --dry-run or --allow-demo (safe for testing)
    allow_demo = args.dry_run or args.allow_demo
    try:
        raw_lines, script_rc = run_real_script(script_path, passthrough_args or None, allow_demo=allow_demo)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    logger.info("Collected %d raw output lines (script exit code: %d)", len(raw_lines), script_rc)

    # Step 2: Filter and send
    success = filter_and_send(
        raw_lines,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    # Fail if real script failed OR filter/send failed
    if script_rc != 0:
        logger.warning("⚠️ Real script exited with code %d — marking wrapper as failed", script_rc)
    sys.exit(0 if (success and script_rc == 0) else 1)


if __name__ == "__main__":
    main()
