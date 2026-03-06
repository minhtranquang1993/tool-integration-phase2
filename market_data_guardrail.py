#!/usr/bin/env python3
"""
market_data_guardrail.py — Validate and format market data responses.

Ensures every market data response includes:
- Timestamp (when data was fetched/updated)
- Source (where data came from)
- Confidence level (high/medium/low based on freshness)
- Warning if data is stale or sources conflict

Spec: 2026-03-05-editor-improvements-for-ni (item 3)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Staleness thresholds (in minutes)
STALE_THRESHOLD_MEDIUM = 15    # > 15 min → medium confidence
STALE_THRESHOLD_LOW = 30       # > 30 min → low confidence
STALE_THRESHOLD_VERY_LOW = 60  # > 60 min → very low, strong warning

# Vietnam timezone (ICT = UTC+7)
ICT = timezone(timedelta(hours=7))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MarketDataPoint:
    """A single market data point from one source."""
    value: str              # e.g., "89,200,000 VND/lượng"
    source: str             # e.g., "SJC", "VNDIRECT"
    timestamp: str          # ISO 8601 or descriptive
    asset_type: str = ""    # e.g., "gold", "stock", "forex"
    asset_name: str = ""    # e.g., "SJC 1 lượng", "VN-INDEX"
    raw_value: float | None = None  # numeric value for comparison


@dataclass
class MarketDataResponse:
    """Validated and formatted market data response."""
    asset_name: str
    asset_type: str
    value: str
    timestamp: str
    source: str
    confidence: str         # "high", "medium", "low"
    is_realtime: bool
    warnings: list[str] = field(default_factory=list)
    range_note: str | None = None  # when sources conflict


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when required fields are missing."""
    pass


def parse_timestamp(ts_str: str) -> datetime | None:
    """Try to parse a timestamp string into datetime.

    Supports:
    - ISO 8601: "2026-03-05T19:30:00+07:00"
    - Simple datetime: "2026-03-05 19:30"
    - Relative: "5 phút trước", "just now"
    """
    if not ts_str:
        return None

    # Try ISO 8601
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(ts_str, fmt)
            # Normalize: if naive (no timezone), assume ICT
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ICT)
            return dt
        except ValueError:
            continue

    # Relative time patterns (Vietnamese)
    ts_lower = ts_str.lower().strip()
    now = datetime.now(ICT)

    if ts_lower in ("just now", "vừa xong", "mới cập nhật"):
        return now

    # "X phút trước" / "X minutes ago"
    import re
    m = re.match(r"(\d+)\s*(phút|minutes?|min)\s*(trước|ago)?", ts_lower)
    if m:
        minutes = int(m.group(1))
        return now - timedelta(minutes=minutes)

    m = re.match(r"(\d+)\s*(giờ|hours?|hr)\s*(trước|ago)?", ts_lower)
    if m:
        hours = int(m.group(1))
        return now - timedelta(hours=hours)

    return None


def compute_confidence(timestamp_str: str) -> tuple[str, bool, list[str]]:
    """Compute confidence level based on data freshness.

    Returns:
        (confidence_level, is_realtime, warnings)
    """
    warnings = []
    parsed = parse_timestamp(timestamp_str)

    if parsed is None:
        return "low", False, ["⚠️ Không parse được timestamp — mức tin cậy thấp"]

    now = datetime.now(ICT)

    # Make both timezone-aware for comparison
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ICT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ICT)

    age_minutes = (now - parsed).total_seconds() / 60

    if age_minutes < 0:
        # Future timestamp? Likely clock skew
        return "medium", False, ["⚠️ Timestamp trong tương lai — có thể lệch đồng hồ"]

    if age_minutes <= 5:
        return "high", True, []
    elif age_minutes <= STALE_THRESHOLD_MEDIUM:
        return "high", False, []
    elif age_minutes <= STALE_THRESHOLD_LOW:
        return "medium", False, [
            f"⚠️ Dữ liệu cách đây {int(age_minutes)} phút — mức tin cậy trung bình"
        ]
    elif age_minutes <= STALE_THRESHOLD_VERY_LOW:
        return "low", False, [
            f"⚠️ Dữ liệu cách đây {int(age_minutes)} phút — mức tin cậy THẤP, nên xác nhận nguồn chính thức"
        ]
    else:
        hours = age_minutes / 60
        return "low", False, [
            f"🚨 Dữ liệu cách đây {hours:.1f} giờ — ƯỚC LƯỢNG, KHÔNG phải realtime. "
            f"Cần xác nhận nguồn chính thức."
        ]


