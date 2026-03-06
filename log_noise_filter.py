#!/usr/bin/env python3
"""
log_noise_filter.py — Filter internal logs/cron output before showing to user.

Rules:
- SUPPRESS: cron success, heartbeats, "no output" messages
- PASS_THROUGH: errors, exceptions, non-zero exit codes
- ALERT: critical errors, service down, disk full, OOM
- SUMMARY: optional aggregation of suppressed entries

Spec: 2026-03-05-editor-improvements-for-ni (item 4)
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ICT = timezone(timedelta(hours=7))


# Default patterns for each classification
DEFAULT_SUPPRESS_PATTERNS = [
    r"ran\s+(ok|successfully)",
    r"no\s+output",
    r"exit\s+code\s*[:=]?\s*0\b",
    r"heartbeat\s+(ok|ping|alive)",
    r"health\s*check\s*(ok|passed|✅)",
    r"cron\s+completed?\s+(ok|success)",
    r"script\s+finished\s+(ok|successfully)",
    r"all\s+checks?\s+pass",
    r"nothing\s+to\s+(do|report|update)",
    r"0\s+errors?,?\s+0\s+warnings?",
    r"^\s*ok\s*$",
    r"^\s*done\.?\s*$",
]

DEFAULT_ALERT_PATTERNS = [
    r"\bERROR\b",
    r"\bCRITICAL\b",
    r"\bFATAL\b",
    r"\bPANIC\b",
    r"disk\s+(full|space)",
    r"\bOOM\b",
    r"out\s+of\s+memory",
    r"connection\s+(refused|timeout|failed)",
    r"service\s+(down|unavailable|crashed)",
    r"permission\s+denied",
    r"segmentation\s+fault",
    r"kill(ed)?\s+signal",
    r"exit\s+code\s*[:=]?\s*[1-9]",
    r"non-?zero\s+exit",
    r"traceback",
    r"exception",
    r"unhandled\s+error",
]

DEFAULT_PASS_KEYWORDS = [
    "user_query",
    "user_request",
    "explicit",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LogEntry:
    """A single log entry."""
    message: str
    source: str = ""
    severity: str = ""
    timestamp: str = ""
    raw_line: str = ""

    @classmethod
    def from_line(cls, line: str) -> "LogEntry":
        """Parse a log line into a LogEntry.

        Tries to detect common log formats:
        - [TIMESTAMP] [SEVERITY] [SOURCE] message
        - SEVERITY: message
        - Plain text
        """
        line = line.rstrip("\n\r")
        original = line

        # Try pattern: [2026-03-05 19:30:00] [ERROR] [cron] message
        m = re.match(
            r"\[([^\]]+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.*)",
            line,
        )
        if m:
            return cls(
                timestamp=m.group(1).strip(),
                severity=m.group(2).strip().upper(),
                source=m.group(3).strip(),
                message=m.group(4).strip(),
                raw_line=original,
            )

        # Try pattern: 2026-03-05 19:30:00 ERROR [source] message
        m = re.match(
            r"(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}(?::\d{2})?)\s+"
            r"(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\s+"
            r"(?:\[([^\]]*)\])?\s*(.*)",
            line,
            re.IGNORECASE,
        )
        if m:
            return cls(
                timestamp=m.group(1).strip(),
                severity=m.group(2).strip().upper(),
                source=(m.group(3) or "").strip(),
                message=m.group(4).strip(),
                raw_line=original,
            )

        # Try pattern: ERROR: message
        m = re.match(
            r"(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\s*:\s*(.*)",
            line,
            re.IGNORECASE,
        )
        if m:
            return cls(
                severity=m.group(1).strip().upper(),
                message=m.group(2).strip(),
                raw_line=original,
            )

        # Plain text
        return cls(message=line.strip(), raw_line=original)


@dataclass
class FilterResult:
    """Result of filtering a log entry."""
    action: str       # "SUPPRESS", "PASS", "ALERT"
    entry: LogEntry
    reason: str = ""


@dataclass
class FilterConfig:
    """Configuration for log filtering."""
    suppress_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_SUPPRESS_PATTERNS))
    alert_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_ALERT_PATTERNS))
    pass_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_PASS_KEYWORDS))

    @classmethod
    def from_json(cls, config_path: Path) -> "FilterConfig":
        """Load config from JSON file."""
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        except json.JSONDecodeError as e:
            print(f"⚠️  Invalid JSON in {config_path}: {e}", file=sys.stderr)
            return cls()

        return cls(
            suppress_patterns=data.get("suppress_patterns", DEFAULT_SUPPRESS_PATTERNS),
            alert_patterns=data.get("alert_patterns", DEFAULT_ALERT_PATTERNS),
            pass_keywords=data.get("pass_keywords", DEFAULT_PASS_KEYWORDS),
        )


# ---------------------------------------------------------------------------
# Filter engine
# ---------------------------------------------------------------------------

class LogNoiseFilter:
    """Central log filtering engine."""

    def __init__(self, config: FilterConfig | None = None, context_mode: str = "internal"):
        """Initialize filter.

        Args:
            config: Filter configuration.
            context_mode: "internal" (default suppress) or "user" (default pass).
                Per spec: internal logs default to silent unless alert/user-requested.
        """
        self.config = config or FilterConfig()
        self.context_mode = context_mode
        # Pre-compile regex patterns, skipping invalid ones
        self._suppress_re = []
        for p in self.config.suppress_patterns:
            try:
                self._suppress_re.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                print(f"⚠️  Skipping invalid suppress pattern '{p}': {e}", file=sys.stderr)

        self._alert_re = []
        for p in self.config.alert_patterns:
            try:
                self._alert_re.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                print(f"⚠️  Skipping invalid alert pattern '{p}': {e}", file=sys.stderr)

    def classify(self, entry: LogEntry) -> FilterResult:
        """Classify a log entry into SUPPRESS, PASS, or ALERT.

        Priority order:
        1. ALERT patterns (highest priority — never suppress errors)
        2. Pass keywords (explicit user-requested content)
        3. SUPPRESS patterns
        4. Default: context-dependent
           - internal mode → SUPPRESS (per spec: silent by default)
           - user mode → PASS (user explicitly asked for logs)
        """
        text = entry.message
        combined = f"{entry.severity} {entry.source} {text}"

        # 1. Check ALERT patterns first
        for pattern in self._alert_re:
            if pattern.search(combined):
                return FilterResult(
                    action="ALERT",
                    entry=entry,
                    reason=f"Matched alert pattern: {pattern.pattern}",
                )

        # 2. Check pass keywords
        for keyword in self.config.pass_keywords:
            if keyword.lower() in combined.lower():
                return FilterResult(
                    action="PASS",
                    entry=entry,
                    reason=f"Contains pass keyword: {keyword}",
                )

        # 3. Check SUPPRESS patterns
        for pattern in self._suppress_re:
            if pattern.search(combined):
                return FilterResult(
                    action="SUPPRESS",
                    entry=entry,
                    reason=f"Matched suppress pattern: {pattern.pattern}",
                )

        # 4. Default: context-dependent
        if self.context_mode == "internal":
            return FilterResult(
                action="SUPPRESS",
                entry=entry,
                reason="No pattern matched — internal mode default suppress",
            )
        else:
            return FilterResult(
                action="PASS",
                entry=entry,
                reason="No pattern matched — user mode default pass-through",
            )

    def filter_lines(self, lines: list[str]) -> list[FilterResult]:
        """Filter a list of log lines."""
        results = []
        for line in lines:
            if not line.strip():
                continue
            entry = LogEntry.from_line(line)
            result = self.classify(entry)
            results.append(result)
        return results


# ---------------------------------------------------------------------------
# Summary generator
# ---------------------------------------------------------------------------

def generate_summary(results: list[FilterResult], period: str = "recent") -> str:
    """Generate a compact summary of filtered logs.

    Instead of showing each suppressed line, show:
    "📊 Cron summary: 12 tasks ran OK, 0 failures (last 1h)"
    """
    suppressed = [r for r in results if r.action == "SUPPRESS"]
    alerts = [r for r in results if r.action == "ALERT"]
    passed = [r for r in results if r.action == "PASS"]

    lines = []

    if suppressed or alerts or passed:
        lines.append(f"📊 Log summary ({period}):")
        if suppressed:
            lines.append(f"   ✅ {len(suppressed)} normal entries (suppressed)")
        if passed:
            lines.append(f"   📋 {len(passed)} entries passed through")
        if alerts:
            lines.append(f"   🚨 {len(alerts)} alerts require attention")
            for a in alerts:
                lines.append(f"      → {a.entry.message}")

    if not lines:
        lines.append("📊 No log entries to summarize.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_filtered_text(results: list[FilterResult], show_suppressed: bool = False) -> str:
    """Format filtered results as text."""
    lines = []

    alerts = [r for r in results if r.action == "ALERT"]
    passed = [r for r in results if r.action == "PASS"]
    suppressed = [r for r in results if r.action == "SUPPRESS"]

    if alerts:
        lines.append("🚨 ALERTS:")
        for r in alerts:
            lines.append(f"  {r.entry.raw_line or r.entry.message}")
        lines.append("")

    if passed:
        lines.append("📋 LOG OUTPUT:")
        for r in passed:
            lines.append(f"  {r.entry.raw_line or r.entry.message}")
        lines.append("")

    if show_suppressed and suppressed:
        lines.append(f"🔇 SUPPRESSED ({len(suppressed)} entries):")
        for r in suppressed:
            lines.append(f"  [{r.reason}] {r.entry.message}")
        lines.append("")

    if not alerts and not passed:
        lines.append("✅ Không có log đáng chú ý.")

    # Always append summary
    lines.append("")
    lines.append(generate_summary(results))

    return "\n".join(lines)


def format_filtered_json(results: list[FilterResult]) -> str:
    """Format filtered results as JSON."""
    return json.dumps({
        "total": len(results),
        "alerts": len([r for r in results if r.action == "ALERT"]),
        "passed": len([r for r in results if r.action == "PASS"]),
        "suppressed": len([r for r in results if r.action == "SUPPRESS"]),
        "entries": [
            {
                "action": r.action,
                "message": r.entry.message,
                "source": r.entry.source,
                "severity": r.entry.severity,
                "timestamp": r.entry.timestamp,
                "reason": r.reason,
            }
            for r in results
        ],
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Log noise filter — suppress routine logs, surface alerts",
    )
    parser.add_argument(
        "input", nargs="?", type=str, default=None,
        help="Input file path (default: read from stdin)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to log_filter_config.json",
    )
    parser.add_argument(
        "--format", choices=["text", "json", "summary"], default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--show-suppressed", action="store_true",
        help="Also show suppressed entries (for debugging)",
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="Output file path (default: stdout)",
    )

    args = parser.parse_args()

    # Load config
    if args.config:
        config = FilterConfig.from_json(Path(args.config))
    else:
        config = FilterConfig()

    # Read input
    if args.input:
        try:
            with open(args.input, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"❌ Error reading {args.input}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if sys.stdin.isatty():
            print("Reading from stdin (Ctrl+D to end)...", file=sys.stderr)
        lines = sys.stdin.readlines()

    # Filter
    log_filter = LogNoiseFilter(config)
    results = log_filter.filter_lines(lines)

    # Format output
    if args.format == "json":
        output = format_filtered_json(results)
    elif args.format == "summary":
        output = generate_summary(results)
    else:
        output = format_filtered_text(results, show_suppressed=args.show_suppressed)

    # Write output
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"✅ Filtered output written to {out_path.name}")
    else:
        print(output)

    # Exit code: non-zero if any alerts
    alert_count = sum(1 for r in results if r.action == "ALERT")
    if alert_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
