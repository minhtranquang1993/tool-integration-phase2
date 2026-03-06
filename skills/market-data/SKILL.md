---
name: market-data
description: >-
  Validate market data responses (giá vàng, chứng khoán, tỷ giá, bitcoin...) 
  với timestamp + source enforcement. Trigger khi anh hỏi về giá cả thị trường.
---

# 📋 SKILL: Market Data Guardrail

## Trigger

Khi anh hỏi bất kỳ câu nào liên quan đến:
- **Giá vàng** (SJC, 9999, vàng miếng)
- **Giá USD**, **tỷ giá** ngoại tệ
- **Chứng khoán**, **VN-Index**, **HNX**, cổ phiếu
- **Giá bitcoin**, crypto, ETH, BTC
- **Giá dầu**, **giá xăng**

## Khi nhận trigger → em làm:

### Bước 1: Thu thập data
1. Search/fetch giá từ nguồn tin cậy
2. Ghi nhận: `asset_name`, `value`, `source`, `timestamp`

### Bước 2: Validate qua guardrail

```bash
python3 skills/market-data/scripts/market_lookup.py \
  --asset "Vàng SJC" \
  --value "92,500,000" \
  --source "SJC.com.vn" \
  --timestamp "2026-03-05T19:30:00+07:00"
```

### Bước 3: Trả kết quả
- Kết quả luôn có: **Nguồn** + **Thời gian cập nhật** + **Confidence label**
- Nếu thiếu timestamp/source → tự động label `⚠️ Không có nguồn/thời gian xác nhận`

## Output Contract

Mỗi câu trả lời về market data **BẮT BUỘC** phải có:
1. `Nguồn: X` — trang web hoặc API lấy data
2. `Cập nhật: HH:MM DD/MM/YYYY` — thời điểm data được cập nhật
3. Confidence label: `🟢 Realtime` / `🟡 Gần đây` / `🔴 Cũ`

## Anti-Hallucination

- **KHÔNG BAO GIỜ** trả số liệu từ training data hoặc đoán
- Nếu không fetch được data real-time → nói rõ: "Em không lấy được giá real-time"
- Luôn ghi nguồn cụ thể, không ghi "theo thông tin em biết"

## Mesh Connections
- **→ search-kit**: Dùng để fetch giá real-time
- **→ report-ads**: Không liên quan — skill này độc lập
