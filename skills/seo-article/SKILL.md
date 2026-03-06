---
name: seo-article
description: >-
  Viết bài SEO hoàn chỉnh rồi push lên GitHub.
  Dùng TokenManager để auto-select token cho git push.
---

# 📋 SKILL: SEO Article

## Trigger

Khi anh yêu cầu viết bài SEO từ outline có sẵn.

## Workflow

1. Nhận outline + keyword từ anh
2. Viết bài SEO hoàn chỉnh (HTML, 3000-5000 words)
3. Lưu file vào repo và push lên GitHub

## Git Push

Script `scripts/push_to_github.py` sử dụng `git_push_helper` (TokenManager) để:
- Auto-select token theo policy (classic → fallback → repo override)
- Log masked token (không bao giờ log raw token)
- Retry tự động nếu token đầu tiên fail

```bash
python3 skills/seo-article/scripts/push_to_github.py \
  --repo "owner/repo" \
  --dir "/path/to/local/repo" \
  --branch main \
  --msg "add: seo article for keyword X"
```
