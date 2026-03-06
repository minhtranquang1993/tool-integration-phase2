---
name: report-ads
description: >-
  Chạy report quảng cáo và gửi Telegram.
  Output được filter qua LogNoiseFilter trước khi gửi.
---

# 📋 SKILL: Report Ads

## Trigger

Chạy tự động qua cron hoặc khi anh yêu cầu report quảng cáo.

## Workflow

1. Thu thập dữ liệu ads (Google Ads, Meta, etc.)
2. Xử lý, tính toán KPI
3. Format report
4. **Filter output** qua `LogNoiseFilter` → chỉ giữ report body + alert
5. Gửi kết quả sạch lên Telegram

## Log Filter

Output được filter trước khi gửi Telegram:
- **Suppress**: `atom_N ✅`, `save_db ✅`, `[AoT]`, debug lines, "script ran ok"
- **Alert**: `❌`, `⚠️ Lỗi`, `FAIL`, `anomaly`
- **Pass**: Report body (`📊`, `🔵`, `🎵`, `🟢`, `📌`, `BÁO CÁO`)
