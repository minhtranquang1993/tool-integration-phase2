#!/usr/bin/env python3
"""
market_lookup.py — Validate and format market data using MarketDataGuardrail.

Usage:
    python3 market_lookup.py --asset "Vàng SJC" --value "92,500,000" \
        --source "SJC.com.vn" --timestamp "2026-03-05T19:30:00+07:00"

    # Missing source/timestamp → auto-warning
    python3 market_lookup.py --asset "Bitcoin" --value "$67,000"
"""

import argparse
import sys
from pathlib import Path

# Add workspace root to path so we can import market_data_guardrail
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE = _SCRIPT_DIR.parents[2]  # skills/market-data/scripts → market-data → skills (workspace root)
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from market_data_guardrail import (
    MarketDataPoint,
    format_text,
    validate_single,
    ValidationError,
)


def main():
    parser = argparse.ArgumentParser(
        description="Validate and format market data responses"
    )
    parser.add_argument("--asset", required=True, help="Asset name (e.g. 'Vàng SJC')")
    parser.add_argument("--value", required=True, help="Current value/price")
    parser.add_argument("--source", default="", help="Data source (e.g. 'SJC.com.vn')")
    parser.add_argument("--timestamp", default="", help="ISO timestamp or relative")
    parser.add_argument("--type", default="", dest="asset_type",
                        help="Asset type (gold, forex, stock, crypto)")
    parser.add_argument("--format", choices=["text", "json", "markdown"],
                        default="text", dest="output_format",
                        help="Output format")

    args = parser.parse_args()

    # Build data point
    data_point = MarketDataPoint(
        value=args.value,
        source=args.source,
        timestamp=args.timestamp,
        asset_type=args.asset_type,
        asset_name=args.asset,
    )

    try:
        response = validate_single(data_point)
    except ValidationError as e:
        # Missing required fields → add warning and continue with defaults
        print(f"⚠️ Không có nguồn/thời gian xác nhận", file=sys.stderr)
        # Create response manually with warnings
        from market_data_guardrail import MarketDataResponse
        response = MarketDataResponse(
            asset_name=args.asset,
            asset_type=args.asset_type or "unknown",
            value=args.value,
            timestamp=args.timestamp or "N/A",
            source=args.source or "N/A",
            confidence="⚠️ Không xác nhận",
            is_realtime=False,
            warnings=["⚠️ Không có nguồn/thời gian xác nhận — dữ liệu chưa được validate"],
        )

    # Format output
    if args.output_format == "json":
        from market_data_guardrail import format_json_output
        print(format_json_output(response))
    elif args.output_format == "markdown":
        from market_data_guardrail import format_markdown
        print(format_markdown(response))
    else:
        print(format_text(response))


if __name__ == "__main__":
    main()
