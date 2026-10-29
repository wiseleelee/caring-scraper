"""Viewer website for the scraped CARiNG catalogue (reads from Cloudflare D1)."""
import csv
import io
import os
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, abort, render_template, request, url_for

from db import get_db

app = Flask(__name__)
db = get_db()
PER_PAGE = 48
PASSWORD = os.getenv("VIEWER_PASSWORD")  # optional: protects the site with a login prompt

SORTS = {
    "updated": "p.shop_updated_at DESC",
    "newest": "p.first_seen DESC, p.published_at DESC",
    "price_asc": "p.price_min ASC",
    "price_desc": "p.price_min DESC",
    "discount": "p.discount_pct DESC, p.price_min ASC",
    "title": "p.title ASC",
}


# ---------------------------------------------------------------- helpers
def requires_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if PASSWORD:
            auth = request.authorization
            if not auth or auth.password != PASSWORD:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Caring scraper"'})
        return f(*a, **kw)
    return wrapper


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def last_ok_run():
    rows = db.query("SELECT * FROM scrape_runs WHERE status='ok' ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def filters_from_request():
    a = request.args
    return {
        "q": a.get("q", "").strip(),
        "vendor": a.get("vendor", "").strip(),
        "category": a.get("category", "").strip(),
        "stock": a.get("stock", ""),
        "sale": a.get("sale", ""),
        "status": a.get("status", "active"),
        "sort": a.get("sort", "updated") if a.get("sort") in SORTS else "updated",
    }


def where_clause(f, run):
    where, params = [], []
    if f["q"]:
        like = f"%{f['q']}%"
        where.append("(p.title LIKE ? OR p.vendor LIKE ? OR p.tags LIKE ? OR EXISTS "
                     "(SELECT 1 FROM variants v WHERE v.product_id = p.id AND v.sku LIKE ?))")
        params += [like, like, like, like]
    if f["vendor"]:
        where.append("p.vendor = ?")
        params.append(f["vendor"])
    if f["category"]:
        where.append("(p.category = ? OR p.category LIKE ?)")
        params += [f["category"], f["category"] + "/%"]
    if f["stock"] == "in":
        where.append("p.available = 1")
    elif f["stock"] == "out":
        where.append("p.available = 0")
    if f["sale"] == "1":
        where.append("p.discount_pct > 0")
    if run and f["status"] == "active":
        where.append("p.last_seen >= ?")
        params.append(run["started_at"])
    elif run and f["status"] == "removed":
        where.append("p.last_seen < ?")
        params.append(run["started_at"])
    return ("WHERE " + " AND ".join(where)) if where else "", params


@app.template_filter("rm")
def rm(v):
    return "–" if v is None else f"RM {v:,.2f}"


@app.template_filter("when")
def when(v):
    """UTC ISO timestamp -> Malaysia time for display."""
    if not v:
        return ""
    try:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)
        return dt.strftime("%d %b %Y, %H:%M MYT")
    except ValueError:
        return v


@app.context_processor
def helpers():
    def page_url(**changes):
        args = request.args.to_dict()
        args.update({k: v for k, v in changes.items()})
        return url_for(request.endpoint, **{k: v for k, v in args.items() if v not in ("", None)},
                       **(request.view_args or {}))
    return {"page_url": page_url}


# ---------------------------------------------------------------- routes
@app.route("/")
@requires_auth
def index():
    f = filters_from_request()
    page = max(1, request.args.get("page", 1, type=int))
    run = last_ok_run()
    where, params = where_clause(f, run)

    total = db.query(f"SELECT COUNT(*) AS n FROM products p {where}", params)[0]["n"]
    rows = db.query(
        f"SELECT p.* FROM products p {where} ORDER BY {SORTS[f['sort']]} LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    )

    def overview():
        cutoff = run["started_at"] if run else ""
        return db.query(
            "SELECT COUNT(*) AS total, SUM(available) AS in_stock, "
            "SUM(CASE WHEN discount_pct > 0 THEN 1 ELSE 0 END) AS on_sale, "
            "COUNT(DISTINCT vendor) AS brands FROM products WHERE last_seen >= ?", [cutoff])[0]

    stats = cached("stats", 300, overview)
    vendors = cached("vendors", 900, lambda: db.query(
        "SELECT vendor, COUNT(*) AS n FROM products WHERE vendor IS NOT NULL GROUP BY vendor ORDER BY vendor"))
    categories = cached("categories", 900, lambda: db.query(
        "SELECT category, COUNT(*) AS n FROM products WHERE category IS NOT NULL GROUP BY category ORDER BY category"))

    pages = max(1, -(-total // PER_PAGE))
    return render_template("index.html", rows=rows, total=total, page=page, pages=pages, f=f,
                           stats=stats, run=run, vendors=vendors, categories=categories,
                           run_started=run["started_at"] if run else "")


@app.route("/product/<int:pid>")
@requires_auth
def product(pid):
    rows = db.query("SELECT * FROM products WHERE id = ?", [pid])
    if not rows:
        abort(404)
    variants = db.query("SELECT * FROM variants WHERE product_id = ? ORDER BY id", [pid])
    history = db.query(
        "SELECT h.*, v.title AS variant_title, v.sku FROM price_history h "
        "LEFT JOIN variants v ON v.id = h.variant_id WHERE h.product_id = ? ORDER BY h.scraped_at DESC LIMIT 200",
        [pid])
    run = last_ok_run()
    return render_template("product.html", p=rows[0], variants=variants, history=history,
                           run_started=run["started_at"] if run else "")


@app.route("/runs")
@requires_auth
def runs():
    rows = db.query("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 100")
    changes = db.query(
        "SELECT h.scraped_at, h.price, h.compare_at_price, h.available, p.id, p.title, p.vendor "
        "FROM price_history h JOIN products p ON p.id = h.product_id "
        "WHERE h.scraped_at > p.first_seen ORDER BY h.scraped_at DESC LIMIT 100")
    return render_template("runs.html", runs=rows, changes=changes)


@app.route("/export.csv")
@requires_auth
def export_csv():
    f = filters_from_request()
    where, params = where_clause(f, last_ok_run())
    rows = db.query(
        f"SELECT p.id, p.title, p.vendor, p.category, p.price_min, p.price_max, p.compare_at_max, "
        f"p.discount_pct, p.available, p.variant_count, "
        f"(SELECT group_concat(sku, ' | ') FROM variants v WHERE v.product_id = p.id) AS skus, "
        f"p.url, p.image, p.first_seen, p.last_seen FROM products p {where} ORDER BY {SORTS[f['sort']]}",
        params)
    buf = io.StringIO()
    cols = ["id", "title", "vendor", "category", "price_min", "price_max", "compare_at_max", "discount_pct",
            "available", "variant_count", "skus", "url", "image", "first_seen", "last_seen"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=caring-products.csv"})


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=bool(os.getenv("DEBUG")))
