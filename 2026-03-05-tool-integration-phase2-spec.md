# SPEC — Tool Integration Phase 2

- **Owner:** Anh Minh + Editor Code
- **Prepared by:** Ní
- **Date:** 2026-03-05
- **Goal:** Wire 3 module mới vào workflow thật (cron scripts + skills).

---

## Context

Sau phase 1 (skill workflow optimization), 3 module đã deploy vào `tools/` nhưng chưa được gọi trong luồng thực:
- `tools/github_token_manager.py` — auto-select token, fallback, log masked
- `tools/log_noise_filter.py` — lọc log trước khi đẩy ra Telegram/user
- `tools/market_data_guardrail.py` — validate + attach timestamp/source cho market data

---

## Scope

### 1) GitHub Token Manager — wiring vào git workflow (priority: HIGH)

**Hiện trạng:** Các script push GitHub dùng `cat credentials/github_token.txt` thủ công.

**Yêu cầu:**
- Refactor helper lấy token trong các script sau sang dùng `TokenManager` từ `tools/github_token_manager.py`:
  - `skills/seo-outline/` — bất kỳ chỗ nào dùng token để push
  - `skills/seo-article/` — tương tự
- Policy token:
  1. Classic token: `credentials/github_token.txt`
  2. Fallback: `credentials/github_token_fire_gains.txt`
  3. Nếu có `credentials/github_seo_repos.json` map `owner/repo → preferred_token` thì ưu tiên theo map
- Log rõ token nào pass/fail nhưng **không log raw token** (chỉ masked: `ghp_****xxxx`)

**Files cần sửa:**
- Bất kỳ script trong `skills/seo-outline/` và `skills/seo-article/` dùng git push/token

**Files cần tạo:**
- `credentials/token_config.json` — copy từ `tools/token_config.example.json`, điền đúng paths

**Acceptance:**
- Push GitHub thành công không cần retry thủ công
- Log in rõ token nào được dùng (masked)

---

### 2) Log Noise Filter — wrap cron output (priority: MEDIUM)

**Hiện trạng:** Output của cron scripts đẩy thẳng ra Telegram, có lẫn internal log gây nhiễu.

**Yêu cầu:**
- Wrap output của 2 script chính qua `LogNoiseFilter` từ `tools/log_noise_filter.py`:
  - `skills/report-ads/run_report.py`
  - `skills/kpi-tracker/scripts/kpi_tracker.py`
- Config filter:
  - **Suppress** (im lặng): `script ran ok`, `no output`, `atom_N ✅`, `save_db ✅`, `[AoT]`, debug lines
  - **Alert** (luôn giữ): `❌`, `⚠️ KPI`, `⚠️ Lỗi`, `FAIL`, `anomaly`
  - **Pass** (giữ nguyên): report body thực sự (lines có `📊`, `🔵`, `🎵`, `🟢`, `📌`)
- Config file: `tools/log_filter_config.json` (copy từ `tools/log_filter_config.example.json` rồi chỉnh)

**Files cần sửa:**
- `skills/report-ads/run_report.py` — wrap output qua filter trước khi gửi Telegram
- `skills/kpi-tracker/scripts/kpi_tracker.py` — wrap logger output qua filter

**Files cần tạo:**
- `tools/log_filter_config.json` — config filter thực tế

**Acceptance:**
- Chat Telegram chỉ còn report body + alert thật
- Internal log (`atom_N ✅`, `save_db`, `[AoT] Phase...`) không xuất hiện trong tin nhắn user

---

### 3) Market Data Guardrail — hook vào search/response (priority: MEDIUM)

**Hiện trạng:** Khi trả lời câu hỏi giá vàng/chứng khoán, không có guardrail tự động enforce timestamp + source.

**Yêu cầu:**
- Tạo skill mới `skills/market-data/SKILL.md` để trigger khi anh hỏi:
  - "giá vàng", "giá USD", "chứng khoán", "VN-Index", "giá bitcoin", "tỷ giá"
- Skill này gọi `tools/market_data_guardrail.py` để validate data trước khi trả
- Wrapper script: `skills/market-data/scripts/market_lookup.py`
  - Nhận: `asset_name`, `value`, `source`, `timestamp`
  - Trả: formatted response có timestamp + source + confidence label
- Nếu response thiếu timestamp hoặc source → tự động label `⚠️ Không có nguồn/thời gian xác nhận`

**Files cần sửa:**
- (không có file nào cần sửa)

**Files cần tạo:**
- `skills/market-data/SKILL.md`
- `skills/market-data/scripts/market_lookup.py`

**Acceptance:**
- Câu trả lời về market data luôn có `Nguồn: X` + `Cập nhật: HH:MM DD/MM/YYYY`
- Không còn trả số "ảo" không có timestamp/source

---

## Definition of Done
1. GitHub push dùng token manager, không hardcode path thủ công
2. Cron Telegram output sạch, không còn internal log
3. Market data response có timestamp + source mặc định
4. Tất cả Python files compile pass (`python3 -m py_compile`)

---

## Handoff note cho Editor Code
- Dùng Python stdlib tối đa, ít dependency ngoài
- Idempotent — chạy nhiều lần không hỏng
- Không thay đổi business logic hiện tại của report/kpi scripts
- Backward compatible với token file hiện tại (`credentials/github_token.txt`)