def detect_conflict(data_points: list[MarketDataPoint], tolerance_pct: float = 2.0) -> tuple[bool, str | None]:
    """Detect if multiple sources report conflicting values.

    Args:
        data_points: List of data points with raw_value
        tolerance_pct: Acceptable deviation percentage

    Returns:
        (has_conflict, range_note)
    """
    values = [dp.raw_value for dp in data_points if dp.raw_value is not None]
    if len(values) < 2:
        return False, None

    min_val = min(values)
    max_val = max(values)

    if min_val == 0:
        return False, None

    deviation_pct = ((max_val - min_val) / min_val) * 100

    if deviation_pct > tolerance_pct:
        sources = [dp.source for dp in data_points if dp.raw_value is not None]
        return True, (
            f"📊 Nguồn mâu thuẫn ({deviation_pct:.1f}% chênh lệch): "
            f"{', '.join(sources)} → "
            f"Range: {min_val:,.0f} – {max_val:,.0f}. "
            f"Nên xác nhận nguồn chính thức."
        )

    return False, None


def validate_single(data_point: MarketDataPoint) -> MarketDataResponse:
    """Validate a single data point and produce a formatted response.

    Raises ValidationError if required fields are missing.
    """
    errors = []
    if not data_point.value:
        errors.append("Thiếu giá trị (value)")
    if not data_point.source:
        errors.append("Thiếu nguồn (source)")
    if not data_point.timestamp:
        errors.append("Thiếu thời điểm cập nhật (timestamp)")

    if errors:
        raise ValidationError(
            f"❌ REJECT — Không đủ thông tin: {'; '.join(errors)}. "
            f"Không trả số 'ảo' không timestamp/source."
        )

    confidence, is_realtime, warnings = compute_confidence(data_point.timestamp)

    return MarketDataResponse(
        asset_name=data_point.asset_name or "N/A",
        asset_type=data_point.asset_type or "unknown",
        value=data_point.value,
        timestamp=data_point.timestamp,
        source=data_point.source,
        confidence=confidence,
        is_realtime=is_realtime,
        warnings=warnings,
    )


def validate_multi(data_points: list[MarketDataPoint]) -> MarketDataResponse:
    """Validate multiple data points (multi-source), detect conflicts.

    Picks the freshest data point as primary (by parsed timestamp).
    """
    if not data_points:
        raise ValidationError("❌ REJECT — Không có dữ liệu")

    if len(data_points) == 1:
        return validate_single(data_points[0])

    # Validate each point and pair with parsed timestamp for sorting
    pairs: list[tuple[MarketDataResponse, datetime | None, MarketDataPoint]] = []
    for dp in data_points:
        resp = validate_single(dp)
        parsed_ts = parse_timestamp(dp.timestamp)
        pairs.append((resp, parsed_ts, dp))

    # Sort by timestamp descending — freshest first
    # Data points without parseable timestamps go last
    epoch = datetime(1970, 1, 1, tzinfo=ICT)
    pairs.sort(key=lambda t: t[1] if t[1] is not None else epoch, reverse=True)

    # Use the freshest data point as primary
    primary = pairs[0][0]
    all_warnings = []
    for resp, _, _ in pairs:
        all_warnings.extend(resp.warnings)

    # Check for conflicts
    has_conflict, range_note = detect_conflict(data_points)
    if has_conflict:
        all_warnings.append(range_note)
        primary.confidence = "low"

    primary.warnings = all_warnings
    primary.range_note = range_note
    sources = [dp.source for dp in data_points]
    primary.source = " + ".join(sources)

    return primary


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_text(response: MarketDataResponse) -> str:
    """Format as text (Telegram-friendly)."""
    confidence_map = {
        "high": "CAO ✅",
        "medium": "TRUNG BÌNH ⚠️",
        "low": "THẤP 🔴",
    }
    confidence_label = confidence_map.get(response.confidence, response.confidence)
    realtime_note = " (realtime)" if response.is_realtime else ""

    lines = []
    lines.append(f"💰 {response.asset_name}: {response.value}")
    lines.append(f"🕐 Cập nhật: {response.timestamp}")
    lines.append(f"📡 Nguồn: {response.source}")
    lines.append(f"🎯 Độ tin cậy: {confidence_label}{realtime_note}")

    if response.warnings:
        lines.append("")
        for w in response.warnings:
            lines.append(w)

    if response.range_note and response.range_note not in response.warnings:
        lines.append("")
        lines.append(response.range_note)

    return "\n".join(lines)


