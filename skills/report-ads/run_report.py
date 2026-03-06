#!/usr/bin/env python3
"""
run_report.py — Wrapper that runs the real ads report script and filters output
before sending to Telegram.

This script:
1. Runs the actual report script (via --script or auto-detect)
2. Captures its stdout/stderr
3. Filters output through LogNoiseFilter (suppress internal noise, keep report + alerts)
4. Sends clean output to Telegram

This does NOT replace the real report logic — it wraps it with noise filtering.

Usage:
    python3 run_report.py
    python3 run_report.py --dry-run   # show filtered output without sending
    python3 run_report.py --verbose    # also show suppressed entries
    python3 run_report.py --script /path/to/real_report.py
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
_WORKSPACE = _SCRIPT_DIR.parents[1]  # skills/report-ads → skills → workspace
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from log_noise_filter import (
    LogNoiseFilter,
    FilterConfig,
    format_filtered_text,
    generate_summary,
)

logger = logging.getLogger("report-ads")

# Vietnam timezone
ICT = timezone(timedelta(hours=7))

# Config paths
LOG_FILTER_CONFIG = _WORKSPACE / "log_filter_config.json"

# Default real script location (can be overridden with --script)
# Searches within workspace first, then common legacy paths
DEFAULT_REAL_SCRIPT_CANDIDATES = [
    _WORKSPACE / "report-ads" / "run_report.py",              # workspace/report-ads/
    _WORKSPACE / "skills" / "report-ads" / "run_report_real.py",  # explicit real script
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
# Run real report script
# ---------------------------------------------------------------------------

def run_real_script(
    script_path: Path | None,
    passthrough_args: list[str] | None = None,
    allow_demo: bool = False,
) -> tuple[list[str], int]:
    """Run the actual report script and capture its output.

    Args:
        script_path: Path to the real report script (None = auto-detect).
        passthrough_args: Extra args to pass to the script.
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
                f"❌ Real report script not found. Searched: {candidates_str}. "
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
        f"📊 BÁO CÁO QUẢNG CÁO — {now}",
        "atom_1 ✅",
        "atom_2 ✅",
        "save_db ✅",
        "[AoT] Phase 1 done",
        "[AoT] Phase 2 done",
        "script ran ok",
        "",
        "📊 Google Ads (demo):",
        "  🔵 Spend: 5,200,000 VND",
        "  🟢 Conversions: 42",
        "  📌 CPA: 123,800 VND",
        "",
        "📊 Meta Ads (demo):",
        "  🔵 Spend: 3,100,000 VND",
        "  🟢 Conversions: 28",
        "  📌 CPA: 110,700 VND",
        "",
        "🎯 TỔNG KẾT: CPA avg 118,200 VND (-5.2% vs yesterday)",
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
        raw_lines: Raw output lines from report logic.
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
    # - Lines matching pass_keywords (report body emojis) → PASS
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
    summary = generate_summary(results, period="report run")
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
        description="Run ads report with log noise filtering → send to Telegram",
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
        help="Path to real report script (default: auto-detect)",
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

    logger.info("🚀 Running ads report...")

    # Determine real script path
    script_path = Path(args.script) if args.script else None

    # Step 1: Run real report script (collect raw output)
    # allow_demo: True if --dry-run or --allow-demo (safe for testing)
    allow_demo = args.dry_run or args.allow_demo
    try:
        raw_lines, script_rc = run_real_script(script_path, allow_demo=allow_demo)
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
