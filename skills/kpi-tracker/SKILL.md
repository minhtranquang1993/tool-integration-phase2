---
name: kpi-tracker
description: >-
  Theo dõi KPI và gửi alert qua Telegram.
  Logger output được filter qua LogNoiseFilter trước khi gửi.
---

# 📋 SKILL: KPI Tracker

## Trigger

Chạy tự động qua cron để theo dõi KPI hàng ngày/tuần.

## Workflow

1. Kết nối data sources (DB, APIs)
2. Tính toán KPI metrics
3. So sánh với thresholds
4. **Filter output** qua `LogNoiseFilter` → chỉ giữ KPI summary + alert
5. Gửi kết quả sạch lên Telegram

## Log Filter

Output được filter trước khi gửi Telegram:
- **Suppress**: `atom_N ✅`, `save_db ✅`, `[AoT]`, debug lines, "script ran ok"
- **Alert**: `❌`, `⚠️ KPI`, `⚠️ Lỗi`, `FAIL`, `anomaly`
- **Pass**: KPI body (`📊`, `🔵`, `🎵`, `🟢`, `📌`, `🩺`, `DATA HEALTH`)