def format_json_output(response: MarketDataResponse) -> str:
    """Format as JSON."""
    return json.dumps(asdict(response), indent=2, ensure_ascii=False)


def format_markdown(response: MarketDataResponse) -> str:
    """Format as Markdown."""
    confidence_map = {
        "high": "🟢 CAO",
        "medium": "🟡 TRUNG BÌNH",
        "low": "🔴 THẤP",
    }
    confidence_label = confidence_map.get(response.confidence, response.confidence)

    lines = []
    lines.append(f"## 💰 {response.asset_name}")
    lines.append("")
    lines.append(f"| Thuộc tính | Giá trị |")
    lines.append(f"|---|---|")
    lines.append(f"| **Giá** | {response.value} |")
    lines.append(f"| **Cập nhật** | {response.timestamp} |")
    lines.append(f"| **Nguồn** | {response.source} |")
    lines.append(f"| **Độ tin cậy** | {confidence_label} |")
    lines.append(f"| **Realtime** | {'✅' if response.is_realtime else '❌'} |")

    if response.warnings:
        lines.append("")
        for w in response.warnings:
            lines.append(f"> {w}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Market data guardrail — validate and format market data with timestamp/source",
    )
    parser.add_argument(
        "--value", type=str, default=None,
        help="Market data value (e.g., '89,200,000 VND/lượng')",
    )
    parser.add_argument(
        "--source", type=str, default=None,
        help="Data source (e.g., 'SJC', 'VNDIRECT')",
    )
    parser.add_argument(
        "--timestamp", type=str, default=None,
        help="Data timestamp (ISO 8601 or relative like '5 phút trước')",
    )
    parser.add_argument(
        "--asset-name", type=str, default="",
        help="Asset name (e.g., 'Giá vàng SJC')",
    )
    parser.add_argument(
        "--asset-type", type=str, default="",
        help="Asset type: gold, stock, forex, crypto",
    )
    parser.add_argument(
        "--from-json", type=str, default=None,
        help="Load data from JSON file (single or array of data points)",
    )
    parser.add_argument(
        "--format", choices=["text", "json", "markdown"], default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args()

    # Build data points
    if args.from_json:
        try:
            with open(args.from_json, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"❌ Error reading JSON: {e}", file=sys.stderr)
            sys.exit(1)

        if isinstance(data, list):
            data_points = [
                MarketDataPoint(
                    value=d.get("value", ""),
                    source=d.get("source", ""),
                    timestamp=d.get("timestamp", ""),
                    asset_type=d.get("asset_type", ""),
                    asset_name=d.get("asset_name", ""),
                    raw_value=d.get("raw_value"),
                )
                for d in data
            ]
        else:
            data_points = [MarketDataPoint(
                value=data.get("value", ""),
                source=data.get("source", ""),
                timestamp=data.get("timestamp", ""),
                asset_type=data.get("asset_type", ""),
                asset_name=data.get("asset_name", ""),
                raw_value=data.get("raw_value"),
            )]
    elif args.value:
        data_points = [MarketDataPoint(
            value=args.value,
            source=args.source or "",
            timestamp=args.timestamp or "",
            asset_type=args.asset_type,
            asset_name=args.asset_name,
        )]
    else:
        print("❌ Specify --value or --from-json", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    # Validate
    try:
        if len(data_points) == 1:
            response = validate_single(data_points[0])
        else:
            response = validate_multi(data_points)
    except ValidationError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Format output
    formatters = {
        "text": format_text,
        "json": format_json_output,
        "markdown": format_markdown,
    }
    print(formatters[args.format](response))


if __name__ == "__main__":
    main()
