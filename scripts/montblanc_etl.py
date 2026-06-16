#!/usr/bin/env python3
"""
Montblanc ETL  —  PC Graf (SQL Server 192.168.1.36) → Railway PostgreSQL
Runs all 8 pipeline tables: products, customers, sales, inventory,
customer_purchases, vip_customers, daily_summary, dead_stock.
"""

import os
import sys
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import pyodbc
import psycopg2
from psycopg2.extras import execute_values

# ── Config ────────────────────────────────────────────────────────────────────

SQL_SERVER   = os.getenv("SQL_SERVER",   "192.168.1.36")
SQL_DB       = os.getenv("SQL_DB",       "siawin14")
SQL_USER     = os.getenv("SQL_USER",     "jaya")
SQL_PASSWORD = os.getenv("SQL_PASSWORD", "PurpleCar20")
SQL_DRIVER   = os.getenv("SQL_DRIVER",   "ODBC Driver 18 for SQL Server")

PG_DSN = os.getenv("MONTBLANC_PG_DSN")
if not PG_DSN:
    raise ValueError("MONTBLANC_PG_DSN environment variable is required")

# Pull sales from this date onwards (6 years of history)
SALES_FROM = os.getenv("SALES_FROM", "2020-01-01")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("montblanc_etl")

# ── Connections ───────────────────────────────────────────────────────────────

