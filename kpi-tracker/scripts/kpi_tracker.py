#!/usr/bin/env python3
"""
KPI Tracker — theo dõi leads + inbox Messenger so với KPI tháng.
Gửi báo cáo qua Telegram. Chạy cron 9h30 UTC (16h30 ICT) hàng ngày.

Usage:
    python3 kpi_tracker.py
    python3 kpi_tracker.py --date 2026-03-15   # override "hôm qua" = end_date
"""

import argparse
import calendar
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Paths (relative to repo root, hoặc có thể override bằng env var)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # skills/kpi-tracker/
WORKSPACE_DIR = os.path.dirname(os.path.dirname(BASE_DIR))              # repo root

CONFIG_PATH = os.path.join(BASE_DIR, "config", "kpi_config.json")

MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")
RUN_LOG_PATH = os.path.join(MEMORY_DIR, "kpi-tracker-run.log")
ERR_LOG_PATH = os.path.join(MEMORY_DIR, "kpi-tracker-errors.log")

CRED_DIR = os.path.join(WORKSPACE_DIR, "credentials")

# ---------------------------------------------------------------------------
# Credentials helpers
# ---------------------------------------------------------------------------

def _read_cred(filename: str) -> str:
    """Đọc credential từ file, strip whitespace."""
    path = os.path.join(CRED_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Credential file không tìm thấy: {path}")
    with open(path) as f:
        return f.read().strip()


def get_supabase_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY") or _read_cred("supabase_key.txt")


def get_fb_token() -> str:
    return os.environ.get("FB_ACCESS_TOKEN") or _read_cred("fb_token.txt")


def get_telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or _read_cred("telegram_token.txt")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def log_run(msg: str):
    _ensure_dir(RUN_LOG_PATH)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    with open(RUN_LOG_PATH, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def log_error(msg: str):
    _ensure_dir(ERR_LOG_PATH)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    with open(ERR_LOG_PATH, "a") as f:
        f.write(f"[{ts}] {msg}\n")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_CHAT_ID = "1661694132"


def send_telegram(text: str) -> bool:
    """Gửi message Telegram, retry 1 lần nếu fail. Return True nếu thành công."""
    try:
        token = get_telegram_token()
    except Exception as e:
        log_error(f"Không lấy được Telegram token: {e}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            log_error(f"Telegram trả về {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log_error(f"Telegram request lỗi (attempt {attempt + 1}): {e}")
        if attempt == 0:
            time.sleep(5)

    return False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def check_config_month(config: dict, today: date) -> bool:
    """Trả về True nếu config đúng tháng hiện tại."""
    current_month = today.strftime("%Y-%m")
    return config.get("month") == current_month


# ---------------------------------------------------------------------------
# Date helpers (UTC+7)
# ---------------------------------------------------------------------------
ICT = timezone(timedelta(hours=7))


def today_ict() -> date:
    """Trả về ngày hôm nay theo ICT (UTC+7)."""
    return datetime.now(ICT).date()


def build_date_range(config: dict, today: date, override_end: date | None = None):
    """
    Returns (start_date, end_date) dạng date.
    - end_date: override_end nếu được truyền (--date), ngược lại = hôm qua (today - 1).
    - start_date: ngày 1 của tháng trong config.
    """
    year, month = map(int, config["month"].split("-"))
    start_date = date(year, month, 1)
    end_date = override_end if override_end is not None else today - timedelta(days=1)
    return start_date, end_date


def pace(target: int, days_passed: int, total_days: int) -> int:
    return round(target * days_passed / total_days)


# ---------------------------------------------------------------------------
# Supabase — Leads
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://lprtokohgnbpdqkrymje.supabase.co"
TABLE_NAME = "status_data"
PLATFORM_FIELD = "platform_ads"
PLATFORM_VALUES = {
    "fb": "facebook",
    "tiktok": "tiktok",
    "google": "google",
}


def _supabase_headers() -> dict:
    key = get_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def verify_schema() -> dict:
    """
    Health-check schema runtime: lấy 1 row để xác nhận PLATFORM_FIELD và field cost tồn tại.
    Trả về {"platform_ok": bool, "cost_ok": bool, "error": str | None}.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    headers = _supabase_headers()
    headers["Range"] = "0-0"

    try:
        resp = requests.get(f"{url}?select=*", headers=headers, timeout=10)
        if resp.status_code not in (200, 206):
            return {"platform_ok": False, "cost_ok": False, "error": f"HTTP {resp.status_code}"}
        rows = resp.json()
        if not rows:
            # Bảng rỗng, không verify được — coi là OK để không block
            return {"platform_ok": True, "cost_ok": False, "error": None}
        sample = rows[0]
        platform_ok = PLATFORM_FIELD in sample
        cost_ok = "cost" in sample
        if not platform_ok:
            log_error(
                f"Schema mismatch: field '{PLATFORM_FIELD}' không tồn tại trong {TABLE_NAME}. "
                f"Fields hiện có: {list(sample.keys())}"
            )
        return {"platform_ok": platform_ok, "cost_ok": cost_ok, "error": None}
    except Exception as e:
        return {"platform_ok": False, "cost_ok": False, "error": str(e)}


def _supabase_count(
    start_dt: str, end_dt: str, platform_value: str | None = None
) -> int:
    """
    Đếm rows trong status_data theo khoảng thời gian (UTC+7) và platform tuỳ chọn.
    Dùng REST API với header Prefer: count=exact.
    Timestamp phải percent-encode dấu + để tránh bị decode thành space trong query string.
    """
    from urllib.parse import quote

    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    # percent-encode toàn bộ giá trị timestamp để + không bị diễn giải thành space
    qs_parts = [
        f"created_at=gte.{quote(start_dt, safe='')}",
        f"created_at=lte.{quote(end_dt, safe='')}",
    ]
    if platform_value:
        qs_parts.append(f"{PLATFORM_FIELD}=eq.{platform_value}")

    qs = "&".join(qs_parts)
    headers = _supabase_headers()
    headers["Prefer"] = "count=exact"
    headers["Range-Unit"] = "items"
    headers["Range"] = "0-0"  # chỉ lấy 1 row, đếm qua Content-Range

    resp = requests.get(f"{url}?{qs}", headers=headers, timeout=15)
    resp.raise_for_status()

    # Content-Range: 0-0/TOTAL hoặc */TOTAL
    content_range = resp.headers.get("Content-Range", "")
    if "/" in content_range:
        total_str = content_range.split("/")[-1]
        if total_str.isdigit():
            return int(total_str)
    return 0


def _supabase_sum_cost(start_dt: str, end_dt: str) -> float | None:
    """
    Lấy tổng field `cost` dùng Supabase aggregate (select=cost.sum()).
    Trả về None nếu field không tồn tại hoặc không có dữ liệu.
    Không cần pagination vì aggregate trả về 1 row kết quả.
    """
    from urllib.parse import quote

    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    qs = (
        f"created_at=gte.{quote(start_dt, safe='')}"
        f"&created_at=lte.{quote(end_dt, safe='')}"
        f"&select=cost.sum()"
    )
    headers = _supabase_headers()

    resp = requests.get(f"{url}?{qs}", headers=headers, timeout=15)
    if resp.status_code == 400:
        # Field không tồn tại → bỏ qua CPL
        return None
    resp.raise_for_status()

    rows = resp.json()
    if not rows or not isinstance(rows, list):
        return None

    # Supabase trả về [{"sum": value}] với aggregate
    val = rows[0].get("sum")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_leads(start_date: date, end_date: date) -> dict:
    """
    Trả về dict:
    {
        "fb": int, "tiktok": int, "google": int,
        "total": int,
        "cost": float | None,
        "error": str | None
    }
    """
    # Format datetime với timezone UTC+7
    start_dt = f"{start_date}T00:00:00+07:00"
    end_dt = f"{end_date}T23:59:59+07:00"

    try:
        # Verify schema trước khi query chính
        schema = verify_schema()
        if schema["error"]:
            return {
                "fb": 0, "tiktok": 0, "google": 0,
                "total": 0, "cost": None,
                "error": f"Schema check lỗi: {schema['error']}",
            }
        if not schema["platform_ok"]:
            return {
                "fb": 0, "tiktok": 0, "google": 0,
                "total": 0, "cost": None,
                "error": f"Field '{PLATFORM_FIELD}' không tồn tại trong bảng {TABLE_NAME} — cần cập nhật PLATFORM_FIELD",
            }

        fb = _supabase_count(start_dt, end_dt, PLATFORM_VALUES["fb"])
        tiktok = _supabase_count(start_dt, end_dt, PLATFORM_VALUES["tiktok"])
        google = _supabase_count(start_dt, end_dt, PLATFORM_VALUES["google"])
        total = fb + tiktok + google
        cost = _supabase_sum_cost(start_dt, end_dt) if schema["cost_ok"] else None
        return {
            "fb": fb, "tiktok": tiktok, "google": google,
            "total": total, "cost": cost, "error": None,
        }
    except Exception as e:
        return {
            "fb": 0, "tiktok": 0, "google": 0,
            "total": 0, "cost": None, "error": str(e),
        }


# ---------------------------------------------------------------------------
# Facebook API — Inbox Messenger
# ---------------------------------------------------------------------------
FB_AD_ACCOUNT = "act_1465106504558065"
FB_API_VERSION = "v21.0"


def fetch_inbox(start_date: date, end_date: date) -> dict:
    """
    Trả về dict: {"inbox": int | str, "error": str | None}
    """
    try:
        access_token = get_fb_token()
    except Exception as e:
        return {"inbox": "N/A (API error)", "error": f"Không lấy được FB token: {e}"}

    url = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_AD_ACCOUNT}/insights"
    params = {
        "fields": "actions",
        "time_range": json.dumps({
            "since": str(start_date),
            "until": str(end_date),
        }),
        "action_type": "onsite_conversion.messaging_conversation_started_7d",
        "access_token": access_token,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
            return {"inbox": "N/A (API error)", "error": f"FB API {resp.status_code}: {err}"}

        data = resp.json().get("data", [])
        if not data:
            return {"inbox": 0, "error": None}

        actions = data[0].get("actions", [])
        for action in actions:
            if action.get("action_type") == "onsite_conversion.messaging_conversation_started_7d":
                return {"inbox": int(action.get("value", 0)), "error": None}

        return {"inbox": 0, "error": None}

    except Exception as e:
        return {"inbox": "N/A (API error)", "error": f"FB API exception: {e}"}


# ---------------------------------------------------------------------------
# Build Telegram message
# ---------------------------------------------------------------------------

def build_message(
    config: dict,
    start_date: date,
    end_date: date,
    leads_data: dict,
    inbox_data: dict,
) -> str:
    year, month_num = map(int, config["month"].split("-"))
    total_days = calendar.monthrange(year, month_num)[1]
    days_passed = (end_date - start_date).days + 1

    leads_target = config["leads_target"]
    inbox_target = config["inbox_target"]

    pace_leads = pace(leads_target, days_passed, total_days)
    pace_inbox = pace(inbox_target, days_passed, total_days)

    total_leads = leads_data["total"]
    fb = leads_data["fb"]
    tiktok = leads_data["tiktok"]
    google = leads_data["google"]
    cost = leads_data["cost"]

    # CPL
    if leads_data["error"]:
        cpl_str = "N/A"
    elif cost is None:
        cpl_str = "N/A"
    elif total_leads == 0:
        cpl_str = "N/A"
    else:
        cpl_val = cost / total_leads
        cpl_str = f"{cpl_val:,.0f}"

    # Inbox
    inbox_val = inbox_data["inbox"]
    if isinstance(inbox_val, int):
        gap_inbox = inbox_val - pace_inbox
        icon_inbox = "✅" if gap_inbox >= 0 else "⚠️"
        sign_inbox = "+" if gap_inbox >= 0 else "-"
        inbox_line = f"  Tổng: {inbox_val} / Pace: {pace_inbox}\n  {icon_inbox} {sign_inbox}{abs(gap_inbox)} so với tiến độ"
    else:
        inbox_line = f"  Tổng: {inbox_val} / Pace: {pace_inbox}"

    # Leads gap
    if leads_data["error"]:
        gap_leads = 0
        icon_leads = "⚠️"
        leads_gap_line = f"  ⚠️ Lỗi: {leads_data['error'][:100]}"
    else:
        gap_leads = total_leads - pace_leads
        icon_leads = "✅" if gap_leads >= 0 else "⚠️"
        sign_leads = "+" if gap_leads >= 0 else "-"
        leads_gap_line = f"  {icon_leads} {sign_leads}{abs(gap_leads)} leads so với tiến độ"

    # Date format
    dd_start = f"01/{month_num:02d}"
    dd_end = end_date.strftime("%d/%m")
    mm = f"{month_num:02d}"

    msg = (
        f"📊 KPI TRACKER — {dd_start} → {dd_end}\n"
        f"\n"
        f"🎯 LEADS ({days_passed} ngày)\n"
        f"  FB: {fb} | TikTok: {tiktok} | Google: {google}\n"
        f"  Tổng: {total_leads} / Pace: {pace_leads}\n"
        f"{leads_gap_line}\n"
        f"\n"
        f"📩 INBOX MESS\n"
        f"{inbox_line}\n"
        f"\n"
        f"💰 CPL thực tế: {cpl_str}đ\n"
        f"\n"
        f"🗓 KPI tháng {mm}/{year}: {leads_target} leads | {inbox_target} mess"
    )

    # Append errors nếu có
    errors = []
    if leads_data.get("error"):
        errors.append(f"Supabase: {leads_data['error'][:150]}")
    if inbox_data.get("error"):
        errors.append(f"FB API: {inbox_data['error'][:150]}")
    if errors:
        msg += "\n\n⚠️ Lỗi:\n" + "\n".join(f"• {e}" for e in errors)

    return msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="KPI Tracker")
    parser.add_argument(
        "--date",
        help="Override end_date = 'hôm qua' (YYYY-MM-DD), dùng để test on-demand",
    )
    args = parser.parse_args()

    today = today_ict()
    override_end: date | None = None
    if args.date:
        try:
            override_end = date.fromisoformat(args.date)
        except ValueError as e:
            msg = f"⚠️ KPI Tracker lỗi: --date không hợp lệ '{args.date}' — {e}"
            log_error(msg)
            send_telegram(msg)
            sys.exit(1)

    # Load config
    try:
        config = load_config()
    except Exception as e:
        msg = f"⚠️ KPI Tracker lỗi: không đọc được config — {e}"
        send_telegram(msg)
        log_error(f"load_config failed: {e}")
        sys.exit(1)

    # Validate tháng config dùng ngày thực tế (today), không dùng override_end
    if not check_config_month(config, today):
        current_month = today.strftime("%Y-%m")
        msg = (
            f"⚠️ KPI config chưa cập nhật tháng {current_month}, "
            f"anh Minh ơi update KPI đi nha 🙏"
        )
        send_telegram(msg)
        log_run(f"status=ERROR:config_month_mismatch config={config.get('month')} current={current_month}")
        sys.exit(0)

    start_date, end_date = build_date_range(config, today, override_end)

    # Ngày 1 đầu tháng chưa có data
    if end_date < start_date:
        msg = "📊 KPI Tracker: Chưa có dữ liệu tháng này (hôm nay là ngày 1)"
        send_telegram(msg)
        log_run("status=OK:no_data_day1")
        sys.exit(0)

    # Fetch data
    leads_data = fetch_leads(start_date, end_date)
    inbox_data = fetch_inbox(start_date, end_date)

    # Nếu Supabase lỗi hoàn toàn → gửi cảnh báo riêng trước, rồi vẫn tiếp tục gửi report
    if leads_data.get("error") and leads_data["total"] == 0:
        send_telegram("⚠️ KPI Tracker lỗi: không lấy được data Supabase")

    # Build và gửi message
    message = build_message(config, start_date, end_date, leads_data, inbox_data)
    sent = send_telegram(message)

    # Logging
    inbox_log = inbox_data["inbox"] if isinstance(inbox_data["inbox"], int) else "N/A"
    year_m, month_m = map(int, config["month"].split("-"))
    total_days_log = calendar.monthrange(year_m, month_m)[1]
    days_passed_log = (end_date - start_date).days + 1
    pace_leads_log = pace(config["leads_target"], days_passed_log, total_days_log)
    pace_inbox_log = pace(config["inbox_target"], days_passed_log, total_days_log)
    gap_leads_log = leads_data["total"] - pace_leads_log
    gap_inbox_log = (inbox_log - pace_inbox_log) if isinstance(inbox_log, int) else "N/A"

    cost = leads_data.get("cost")
    total_leads = leads_data["total"]
    if cost is not None and total_leads > 0:
        cpl_log = f"{cost / total_leads:.0f}"
    else:
        cpl_log = "N/A"

    errors_log = []
    if leads_data.get("error"):
        errors_log.append(f"supabase:{leads_data['error'][:80]}")
    if inbox_data.get("error"):
        errors_log.append(f"fb:{inbox_data['error'][:80]}")

    if sent and not errors_log:
        status = "OK"
    else:
        reasons = errors_log if errors_log else ["telegram_send_failed"]
        if not sent and errors_log:
            reasons = errors_log + ["telegram_send_failed"]
        status = f"ERROR:{';'.join(reasons)}"
    log_run(
        f"leads={leads_data['total']}/{pace_leads_log} gap={gap_leads_log} | "
        f"inbox={inbox_log}/{pace_inbox_log} gap={gap_inbox_log} | "
        f"cpl={cpl_log} | status={status}"
    )

    if not sent:
        log_error("Gửi Telegram thất bại sau 2 lần thử")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        msg = f"⚠️ KPI Tracker lỗi không xác định: {e}"
        log_error(f"{msg}\n{traceback.format_exc()}")
        send_telegram(msg)
        sys.exit(1)
