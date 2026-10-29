# CARiNG e-store scraper

Scrapes the public product catalogue of https://estore.caring2u.com into a Cloudflare D1 database
and shows it on a small website.

## Why this method
The store runs on Shopify, which publishes its full public catalogue as JSON at
`/products.json?limit=250&page=N` (about 10,700 products / 43 pages as of Oct 2026).
Reading that feed means no headless browser and no HTML parsing, so the scraper is fast (a few minutes),
cheap to run, and doesn't break when the site's design changes. robots.txt allows crawling (`Allow: /`).
The scraper waits 1.5 s between pages and backs off on rate limits.

## Pieces
| File | What it does |
|---|---|
| `scraper.py` | Render **cron job**. Fetches every page, upserts products and variants into D1, records price/stock changes in `price_history`, logs each run in `scrape_runs`. |
| `app.py` + `templates/` | Render **web service**. Search, filter (brand, category, stock, on sale, delisted), sort, product detail with price history, run log, CSV export. |
| `db.py` | Talks to D1 through Cloudflare's REST API (or local SQLite for testing). |
| `schema.sql` | D1 tables. Applied automatically by the scraper on every run (idempotent). |
| `render.yaml` | Render Blueprint that creates both services. |

## Setup
1. **Cloudflare D1**: Dashboard → Storage & Databases → D1 → Create database (e.g. `caring-scraper`). Copy its **Database ID**.
   Your **Account ID** is shown on the Workers & Pages overview page.
2. **API token**: My Profile → API Tokens → Create Token → Custom token, permission **Account → D1 → Edit**. Copy the token.
3. **Render**: push this folder to a GitHub repo, then Render → New → Blueprint → pick the repo.
   Fill in `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, `CF_API_TOKEN` and (optional) `VIEWER_PASSWORD`.
4. Open the cron job in Render and click **Trigger Run** for the first scrape. After that it runs daily at 03:13 MYT.

## Environment variables
| Name | Required | Notes |
|---|---|---|
| `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, `CF_API_TOKEN` | yes | D1 access |
| `VIEWER_PASSWORD` | no | if set, the website asks for a password (any username) |
| `PAGE_DELAY_SECONDS` | no | default 1.5 |
| `LOCAL_SQLITE_PATH` | dev only | use a local SQLite file instead of D1 |

## Local test
```
pip install -r requirements.txt
LOCAL_SQLITE_PATH=dev.db python scraper.py
LOCAL_SQLITE_PATH=dev.db python app.py   # http://localhost:8000
```
