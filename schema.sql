-- Cloudflare D1 schema for the CARiNG e-store scraper
-- Apply once:  wrangler d1 execute <DB_NAME> --remote --file=schema.sql
-- (the scraper also runs this automatically on first start; every statement is idempotent)

CREATE TABLE IF NOT EXISTS products (
  id              INTEGER PRIMARY KEY,      -- Shopify product id
  handle          TEXT NOT NULL,
  title           TEXT NOT NULL,
  vendor          TEXT,
  category        TEXT,                     -- deepest "A/B/C" tag, e.g. "Skin & Haircare/Hair/Shampoo"
  tags            TEXT,                     -- comma separated
  url             TEXT,
  image           TEXT,
  price_min       REAL,
  price_max       REAL,
  compare_at_max  REAL,
  discount_pct    REAL,                     -- best discount vs compare-at price, 0 if none
  available       INTEGER NOT NULL DEFAULT 0,
  variant_count   INTEGER NOT NULL DEFAULT 1,
  published_at    TEXT,
  shop_updated_at TEXT,
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_vendor    ON products(vendor);
CREATE INDEX IF NOT EXISTS idx_products_category  ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen);

CREATE TABLE IF NOT EXISTS variants (
  id               INTEGER PRIMARY KEY,     -- Shopify variant id
  product_id       INTEGER NOT NULL,
  title            TEXT,
  sku              TEXT,
  price            REAL,
  compare_at_price REAL,
  available        INTEGER NOT NULL DEFAULT 0,
  last_seen        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_id);
CREATE INDEX IF NOT EXISTS idx_variants_sku     ON variants(sku);

-- One row per variant whenever its price / compare-at price / stock changes
CREATE TABLE IF NOT EXISTS price_history (
  variant_id       INTEGER NOT NULL,
  product_id       INTEGER NOT NULL,
  price            REAL,
  compare_at_price REAL,
  available        INTEGER,
  scraped_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, scraped_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,              -- running | ok | error
  products      INTEGER DEFAULT 0,
  new_products  INTEGER DEFAULT 0,
  price_changes INTEGER DEFAULT 0,
  message       TEXT
);
