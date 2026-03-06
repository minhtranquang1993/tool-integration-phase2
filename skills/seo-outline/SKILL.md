---
name: seo-outline
description: >-
  Tạo SEO outline, build content, rồi push lên GitHub.
  Dùng TokenManager để auto-select token cho git push.
---

# 📋 SKILL: SEO Outline

## Trigger

Khi anh yêu cầu tạo outline SEO cho bài viết mới.

## Workflow

1. Nhận keyword + brief từ anh
2. Research SERP, phân tích top kết quả
3. Tạo outline (headings, subtopics, content brief)
4. Lưu file vào repo và push lên GitHub

## Git Push

Script `scripts/push_to_github.py` sử dụng `git_push_helper` (TokenManager) để:
- Auto-select token theo policy (classic → fallback → repo override)
- Log masked token (không bao giờ log raw token)
- Retry tự động nếu token đầu tiên fail

```bash
python3 skills/seo-outline/scripts/push_to_github.py \
  --repo "owner/repo" \
  --dir "/path/to/local/repo" \
  --branch main \
  --msg "add: seo outline for keyword X"
```
