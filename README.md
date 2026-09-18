# gh-trending-data

Bản chụp github.com/trending hàng ngày, cào bằng GitHub Actions.

- `languages.txt`: danh sách ngôn ngữ cần lấy (slug theo URL trending; `any` = trang tổng).
- `.github/workflows/trending.yml`: chạy 06:30 giờ Hà Nội mỗi ngày, hoặc bấm **Run workflow** trong tab Actions để chạy tay.
- `scrape.py`: lấy daily + weekly cho từng ngôn ngữ (thêm monthly vào ngày 1), gộp theo repo.
- `data/<YYYY-MM-DD>.json` và `data/latest.json`: kết quả.

Cấu trúc một repo trong `repos[]`:

```json
{
  "full_name": "owner/name",
  "description": "...",
  "language": "Rust",
  "stars": 765,
  "forks": 12,
  "appearances": [
    {"period": "weekly", "language": "rust", "rank": 3, "gained": 98}
  ]
}
```
