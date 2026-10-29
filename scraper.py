"""CARiNG e-store scraper.

Method: estore.caring2u.com runs on Shopify, which publishes the whole public catalogue as JSON at
/products.json (250 products per page). Reading that feed is far faster and more stable than parsing
HTML pages, needs no headless browser, and carries every price, SKU, stock flag and category tag.

Run:  python scraper.py            (uses env vars, see README)
"""
import os
import sys
import time
from datetime import datetime, timezone

import requests

from db import get_db, lit

BASE_URL = os.getenv("STORE_URL", "https://estore.caring2u.com").rstrip("/")
PAGE_SIZE = 250
PAGE_DELAY = float(os.getenv("PAGE_DELAY_SECONDS", "1.5"))  # be polite to the store
MAX_PAGES = int(os.getenv("MAX_PAGES", "200"))
BATCH_BYTES = 90_000  # D1 caps one SQL statement at 100 KB; keep each request well under
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; CaringCatalogueBot/1.0; price monitoring)",
)
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


# ---------------------------------------------------------------- fetching
def fetch_page(session, page):
    url = f"{BASE_URL}/products.json"
    for attempt in range(6):
        try:
            r = session.get(url, params={"limit": PAGE_SIZE, "page": page}, timeout=60)
            if r.status_code == 200:
                return r.json().get("products", [])
            if r.status_code in (429, 430) or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
                log(f"page {page}: HTTP {r.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"page {page}: {e}, retrying")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Giving up on page {page}")


def fetch_all():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    products, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        batch = fetch_page(s, page)
        if not batch:
            break
        fresh = [p for p in batch if p["id"] not in seen]
        seen.update(p["id"] for p in fresh)
        products.extend(fresh)
        log(f"page {page}: {len(batch)} products (total {len(products)})")
        time.sleep(PAGE_DELAY)
    return products


# ---------------------------------------------------------------- transform
def money(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def pick_category(tags):
    paths = [t for t in tags if "/" in t]
    if not paths:
        return None
    return max(paths, key=lambda t: (t.count("/"), len(t)))


def transform(p, ts):
    variants = []
    for v in p.get("variants", []):
        variants.append({
            "id": v["id"],
            "product_id": p["id"],
            "title": None if v.get("title") == "Default Title" else v.get("title"),
            "sku": v.get("sku") or None,
            "price": money(v.get("price")),
            "compare_at_price": money(v.get("compare_at_price")),
            "available": 1 if v.get("available") else 0,
        })
    prices = [v["price"] for v in variants if v["price"] is not None]
    caps = [v["compare_at_price"] for v in variants if v["compare_at_price"] is not None]
    discounts = [
        (v["compare_at_price"] - v["price"]) / v["compare_at_price"] * 100
        for v in variants
        if v["price"] is not None and v["compare_at_price"] and v["compare_at_price"] > v["price"]
    ]
    images = p.get("images") or []
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    product = {
        "id": p["id"],
        "handle": p["handle"],
        "title": (p.get("title") or "").strip(),
        "vendor": (p.get("vendor") or "").strip() or None,
        "category": pick_category(tags),
        "tags": ", ".join(tags) or None,
        "url": f"{BASE_URL}/products/{p['handle']}",
        "image": images[0]["src"] if images else None,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "compare_at_max": max(caps) if caps else None,
        "discount_pct": round(max(discounts), 1) if discounts else 0,
        "available": 1 if any(v["available"] for v in variants) else 0,
        "variant_count": len(variants),
        "published_at": p.get("published_at"),
        "shop_updated_at": p.get("updated_at"),
        "first_seen": ts,
        "last_seen": ts,
    }
    return product, variants


# ---------------------------------------------------------------- SQL building
P_COLS = ["id", "handle", "title", "vendor", "category", "tags", "url", "image", "price_min",
          "price_max", "compare_at_max", "discount_pct", "available", "variant_count",
          "published_at", "shop_updated_at", "first_seen", "last_seen"]
V_COLS = ["id", "product_id", "title", "sku", "price", "compare_at_price", "available", "last_seen"]


def upsert_sql(table, cols, row, keep=("first_seen",)):
    vals = ", ".join(lit(row[c]) for c in cols)
    sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id" and c not in keep)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({vals}) ON CONFLICT(id) DO UPDATE SET {sets};"


def build_statements(products, previous, ts):
    """previous: {variant_id: (price, compare_at, available)} from the last run."""
    stmts, n_changes = [], 0
    for raw in products:
        product, variants = transform(raw, ts)
        stmts.append(upsert_sql("products", P_COLS, product))
        for v in variants:
            v["last_seen"] = ts
            stmts.append(upsert_sql("variants", V_COLS, v, keep=()))
            state = (v["price"], v["compare_at_price"], v["available"])
            if previous.get(v["id"]) != state:
                if v["id"] in previous:
                    n_changes += 1
                stmts.append(
                    "INSERT INTO price_history (variant_id, product_id, price, compare_at_price, available, scraped_at) "
                    f"VALUES ({lit(v['id'])}, {lit(v['product_id'])}, {lit(v['price'])}, "
                    f"{lit(v['compare_at_price'])}, {lit(v['available'])}, {lit(ts)});"
                )
    return stmts, n_changes


def chunk(stmts, limit=BATCH_BYTES):
    buf, size = [], 0
    for s in stmts:
        n = len(s.encode("utf-8")) + 1
        if buf and size + n > limit:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(s)
        size += n
    if buf:
        yield "\n".join(buf)


# ---------------------------------------------------------------- main
def load_previous(db):
    rows = db.query("SELECT id, price, compare_at_price, available FROM variants")
    return {r["id"]: (r["price"], r["compare_at_price"], r["available"]) for r in rows}


def run(products=None):
    db = get_db()
    with open(SCHEMA) as f:
        db.script(f.read())

    ts = now_iso()
    db.query("INSERT INTO scrape_runs (started_at, status) VALUES (?, 'running')", [ts])
    run_id = db.query("SELECT MAX(id) AS id FROM scrape_runs")[0]["id"]

    try:
        known_ids = {r["id"] for r in db.query("SELECT id FROM products")}
        previous = load_previous(db)
        log(f"{len(known_ids)} products already in database")

        if products is None:
            products = fetch_all()
        if not products:
            raise RuntimeError("Store returned no products; nothing written")

        stmts, n_changes = build_statements(products, previous, ts)
        batches = list(chunk(stmts))
        log(f"writing {len(stmts)} statements in {len(batches)} batches")
        for i, sql in enumerate(batches, 1):
            db.script(sql)
            if i % 10 == 0 or i == len(batches):
                log(f"  batch {i}/{len(batches)}")

        n_new = sum(1 for p in products if p["id"] not in known_ids)
        db.query(
            "UPDATE scrape_runs SET finished_at=?, status='ok', products=?, new_products=?, price_changes=? WHERE id=?",
            [now_iso(), len(products), n_new, n_changes, run_id],
        )
        log(f"done: {len(products)} products, {n_new} new, {n_changes} price/stock changes")
    except Exception as e:
        db.query(
            "UPDATE scrape_runs SET finished_at=?, status='error', message=? WHERE id=?",
            [now_iso(), str(e)[:500], run_id],
        )
        raise


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # non-zero exit so Render marks the cron run as failed
        log(f"FAILED: {e}")
        sys.exit(1)