def mssql_connect():
    cs = (
        f"DRIVER={{{SQL_DRIVER}}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DB};"
        f"UID={SQL_USER};"
        f"PWD={SQL_PASSWORD};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(cs, timeout=30)

def pg_connect():
    return psycopg2.connect(PG_DSN, connect_timeout=30)

# ── Derivation helpers ────────────────────────────────────────────────────────

def derive_collection(name: str):
    n = (name or "").upper()
    if "MEISTERSTÜCK" in n or "MEISTERSTUCK" in n or "MSTK" in n:
        return "Meisterstück"
    if "1858" in n:
        return "1858"
    if "STARWALKER" in n or "STAR WALKER" in n:
        return "StarWalker"
    if "SARTORIAL" in n:
        return "Sartorial"
    if "M_GRAM" in n or "MGRAM" in n or "M GRAM" in n:
        return "M_Gram"
    if "EXTREME" in n:
        return "Extreme 3.0"
    if "ROUGE" in n or "ROUGE & NOIR" in n or "ROUGE&NOIR" in n:
        return "Heritage Rouge & Noir"
    if "HERITAGE" in n:
        return "Heritage"
    if "BOHEME" in n or "BOHÈME" in n:
        return "Bohème"
    if "AUGMENTED" in n:
        return "Augmented Paper"
    if "VICTOR HUGO" in n:
        return "Victor Hugo"
    if "SUMMIT" in n:
        return "Summit"
    return None

def derive_category(name: str):
    n = (name or "").upper()
    # Pens checked first since names often start with type
    if "PLUMA" in n or "FOUNTAIN" in n:
        return "Fountain Pen"
    if "BOLIGRAFO" in n or "BOLÍGRAFO" in n or "BALLPOINT" in n:
        return "Ballpoint"
    if "ROLLER" in n:
        return "Rollerball"
    if "FINELINER" in n:
        return "Fineliner"
    # Watches
    if "RELOJ" in n or "WATCH" in n or "CHRONO" in n:
        return "Watch"
    # Leather / bags
    if "MALETIN" in n or "MALETÍN" in n or "BRIEFCASE" in n:
        return "Bag"
    if "BOLSO" in n or "MOCHILA" in n or "BOLSA" in n or "BAG" in n or "BACKPACK" in n or "TOTE" in n:
        return "Bag"
    if "BILLETERA" in n or "WALLET" in n or "CARTERA" in n:
        return "Wallet"
    if "CINTURON" in n or "CINTURÓN" in n or "CINTO" in n or "BELT" in n or "CORREA" in n:
        return "Belt"
    if "TARJETERO" in n or "CARD HOLDER" in n or "CARD-HOLDER" in n:
        return "Card Holder"
    # Stationery / paper
    if "LIBRETA" in n or "NOTEBOOK" in n or "AGENDA" in n or "CUADERNO" in n or "DIARIO" in n:
        return "Stationery"
    if "LLAVERO" in n or "KEYRING" in n or "KEY RING" in n or "KEY-RING" in n:
        return "Keyring"
    if "ESCRITORIO" in n or "DESK" in n or "ORGANIZADOR" in n:
        return "Desk Organizer"
    if "PORTA DOCUMENTO" in n or "PORTA-DOCUMENTO" in n or "DOCUMENT HOLDER" in n:
        return "Document Holder"
    # Ink / refills
    if "RECARGA" in n or "REFILL" in n or "TINTA" in n or "INK" in n or "CARTUCHO" in n:
        return "Refill/Ink"
    # Fragrance
    if "PERFUME" in n or "FRAGRANC" in n or " EDP" in n or " EDT" in n:
        return "Perfume"
    # Pen cases
    if "ESTUCHE" in n or "POUCH" in n or "PORTA PLUMA" in n or "PEN CASE" in n or "PEN POUCH" in n:
        return "Pen Case/Pouch"
    # Eyewear
    if "GAFA" in n or "LENTE" in n or "EYEWEAR" in n or "SUNGLASS" in n:
        return "Eyewear"
    # Jewelry
    if "JOYA" in n or "COLLAR" in n or "PULSERA" in n or "ANILLO" in n or "JEWELRY" in n:
        return "Jewelry"
    # Sets
    if " SET " in n or n.endswith(" SET") or "GIFT SET" in n or "WRITING SET" in n or "JUEGO" in n:
        return "Set"
    return None

def safe_float(val):
    if val is None:
        return None
    try:
        return float(Decimal(str(val)))
    except (InvalidOperation, ValueError):
        return None

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_code       VARCHAR(10)   PRIMARY KEY,
    name                VARCHAR(100)  NOT NULL,
    email               VARCHAR(150),
    phone               VARCHAR(50),
    segment             VARCHAR(10),
    registration_date   DATE,
    last_purchase_date  DATE,
    synced_at           TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    sku                 VARCHAR(20)   PRIMARY KEY,
    name                VARCHAR(200)  NOT NULL,
    collection          VARCHAR(50),
    category            VARCHAR(30),
    price_usd           NUMERIC(10,2),
    active              BOOLEAN       DEFAULT TRUE,
    last_sold_date      DATE,
    days_since_sold     INTEGER,
    synced_at           TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sales (
    id                  SERIAL        PRIMARY KEY,
    sale_id             VARCHAR(20)   NOT NULL,
    sale_date           TIMESTAMPTZ   NOT NULL,
    customer_code       VARCHAR(10),
    customer_name       VARCHAR(100),
    sku                 VARCHAR(20)   NOT NULL,
    product_name        VARCHAR(200),
    collection          VARCHAR(50),
    category            VARCHAR(30),
    quantity            NUMERIC(10,2) NOT NULL,
    price_usd           NUMERIC(10,2) NOT NULL,
    line_total_usd      NUMERIC(12,2) NOT NULL,
    store               VARCHAR(5)    DEFAULT '23',
    synced_at           TIMESTAMPTZ   DEFAULT NOW(),
    UNIQUE (sale_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_sales_date       ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_customer   ON sales(customer_code);
CREATE INDEX IF NOT EXISTS idx_sales_sku        ON sales(sku);
CREATE INDEX IF NOT EXISTS idx_sales_collection ON sales(collection);

CREATE TABLE IF NOT EXISTS inventory (
    sku                 VARCHAR(20)   NOT NULL,
    product_name        VARCHAR(200),
    collection          VARCHAR(50),
    category            VARCHAR(30),
    store_code          VARCHAR(5)    NOT NULL,
    store_name          VARCHAR(100),
    stock_qty           NUMERIC(10,2) NOT NULL DEFAULT 0,
    synced_at           TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (sku, store_code)
);

CREATE TABLE IF NOT EXISTS customer_purchases (
    id                  SERIAL        PRIMARY KEY,
    customer_code       VARCHAR(10)   NOT NULL,
    sku                 VARCHAR(20)   NOT NULL,
    product_name        VARCHAR(200),
    collection          VARCHAR(50),
    category            VARCHAR(30),
    purchase_date       DATE          NOT NULL,
    price_usd           NUMERIC(10,2),
    synced_at           TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cp_customer   ON customer_purchases(customer_code);
CREATE INDEX IF NOT EXISTS idx_cp_collection ON customer_purchases(collection);
CREATE INDEX IF NOT EXISTS idx_cp_category   ON customer_purchases(category);

CREATE TABLE IF NOT EXISTS vip_customers (
    customer_code           VARCHAR(10)   PRIMARY KEY,
    name                    VARCHAR(100),
    email                   VARCHAR(150),
    phone                   VARCHAR(50),
    lifetime_value_usd      NUMERIC(14,2),
    purchase_count          INTEGER,
    last_purchase_date      DATE,
    first_purchase_date     DATE,
    preferred_category      VARCHAR(30),
    preferred_collection    VARCHAR(50),
    owned_skus              JSONB,
    owned_categories        JSONB,
    vip_tier                VARCHAR(10),
    synced_at               TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_summary (
    summary_date            DATE          PRIMARY KEY,
    revenue_usd             NUMERIC(12,2),
    transaction_count       INTEGER,
    units_sold              NUMERIC(10,2),
    top_sku                 VARCHAR(20),
    top_product_name        VARCHAR(200),
    top_collection          VARCHAR(50),
    top_category            VARCHAR(30),
    revenue_mtd             NUMERIC(14,2),
    txn_count_mtd           INTEGER,
    revenue_same_day_lm     NUMERIC(12,2),
    revenue_mtd_lm          NUMERIC(14,2),
    txn_count_mtd_lm        INTEGER,
    pct_change_revenue      NUMERIC(7,2),
    synced_at               TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dead_stock (
    sku                 VARCHAR(20)   PRIMARY KEY,
    product_name        VARCHAR(200),
    collection          VARCHAR(50),
    category            VARCHAR(30),
    stock_qty           NUMERIC(10,2),
    price_usd           NUMERIC(10,2),
    total_value_usd     NUMERIC(12,2),
    last_sold_date      DATE,
    days_since_sold     INTEGER,
    synced_at           TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ds_collection      ON dead_stock(collection);
CREATE INDEX IF NOT EXISTS idx_ds_category        ON dead_stock(category);
CREATE INDEX IF NOT EXISTS idx_ds_days_since_sold ON dead_stock(days_since_sold);
"""

# ── PC Graf queries ───────────────────────────────────────────────────────────

Q_PRODUCTS = """
SELECT
    i.sCodigo_Producto          AS sku,
    i.sDescripcion_Inventario   AS name,
    i.cPrecio_Publico           AS price_usd,
    i.bEstado                   AS active
FROM siawin14.dbo.IN04 i
WHERE i.sCodigo_Producto LIKE 'MB%'
  AND i.sDescripcion_Inventario IS NOT NULL
ORDER BY i.sCodigo_Producto
"""

Q_CUSTOMERS = """
SELECT DISTINCT
    c.sCodigo               AS customer_code,
    c.sNombre               AS name,
    c.sDireccion_E_Mail     AS email,
    c.sTelefono             AS phone,
    c.sClase_Cliente        AS segment,
    c.dFecha_Ingreso        AS registration_date
FROM siawin14.dbo.CC01 c
WHERE c.sCodigo IN (
    SELECT DISTINCT h.sCodigo_Cliente
    FROM siawin14.dbo.FA00 h
    WHERE h.sBodega = '23'
      AND h.bEstado = 1
      AND h.sTipoFactura = 'FA'
      AND h.sCodigo_Cliente IS NOT NULL
      AND h.sCodigo_Cliente != ''
      AND EXISTS (
          SELECT 1 FROM siawin14.dbo.FA01 l
          WHERE l.sPedido = h.sPedido
            AND l.sCodigo_Producto LIKE 'MB%'
      )
)
  AND c.sCodigo NOT IN ('000001', '000002')
"""

Q_SALES = """
SELECT
    h.sPedido               AS sale_id,
    h.dFecha                AS sale_date,
    h.sCodigo_Cliente       AS customer_code,
    h.sNombre_Cliente       AS customer_name,
    l.sCodigo_Producto      AS sku,
    l.sDescripcion          AS product_name,
    l.cCantidad             AS quantity,
    l.cPrecio_Venta         AS price_usd,
    i.cPrecio_Publico       AS list_price
FROM siawin14.dbo.FA00 h
JOIN siawin14.dbo.FA01 l ON l.sPedido = h.sPedido
LEFT JOIN siawin14.dbo.IN04 i ON i.sCodigo_Producto = l.sCodigo_Producto
WHERE h.sBodega = '23'
  AND h.bEstado = 1
  AND h.sTipoFactura = 'FA'
  AND l.sCodigo_Producto LIKE 'MB%'
  AND h.dFecha >= ?
  AND l.cPrecio_Venta > 0
  AND (i.cPrecio_Publico IS NULL OR l.cPrecio_Venta < i.cPrecio_Publico * 3)
ORDER BY h.dFecha DESC
"""

Q_INVENTORY = """
SELECT
    s.SKU           AS sku,
    s.NombreProducto AS product_name,
    s.Bodega        AS store_code,
    s.NombreBodega  AS store_name,
    SUM(s.SaldoTotal) AS stock_qty
FROM siawin14.dbo.SaldosxBodega s
WHERE s.Bodega IN ('23', 'AM')
  AND s.SKU LIKE 'MB%'
GROUP BY s.SKU, s.NombreProducto, s.Bodega, s.NombreBodega
"""

# ── Sync: base tables ─────────────────────────────────────────────────────────

def sync_products(ms_cur, pg):
    log.info("Products: fetching from PC Graf...")
    ms_cur.execute(Q_PRODUCTS)
    rows = ms_cur.fetchall()
    log.info(f"  {len(rows)} rows")

    data = []
    for r in rows:
        sku  = (r.sku  or "").strip()
        name = (r.name or "").strip()
        if not sku or not name:
            continue
        price  = safe_float(r.price_usd)
        # bEstado in PC Graf: 1=active, 0=inactive
        active = (int(r.active) == 1) if r.active is not None else True
        col = derive_collection(name)
        cat = derive_category(name)
        data.append((sku, name, col, cat, price, active))

    with pg.cursor() as cur:
        execute_values(cur, """
            INSERT INTO products (sku, name, collection, category, price_usd, active, synced_at)
            VALUES %s
            ON CONFLICT (sku) DO UPDATE SET
                name        = EXCLUDED.name,
                collection  = EXCLUDED.collection,
                category    = EXCLUDED.category,
                price_usd   = EXCLUDED.price_usd,
                active      = EXCLUDED.active,
                synced_at   = NOW()
        """, data, template="(%s,%s,%s,%s,%s,%s,NOW())")
        pg.commit()

    log.info(f"  {len(data)} products upserted")
    return {r[0]: r for r in data}  # sku -> (sku,name,col,cat,price,active)


def sync_customers(ms_cur, pg):
    log.info("Customers: fetching from PC Graf...")
    ms_cur.execute(Q_CUSTOMERS)
    rows = ms_cur.fetchall()
    log.info(f"  {len(rows)} rows")

    data = []
    for r in rows:
        code = (r.customer_code or "").strip()
        name = (r.name or "").strip()
        if not code or not name:
            continue
        email    = (r.email or "").strip() or None
        phone    = (r.phone or "").strip() or None
        segment  = (r.segment or "").strip() or None
        reg_date = r.registration_date.date() if r.registration_date else None
        data.append((code, name, email, phone, segment, reg_date))

    with pg.cursor() as cur:
        execute_values(cur, """
            INSERT INTO customers (customer_code, name, email, phone, segment, registration_date, synced_at)
            VALUES %s
            ON CONFLICT (customer_code) DO UPDATE SET
                name              = EXCLUDED.name,
                email             = COALESCE(EXCLUDED.email, customers.email),
                phone             = COALESCE(EXCLUDED.phone, customers.phone),
                segment           = EXCLUDED.segment,
                registration_date = EXCLUDED.registration_date,
                synced_at         = NOW()
        """, data, template="(%s,%s,%s,%s,%s,%s,NOW())")
        pg.commit()

    log.info(f"  {len(data)} customers upserted")


def sync_sales(ms_cur, pg, product_map):
    log.info(f"Sales: fetching from PC Graf (from {SALES_FROM})...")
    ms_cur.execute(Q_SALES, SALES_FROM)
    rows = ms_cur.fetchall()
    log.info(f"  {len(rows)} rows")

    seen = {}  # (sale_id, sku) -> row tuple; dedup before upsert
    for r in rows:
        sale_id = (r.sale_id or "").strip()
        sku     = (r.sku     or "").strip()
        if not sale_id or not sku:
            continue

        customer_code = (r.customer_code or "").strip() or None
        customer_name = (r.customer_name or "").strip() or None
        product_name  = (r.product_name  or "").strip() or None
        qty           = safe_float(r.quantity) or 0
        price         = safe_float(r.price_usd) or 0
        line_total    = round(qty * price, 2)

        p   = product_map.get(sku)
        col = p[2] if p else derive_collection(product_name)
        cat = p[3] if p else derive_category(product_name)

        key = (sale_id, sku)
        if key not in seen:
            seen[key] = (sale_id, r.sale_date, customer_code, customer_name,
                         sku, product_name, col, cat, qty, price, line_total)

    data = list(seen.values())

    with pg.cursor() as cur:
        execute_values(cur, """
            INSERT INTO sales (
                sale_id, sale_date, customer_code, customer_name,
                sku, product_name, collection, category,
                quantity, price_usd, line_total_usd, synced_at
            ) VALUES %s
            ON CONFLICT (sale_id, sku) DO UPDATE SET
                sale_date      = EXCLUDED.sale_date,
                customer_code  = EXCLUDED.customer_code,
                customer_name  = EXCLUDED.customer_name,
                product_name   = EXCLUDED.product_name,
                collection     = EXCLUDED.collection,
                category       = EXCLUDED.category,
                quantity       = EXCLUDED.quantity,
                price_usd      = EXCLUDED.price_usd,
                line_total_usd = EXCLUDED.line_total_usd,
                synced_at      = NOW()
        """, data, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())")
        pg.commit()

    log.info(f"  {len(data)} sale lines upserted")


def sync_inventory(ms_cur, pg, product_map):
    log.info("Inventory: fetching from PC Graf (bodegas 23, AM)...")
    ms_cur.execute(Q_INVENTORY)
    rows = ms_cur.fetchall()
    log.info(f"  {len(rows)} rows")

    data = []
    for r in rows:
        sku = (r.sku or "").strip()
        if not sku:
            continue
        product_name = (r.product_name or "").strip() or None
        store_code   = (r.store_code   or "").strip()
        store_name   = (r.store_name   or "").strip() or None
        qty          = safe_float(r.stock_qty) or 0

        p   = product_map.get(sku)
        col = p[2] if p else derive_collection(product_name)
        cat = p[3] if p else derive_category(product_name)

        data.append((sku, product_name, col, cat, store_code, store_name, qty))

    with pg.cursor() as cur:
        cur.execute("DELETE FROM inventory")
        if data:
            execute_values(cur, """
                INSERT INTO inventory (sku, product_name, collection, category,
                                       store_code, store_name, stock_qty, synced_at)
                VALUES %s
            """, data, template="(%s,%s,%s,%s,%s,%s,%s,NOW())")
        pg.commit()

    log.info(f"  {len(data)} inventory rows loaded")

# ── Compute: derived tables ───────────────────────────────────────────────────

def compute_customer_purchases(pg):
    log.info("Computing customer_purchases...")
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE customer_purchases RESTART IDENTITY")
        cur.execute("""
            INSERT INTO customer_purchases
                (customer_code, sku, product_name, collection, category,
                 purchase_date, price_usd, synced_at)
            SELECT DISTINCT ON (customer_code, sku, sale_date::date)
                customer_code, sku, product_name, collection, category,
                sale_date::date, price_usd, NOW()
            FROM sales
            WHERE customer_code IS NOT NULL
              AND customer_code NOT IN ('000001', '000002')
            ORDER BY customer_code, sku, sale_date::date
        """)
        pg.commit()
    log.info(f"  done")


def compute_vip_customers(pg):
    log.info("Computing vip_customers...")
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE vip_customers")
        cur.execute("""
            INSERT INTO vip_customers (
                customer_code, name, email, phone,
                lifetime_value_usd, purchase_count,
                last_purchase_date, first_purchase_date,
                preferred_category, preferred_collection,
                owned_skus, owned_categories,
                vip_tier, synced_at
            )
            WITH customer_totals AS (
                SELECT
                    s.customer_code,
                    SUM(s.line_total_usd)         AS lifetime_value,
                    COUNT(DISTINCT s.sale_id)     AS purchase_count,
                    MAX(s.sale_date::date)         AS last_purchase,
                    MIN(s.sale_date::date)         AS first_purchase
                FROM sales s
                WHERE s.customer_code IS NOT NULL
                  AND s.customer_code NOT IN ('000001', '000002')
                GROUP BY s.customer_code
            ),
            customer_pref_cat AS (
                SELECT DISTINCT ON (customer_code)
                    customer_code, category
                FROM sales
                WHERE customer_code IS NOT NULL AND category IS NOT NULL
                GROUP BY customer_code, category
                ORDER BY customer_code, COUNT(*) DESC
            ),
            customer_pref_col AS (
                SELECT DISTINCT ON (customer_code)
                    customer_code, collection
                FROM sales
                WHERE customer_code IS NOT NULL AND collection IS NOT NULL
                GROUP BY customer_code, collection
                ORDER BY customer_code, COUNT(*) DESC
            ),
            customer_skus AS (
                SELECT customer_code, jsonb_agg(DISTINCT sku) AS owned_skus
                FROM sales WHERE customer_code IS NOT NULL
                GROUP BY customer_code
            ),
            customer_cats AS (
                SELECT
                    customer_code,
                    jsonb_object_agg(category, cnt) AS owned_categories
                FROM (
                    SELECT customer_code, category, COUNT(*) AS cnt
                    FROM sales
                    WHERE customer_code IS NOT NULL AND category IS NOT NULL
                    GROUP BY customer_code, category
                ) sub
                GROUP BY customer_code
            )
            SELECT
                ct.customer_code,
                c.name,
                c.email,
                c.phone,
                ct.lifetime_value,
                ct.purchase_count,
                ct.last_purchase,
                ct.first_purchase,
                pc.category,
                pcol.collection,
                cs.owned_skus,
                cc.owned_categories,
                CASE
                    WHEN ct.lifetime_value >= 5000 THEN 'platinum'
                    WHEN ct.lifetime_value >= 2000 THEN 'gold'
                    WHEN ct.lifetime_value >= 500  THEN 'silver'
                    ELSE 'standard'
                END AS vip_tier,
                NOW()
            FROM customer_totals ct
            JOIN customers c             ON c.customer_code = ct.customer_code
            LEFT JOIN customer_pref_cat  pc    ON pc.customer_code  = ct.customer_code
            LEFT JOIN customer_pref_col  pcol  ON pcol.customer_code = ct.customer_code
            LEFT JOIN customer_skus      cs    ON cs.customer_code  = ct.customer_code
            LEFT JOIN customer_cats      cc    ON cc.customer_code  = ct.customer_code
        """)
        pg.commit()
    log.info("  done")


def compute_daily_summary(pg):
    log.info("Computing daily_summary (last 120 days)...")
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO daily_summary (
                summary_date, revenue_usd, transaction_count, units_sold,
                top_sku, top_product_name, top_collection, top_category,
                revenue_mtd, txn_count_mtd,
                revenue_same_day_lm, revenue_mtd_lm, txn_count_mtd_lm,
                pct_change_revenue, synced_at
            )
            WITH day_base AS (
                SELECT
                    sale_date::date AS d,
                    SUM(line_total_usd)         AS rev,
                    COUNT(DISTINCT sale_id)     AS txns,
                    SUM(quantity)               AS units
                FROM sales
                WHERE sale_date >= CURRENT_DATE - INTERVAL '120 days'
                GROUP BY 1
            ),
            day_top AS (
                SELECT DISTINCT ON (sale_date::date)
                    sale_date::date AS d, sku, product_name, collection, category
                FROM (
                    SELECT sale_date, sku, product_name, collection, category,
                           SUM(line_total_usd) OVER (PARTITION BY sale_date::date, sku) AS sku_rev
                    FROM sales
                    WHERE sale_date >= CURRENT_DATE - INTERVAL '120 days'
                ) sub
                ORDER BY sale_date::date, sku_rev DESC
            ),
            mtd AS (
                SELECT
                    DATE_TRUNC('month', sale_date)::date AS month_start,
                    SUM(line_total_usd)             AS rev_mtd,
                    COUNT(DISTINCT sale_id)         AS txns_mtd
                FROM sales
                WHERE sale_date >= CURRENT_DATE - INTERVAL '120 days'
                GROUP BY 1
            )
            SELECT
                db.d,
                db.rev,
                db.txns,
                db.units,
                dt.sku, dt.product_name, dt.collection, dt.category,
                m.rev_mtd,
                m.txns_mtd,
                (SELECT SUM(line_total_usd) FROM sales
                 WHERE sale_date::date = db.d - INTERVAL '1 month'),
                (SELECT SUM(line_total_usd) FROM sales
                 WHERE sale_date >= DATE_TRUNC('month', db.d - INTERVAL '1 month')
                   AND sale_date::date <= (db.d - INTERVAL '1 month')),
                (SELECT COUNT(DISTINCT sale_id) FROM sales
                 WHERE sale_date >= DATE_TRUNC('month', db.d - INTERVAL '1 month')
                   AND sale_date::date <= (db.d - INTERVAL '1 month')),
                CASE
                    WHEN (SELECT SUM(line_total_usd) FROM sales
                          WHERE sale_date >= DATE_TRUNC('month', db.d - INTERVAL '1 month')
                            AND sale_date::date <= (db.d - INTERVAL '1 month')) > 0
                    THEN ROUND(
                        (m.rev_mtd - (SELECT SUM(line_total_usd) FROM sales
                            WHERE sale_date >= DATE_TRUNC('month', db.d - INTERVAL '1 month')
                              AND sale_date::date <= (db.d - INTERVAL '1 month')))
                        / (SELECT SUM(line_total_usd) FROM sales
                            WHERE sale_date >= DATE_TRUNC('month', db.d - INTERVAL '1 month')
                              AND sale_date::date <= (db.d - INTERVAL '1 month'))
                        * 100, 2)
                    ELSE NULL
                END,
                NOW()
            FROM day_base db
            JOIN day_top dt ON dt.d = db.d
            JOIN mtd m ON m.month_start = DATE_TRUNC('month', db.d)::date
            ON CONFLICT (summary_date) DO UPDATE SET
                revenue_usd         = EXCLUDED.revenue_usd,
                transaction_count   = EXCLUDED.transaction_count,
                units_sold          = EXCLUDED.units_sold,
                top_sku             = EXCLUDED.top_sku,
                top_product_name    = EXCLUDED.top_product_name,
                top_collection      = EXCLUDED.top_collection,
                top_category        = EXCLUDED.top_category,
                revenue_mtd         = EXCLUDED.revenue_mtd,
                txn_count_mtd       = EXCLUDED.txn_count_mtd,
                revenue_same_day_lm = EXCLUDED.revenue_same_day_lm,
                revenue_mtd_lm      = EXCLUDED.revenue_mtd_lm,
                txn_count_mtd_lm    = EXCLUDED.txn_count_mtd_lm,
                pct_change_revenue  = EXCLUDED.pct_change_revenue,
                synced_at           = NOW()
        """)
        pg.commit()
    log.info("  done")


def compute_dead_stock(pg):
    log.info("Computing dead_stock (stock > 0, no sale in 60 days)...")
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE dead_stock")
        cur.execute("""
            INSERT INTO dead_stock (
                sku, product_name, collection, category,
                stock_qty, price_usd, total_value_usd,
                last_sold_date, days_since_sold, synced_at
            )
            SELECT
                i.sku,
                COALESCE(p.name, i.product_name)           AS product_name,
                COALESCE(p.collection, i.collection)       AS collection,
                COALESCE(p.category, i.category)           AS category,
                SUM(i.stock_qty)                           AS stock_qty,
                p.price_usd,
                ROUND(SUM(i.stock_qty) * COALESCE(p.price_usd, 0), 2) AS total_value_usd,
                MAX(s.sale_date::date)                     AS last_sold_date,
                CASE
                    WHEN MAX(s.sale_date) IS NOT NULL
                    THEN (CURRENT_DATE - MAX(s.sale_date::date))
                    ELSE NULL
                END                                        AS days_since_sold,
                NOW()                                      AS synced_at
            FROM inventory i
            LEFT JOIN products p ON p.sku = i.sku
            LEFT JOIN sales s ON s.sku = i.sku
                AND s.sale_date >= CURRENT_DATE - INTERVAL '180 days'
            GROUP BY i.sku, p.name, i.product_name,
                     p.collection, i.collection,
                     p.category, i.category, p.price_usd
            HAVING SUM(i.stock_qty) > 0
               AND (
                   MAX(s.sale_date) IS NULL
                   OR MAX(s.sale_date) < CURRENT_DATE - INTERVAL '60 days'
               )
        """)
        pg.commit()
    log.info("  done")

# ── Post-sync updates ─────────────────────────────────────────────────────────

def update_product_last_sold(pg):
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE products p SET
                last_sold_date  = sub.last_sold,
                days_since_sold = (CURRENT_DATE - sub.last_sold),
                synced_at       = NOW()
            FROM (
                SELECT sku, MAX(sale_date::date) AS last_sold
                FROM sales GROUP BY sku
            ) sub
            WHERE p.sku = sub.sku
        """)
        pg.commit()
    log.info("products.last_sold_date updated")


def update_customer_last_purchase(pg):
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE customers c SET
                last_purchase_date = sub.last_purchase,
                synced_at          = NOW()
            FROM (
                SELECT customer_code, MAX(sale_date::date) AS last_purchase
                FROM sales WHERE customer_code IS NOT NULL
                GROUP BY customer_code
            ) sub
            WHERE c.customer_code = sub.customer_code
        """)
        pg.commit()
    log.info("customers.last_purchase_date updated")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Montblanc ETL starting ===")

    log.info("Connecting to Railway PostgreSQL...")
    pg = pg_connect()

    log.info("Creating schema (if new)...")
    with pg.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        pg.commit()
    log.info("  Schema ready")

    log.info("Connecting to PC Graf (SQL Server via VPN)...")
    ms     = mssql_connect()
    ms_cur = ms.cursor()

    try:
        product_map = sync_products(ms_cur, pg)
        sync_customers(ms_cur, pg)
        sync_sales(ms_cur, pg, product_map)
        sync_inventory(ms_cur, pg, product_map)
    finally:
        ms_cur.close()
        ms.close()
        log.info("PC Graf connection closed")

    update_product_last_sold(pg)
    update_customer_last_purchase(pg)
    compute_customer_purchases(pg)
    compute_vip_customers(pg)
    compute_daily_summary(pg)
    compute_dead_stock(pg)

    pg.close()
    log.info("=== Montblanc ETL complete ===")


if __name__ == "__main__":
    main()
