name: kpi-tracker
description: >-
  Theo dõi tiến độ KPI leads + inbox Messenger tháng này (01/{tháng} → hôm qua).
  Tự động gửi báo cáo 16h30 ICT hàng ngày qua Telegram.
  Trigger on-demand: "/kpi", "kpi hôm nay", "tiến độ leads", "leads tháng này"

cron: "30 9 * * *"   # 9h30 UTC = 16h30 ICT
script: python3 skills/kpi-tracker/scripts/kpi_tracker.py
on-demand: python3 skills/kpi-tracker/scripts/kpi_tracker.py --date YYYY-MM-DD
config: skills/kpi-tracker/config/kpi_config.json

# --- Hướng dẫn ---
# Update KPI đầu tháng:
#   - Anh báo KPI tháng mới → em update kpi_config.json (month + leads_target + inbox_target + updated_at)
#   - Hoặc ngày 1 hàng tháng em chủ động hỏi anh

# Credentials cần có trong workspace:
#   - credentials/supabase_key.txt   (Supabase DND service role key)
#   - credentials/fb_token.txt       (Facebook Ads access token)
#   - credentials/telegram_token.txt (Telegram bot token)
