import os
import time
import math
import json
import requests
import pyodbc
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import defaultdict
from dotenv import load_dotenv, dotenv_values

load_dotenv(override=False)
DOTENV_VALUES = dotenv_values(".env")

def dbg(msg: str):
    if DEBUG:
        print(msg)

def preview_text(s: str, n: int = 500) -> str:
    s = s or ""
    s = s.replace("\r", " ").replace("\n", " ")
    return s[:n] + ("..." if len(s) > n else "")

def normalize_handle(s: str) -> str:
    return (s or "").strip().upper()

def load_sync_cache(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def save_sync_cache(path: str, cache: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)

def read_env(key: str, default=None):
    """
    Match original behavior: prefer current process environment first,
    then fall back to .env file values.
    """
    env_val = os.getenv(key)
    if env_val is not None and str(env_val).strip() != "":
        return str(env_val).strip()
    val = DOTENV_VALUES.get(key)
    if val is not None and str(val).strip() != "":
        return str(val).strip()
    return default

DEBUG = read_env("DEBUG", "1") == "1"
DEBUG_SAMPLE_SIZE = int(read_env("DEBUG_SAMPLE_SIZE", "10"))
SHOPIFY_DELAY_SECONDS = float(read_env("SHOPIFY_DELAY_SECONDS", "0.35"))
PROGRESS_EVERY = int(read_env("PROGRESS_EVERY", "25"))
MAX_UPDATES = int(read_env("MAX_UPDATES", "0"))
DRY_RUN = read_env("DRY_RUN", "0") == "1"
ONLY_CHANGED = read_env("ONLY_CHANGED", "0") == "1"
SYNC_CACHE_PATH = read_env("SYNC_CACHE_PATH", "./inventory_sync_cache.json")

ONLY_HANDLE = (read_env("ONLY_HANDLE", "") or "").strip()
ONLY_SKU = (read_env("ONLY_SKU", "") or "").strip()
PRINT_LOCATIONS = read_env("PRINT_LOCATIONS", "0") == "1"
PRINT_LOCATION_DIRECTORY = read_env("PRINT_LOCATION_DIRECTORY", "0") == "1"
DEBUG_SKU = (read_env("DEBUG_SKU", "") or "").strip()
PRINT_GLOBAL_TOTALS = read_env("PRINT_GLOBAL_TOTALS", "0") == "1"

def parse_bodega_list(raw: str):
    if not raw:
        return []
    parts = []
    for p in raw.split(","):
        p = p.strip().upper()
        if p:
            parts.append(p)
    return parts

def validate_bodega_code(code: str) -> bool:
    if not code:
        return False
    for ch in code:
        if not (ch.isdigit() or ch.isalpha()):
            return False
    return True

SHOP = read_env("SHOPIFY_SHOP") or read_env("SHOP")
TOKEN = read_env("SHOPIFY_TOKEN")
API_VERSION = read_env("SHOPIFY_API_VERSION", "2024-10")

if not SHOP or not TOKEN:
    raise ValueError("Missing SHOPIFY_SHOP/SHOP or SHOPIFY_TOKEN in .env")

SQL_SERVER = read_env("SQL_SERVER")
SQL_DB = read_env("SQL_DB")
SQL_USER = read_env("SQL_USER")
SQL_PASSWORD = read_env("SQL_PASSWORD")
SQL_DRIVER = read_env("SQL_DRIVER") or "ODBC Driver 18 for SQL Server"

BODEGA_LOCATION_MAP_PATH = read_env("BODEGA_LOCATION_MAP_PATH", "./bodega_location_map.json")
SELLABLE_BODEGAS_RAW = read_env("SELLABLE_BODEGAS", "")

SQL_QUERY_MAIN_TEMPLATE = """
SELECT
  CodigoProducto AS SKU,
  CodigoBodega,
  SUM(StockDisponible) AS StockDisponible,
  SUM(SaldoApartado) AS SaldoApartado,
  CASE
    WHEN SUM(StockDisponible) - SUM(SaldoApartado) < 0 THEN 0
    ELSE SUM(StockDisponible) - SUM(SaldoApartado)
  END AS DisponibleParaWeb
FROM [siawin14].[dbo].[vw_Web_Stock]
WHERE CodigoBodega IN ({bodegas_in})
{sku_filter}
GROUP BY
  CodigoProducto,
  CodigoBodega
ORDER BY
  DisponibleParaWeb DESC;
"""

SQL_QUERY_PRICE_TEMPLATE = """
SELECT
  CodigoProducto AS SKU,
  MAX(NombreProducto) AS NombreProducto,
  MAX([PrecioSinIVA$]) * 1.13 AS PrecioBase,
  MIN([PrecioSinIVA$]) * 1.13 AS PrecioMin,
  MAX([PrecioSinIVA$]) * 1.13 AS PrecioMax,
  COUNT(DISTINCT [PrecioSinIVA$]) AS DistinctPriceCount,
  MAX(DescuentoActivo) AS DescuentoActivo,
  MAX(VenceDescuento) AS VenceDescuento
FROM [siawin14].[dbo].[vw_Web_Stock]
WHERE CodigoBodega IN ({bodegas_in})
  AND [PrecioSinIVA$] IS NOT NULL
  {sku_filter}
GROUP BY CodigoProducto
ORDER BY CodigoProducto;
"""

SQL_QUERY_LOCATION_DIRECTORY = """
SELECT DISTINCT
  CodigoBodega,
  NombreBodega,
  CodigoTienda,
  NombreTienda
FROM [siawin14].[dbo].[vw_Web_Stock]
ORDER BY
  NombreTienda,
  CodigoBodega;
"""

SQL_QUERY_DEBUG_SKU = """
SELECT
  CodigoProducto,
  NombreProducto,
  CodigoTienda,
  NombreTienda,
  CodigoBodega,
  NombreBodega,
  StockDisponible,
  SaldoApartado,
  (StockDisponible - SaldoApartado) AS DisponibleParaWeb
FROM [siawin14].[dbo].[vw_Web_Stock]
WHERE CodigoProducto = ?
ORDER BY
  CodigoTienda, CodigoBodega;
"""

SQL_QUERY_GLOBAL_TOTALS = """
SELECT
  CodigoProducto AS SKU,
  MAX(NombreProducto) AS NombreProducto,
  CASE
    WHEN SUM(StockDisponible) - SUM(SaldoApartado) < 0 THEN 0
    ELSE SUM(StockDisponible) - SUM(SaldoApartado)
  END AS TotalDisponibleParaWeb
FROM [siawin14].[dbo].[vw_Web_Stock]
GROUP BY CodigoProducto
ORDER BY TotalDisponibleParaWeb DESC;
"""

BASE_URL = f"https://{SHOP}/admin/api/{API_VERSION}"
HEADERS = {
    "X-Shopify-Access-Token": TOKEN,
    "Content-Type": "application/json",
}

def sql_connect():
    conn_str = (
        f"DRIVER={{{SQL_DRIVER}}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DB};"
        f"UID={SQL_USER};"
        f"PWD={SQL_PASSWORD};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=30)

def sql_quote_list(values):
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)

def build_sql_main_query(sellable_bodegas, only_sku):
    bodegas_in = sql_quote_list(sellable_bodegas)
    sku_filter = "AND CodigoProducto = ?" if only_sku else ""
    return SQL_QUERY_MAIN_TEMPLATE.format(bodegas_in=bodegas_in, sku_filter=sku_filter)

def build_sql_price_query(sellable_bodegas, only_sku):
    bodegas_in = sql_quote_list(sellable_bodegas)
    sku_filter = "AND CodigoProducto = ?" if only_sku else ""
    return SQL_QUERY_PRICE_TEMPLATE.format(bodegas_in=bodegas_in, sku_filter=sku_filter)

def sql_fetch_inventory(sellable_bodegas, only_sku=None):
    if not sellable_bodegas:
        raise ValueError("SELLABLE_BODEGAS is empty; cannot query inventory.")

    query = build_sql_main_query(sellable_bodegas, only_sku)
    cn = sql_connect()
    cur = cn.cursor()
    if only_sku:
        cur.execute(query, only_sku)
    else:
        cur.execute(query)

    results = defaultdict(dict)
    sample_rows = []
    for row in cur.fetchall():
        sku = normalize_handle(row.SKU)
        bodega = normalize_handle(row.CodigoBodega)
        qty = row.DisponibleParaWeb
        if qty is None:
            qty = 0
        qty_int = int(math.floor(float(qty)))
        if qty_int < 0:
            qty_int = 0
        results[sku][bodega] = qty_int
        if len(sample_rows) < DEBUG_SAMPLE_SIZE:
            sample_rows.append((sku, bodega, qty_int))

    cur.close()
    cn.close()

    dbg(f"   Debug SQL SKUs: {len(results)}")
    if sample_rows:
        dbg(f"   Debug SQL sample sku->bodega->qty: {sample_rows}")
    return results

def format_shopify_price(value) -> str | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError):
        return None
    if price < 0:
        price = Decimal("0.00")
    return format(price, ".2f")

def sql_fetch_prices(sellable_bodegas, only_sku=None):
    if not sellable_bodegas:
        raise ValueError("SELLABLE_BODEGAS is empty; cannot query prices.")

    query = build_sql_price_query(sellable_bodegas, only_sku)
    cn = sql_connect()
    cur = cn.cursor()
    if only_sku:
        cur.execute(query, only_sku)
    else:
        cur.execute(query)

    results = {}
    sample_rows = []
    inconsistent_rows = []
    for row in cur.fetchall():
        sku = normalize_handle(row.SKU)
        price = format_shopify_price(row.PrecioBase)
        if price is None:
            continue
        results[sku] = price
        distinct_count = int(row.DistinctPriceCount or 0)
        if distinct_count > 1 and len(inconsistent_rows) < DEBUG_SAMPLE_SIZE:
            inconsistent_rows.append(
                (
                    sku,
                    row.NombreProducto,
                    format_shopify_price(row.PrecioMin),
                    format_shopify_price(row.PrecioMax),
                    distinct_count,
                )
            )
        if len(sample_rows) < DEBUG_SAMPLE_SIZE:
            sample_rows.append((sku, price))

    cur.close()
    cn.close()

    dbg(f"   Debug SQL price SKUs: {len(results)}")
    if sample_rows:
        dbg(f"   Debug SQL sample sku->price: {sample_rows}")
    if inconsistent_rows:
        print(
            "   [WARN] SQL price mismatches detected for some SKUs; "
            "using MAX([PrecioSinIVA$])."
        )
        for sku, name, price_min, price_max, distinct_count in inconsistent_rows:
            print(
                f"   - sku={sku} producto={name} "
                f"price_min={price_min} price_max={price_max} "
                f"distinct_prices={distinct_count}"
            )
    return results

def sql_fetch_location_directory():
    cn = sql_connect()
    cur = cn.cursor()
    cur.execute(SQL_QUERY_LOCATION_DIRECTORY)
    rows = cur.fetchall()
    cur.close()
    cn.close()
    return rows

def sql_fetch_debug_sku(sku: str):
    cn = sql_connect()
    cur = cn.cursor()
    cur.execute(SQL_QUERY_DEBUG_SKU, sku)
    rows = cur.fetchall()
    cur.close()
    cn.close()
    return rows

def sql_fetch_global_totals():
    cn = sql_connect()
    cur = cn.cursor()
    cur.execute(SQL_QUERY_GLOBAL_TOTALS)
    rows = cur.fetchall()
    cur.close()
    cn.close()
    return rows

def shopify_fetch_locations():
    url = f"{BASE_URL}/locations.json"
    r = requests.get(url, headers=HEADERS, timeout=60)
    dbg(f"[Shopify] Locations status: {r.status_code}")
    if r.status_code != 200:
        dbg(f"[Shopify] Body preview: {preview_text(r.text)}")
    r.raise_for_status()
    return r.json().get("locations", [])

def shopify_fetch_variants_sku_to_records(sql_sku_filter=None):
    """
    Build map: SKU -> list[{variant_id, inventory_item_id}]
    Fetch via /products.json (stable), then extract variants.
    """
    sku_map = defaultdict(list)
    page = 0
    total_products = 0
    total_variants = 0
    skipped_variants_not_in_sql = 0
    blank_sku = 0
    missing_variant_id = 0
    missing_inv_item = 0
    sample_variants = []
    logged_variant_keys = False

    url = f"{BASE_URL}/products.json?limit=250&status=active&fields=id,handle,variants"
    while url:
        page += 1
        dbg(f"[Shopify] GET page {page}: {url}")

        r = requests.get(url, headers=HEADERS, timeout=60)
        dbg(f"[Shopify] Status: {r.status_code}")
        if r.status_code != 200:
            dbg(f"[Shopify] Body preview: {preview_text(r.text)}")
        r.raise_for_status()

        payload = r.json()
        products = payload.get("products", [])
        dbg(f"[Shopify] Products returned this page: {len(products)}")
        total_products += len(products)

        variants_this_page = 0
        with_sku = 0

        for p in products:
            handle = normalize_handle(p.get("handle") or "")
            for v in (p.get("variants") or []):
                variants_this_page += 1
                total_variants += 1
                sku = normalize_handle(v.get("sku") or "")
                variant_id = v.get("id")
                inv_item_id = v.get("inventory_item_id")
                if not logged_variant_keys:
                    dbg(f"[Shopify] Variant keys sample: {list(v.keys())}")
                    logged_variant_keys = True
                if len(sample_variants) < DEBUG_SAMPLE_SIZE:
                    sample_variants.append(
                        {
                            "id": variant_id,
                            "handle": handle,
                            "sku": sku,
                            "barcode": v.get("barcode"),
                            "price": v.get("price"),
                            "compare_at_price": v.get("compare_at_price"),
                            "inventory_item_id": inv_item_id,
                        }
                    )
                if not sku:
                    blank_sku += 1
                    continue
                if sql_sku_filter is not None and sku not in sql_sku_filter:
                    skipped_variants_not_in_sql += 1
                    continue
                if not variant_id:
                    missing_variant_id += 1
                    continue
                existing_variant_ids = {
                    item["variant_id"] for item in sku_map[sku]
                }
                if variant_id not in existing_variant_ids:
                    sku_map[sku].append(
                        {
                            "variant_id": variant_id,
                            "inventory_item_id": inv_item_id,
                        }
                    )
                    with_sku += 1
                if not inv_item_id:
                    missing_inv_item += 1

        dbg(
            "[Shopify] Variants this page: "
            f"{variants_this_page}, variants w/ sku+variant: {with_sku}"
        )
        if with_sku > 0:
            sample = list(sku_map.keys())[:DEBUG_SAMPLE_SIZE]
            dbg(f"[Shopify] Sample SKUs so far: {sample}")

        link = r.headers.get("Link", "")
        next_url = None
        if 'rel="next"' in link:
            parts = link.split(",")
            for p in parts:
                if 'rel="next"' in p:
                    next_url = p[p.find("<")+1 : p.find(">")]
        url = next_url

        time.sleep(0.3)

    dbg(f"[Shopify] TOTAL variants seen: {total_variants}")
    dbg(f"[Shopify] TOTAL products seen: {total_products}")
    if sql_sku_filter is not None:
        dbg(
            "[Shopify] Variants skipped (SKU not in SQL set): "
            f"{skipped_variants_not_in_sql}"
        )
    dbg(f"[Shopify] TOTAL unique SKUs mapped: {len(sku_map)}")
    dbg(
        "[Shopify] Blank SKU count: "
        f"{blank_sku}, "
        f"missing variant_id: {missing_variant_id}, "
        f"missing inventory_item_id: {missing_inv_item}"
    )
    if sample_variants:
        dbg(f"[Shopify] Sample variants: {sample_variants}")

    return sku_map

def shopify_post(url, payload):
    r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        dbg(f"[Shopify] Status: {r.status_code}")
        dbg(f"[Shopify] Body preview: {preview_text(r.text)}")
    r.raise_for_status()
    return r.json()

def shopify_put(url, payload):
    r = requests.put(url, headers=HEADERS, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        dbg(f"[Shopify] Status: {r.status_code}")
        dbg(f"[Shopify] Body preview: {preview_text(r.text)}")
    r.raise_for_status()
    return r.json()

def shopify_set_inventory(location_id: int, inventory_item_id: int, available: int):
    url = f"{BASE_URL}/inventory_levels/set.json"
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": available
    }
    return shopify_post(url, payload)

def shopify_set_variant_price(variant_id: int, price: str):
    url = f"{BASE_URL}/variants/{variant_id}.json"
    payload = {
        "variant": {
            "id": variant_id,
            "price": price,
        }
    }
    return shopify_put(url, payload)

def shopify_connect_inventory(location_id: int, inventory_item_id: int):
    url = f"{BASE_URL}/inventory_levels/connect.json"
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
    }
    return shopify_post(url, payload)

def shopify_set_inventory_safe(location_id: int, inventory_item_id: int, available: int):
    try:
        return shopify_set_inventory(location_id, inventory_item_id, available)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        status = resp.status_code if resp is not None else None
        body = resp.text if resp is not None else ""
        msg = (body or "").lower()
        needs_connect = any(
            key in msg
            for key in [
                "not stocked",
                "does not have inventory level",
                "inventory level",
                "inventory item",
                "location",
            ]
        )
        if status in (400, 404, 422) and needs_connect:
            dbg(
                "[Shopify] inventory set failed; trying connect then retry "
                f"(status {status})"
            )
            try:
                shopify_connect_inventory(location_id, inventory_item_id)
                if SHOPIFY_DELAY_SECONDS > 0:
                    time.sleep(SHOPIFY_DELAY_SECONDS)
                return shopify_set_inventory(location_id, inventory_item_id, available)
            except requests.HTTPError:
                return None
        return None

def load_bodega_location_map(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"BODEGA_LOCATION_MAP_PATH not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for k, v in (data or {}).items():
        key = normalize_handle(str(k))
        if not validate_bodega_code(key):
            dbg(f"[WARN] Invalid bodega code in map: {k}")
            continue
        try:
            mapping[key] = int(v)
        except (TypeError, ValueError):
            dbg(f"[WARN] Invalid location id for bodega {k}: {v}")
            continue
    return mapping

def resolve_sellable_bodegas(bodega_location_map):
    sellable = parse_bodega_list(SELLABLE_BODEGAS_RAW)
    if not sellable:
        sellable = list(bodega_location_map.keys())
    valid = []
    for code in sellable:
        if validate_bodega_code(code):
            valid.append(code)
        else:
            dbg(f"[WARN] Invalid bodega code skipped: {code}")
    return sorted(set(valid))

def resolve_target_handle():
    only_handle = normalize_handle(ONLY_HANDLE)
    only_sku = normalize_handle(ONLY_SKU)

    if only_handle in ("ALL", "*"):
        only_handle = ""
    if only_sku in ("ALL", "*"):
        only_sku = ""

    if only_handle and only_sku and only_handle != only_sku:
        dbg(
            "[WARN] ONLY_HANDLE and ONLY_SKU differ; ONLY_SKU will be used."
        )
    if only_sku:
        return only_sku
    if only_handle:
        return only_handle
    return ""

def print_location_directory(rows):
    print("   Location directory rows:")
    sample = rows[:DEBUG_SAMPLE_SIZE]
    for r in sample:
        print(
            f"   - CodigoBodega={r.CodigoBodega} "
            f"NombreBodega={r.NombreBodega} "
            f"CodigoTienda={r.CodigoTienda} "
            f"NombreTienda={r.NombreTienda}"
        )
    if len(rows) > len(sample):
        print(f"   (showing {len(sample)} of {len(rows)})")

def print_debug_sku(rows):
    print("   Debug SKU rows:")
    sample = rows[:DEBUG_SAMPLE_SIZE]
    for r in sample:
        print(
            f"   - SKU={r.CodigoProducto} "
            f"Producto={r.NombreProducto} "
            f"Tienda={r.NombreTienda} "
            f"Bodega={r.CodigoBodega} "
            f"Stock={r.StockDisponible} "
            f"Apartado={r.SaldoApartado} "
            f"Disponible={r.DisponibleParaWeb}"
        )
    if len(rows) > len(sample):
        print(f"   (showing {len(sample)} of {len(rows)})")

def print_global_totals(rows):
    print("   Global totals rows:")
    sample = rows[:DEBUG_SAMPLE_SIZE]
    for r in sample:
        print(
            f"   - SKU={r.SKU} "
            f"Producto={r.NombreProducto} "
            f"TotalDisponible={r.TotalDisponibleParaWeb}"
        )
    if len(rows) > len(sample):
        print(f"   (showing {len(sample)} of {len(rows)})")

def main():
    print("0) Loading bodega/location mapping...")
    bodega_location_map = load_bodega_location_map(BODEGA_LOCATION_MAP_PATH)
    sellable_bodegas = resolve_sellable_bodegas(bodega_location_map)
    print(f"   Bodegas in mapping file: {len(bodega_location_map)}")
    print(f"   Sellable bodegas: {sellable_bodegas}")
    if ONLY_CHANGED:
        print(f"   ONLY_CHANGED enabled. Cache: {SYNC_CACHE_PATH}")

    target_handle = resolve_target_handle()
    if target_handle:
        print(f"   ONLY SKU filter active: {target_handle}")

    if PRINT_LOCATIONS:
        print("0b) Fetching Shopify locations...")
        locations = shopify_fetch_locations()
        for loc in locations[:DEBUG_SAMPLE_SIZE]:
            print(f"   - id={loc.get('id')} name={loc.get('name')}")
        if len(locations) > DEBUG_SAMPLE_SIZE:
            print(f"   (showing {DEBUG_SAMPLE_SIZE} of {len(locations)})")

    if PRINT_LOCATION_DIRECTORY:
        print("0c) SQL location directory...")
        rows = sql_fetch_location_directory()
        print_location_directory(rows)

    if DEBUG_SKU:
        print(f"0d) SQL debug for SKU={DEBUG_SKU}...")
        rows = sql_fetch_debug_sku(DEBUG_SKU)
        print_debug_sku(rows)

    if PRINT_GLOBAL_TOTALS:
        print("0e) SQL global totals...")
        rows = sql_fetch_global_totals()
        print_global_totals(rows)

    print("1) Fetching SQL inventory per bodega...")
    sql_inventory = sql_fetch_inventory(sellable_bodegas, only_sku=target_handle or None)
    if target_handle:
        if target_handle in sql_inventory:
            sql_inventory = {target_handle: sql_inventory[target_handle]}
        else:
            print("   [WARN] ONLY handle not found in SQL results.")
            sql_inventory = {}
    print(f"   SQL SKUs returned: {len(sql_inventory)}")
    if len(sql_inventory) == 0:
        print("   No SQL SKUs to sync. Exiting.")
        return

    sql_skus = set(sql_inventory.keys())

    print("2) Fetching SQL prices per SKU...")
    sql_prices = sql_fetch_prices(sellable_bodegas, only_sku=target_handle or None)
    if target_handle:
        if target_handle in sql_prices:
            sql_prices = {target_handle: sql_prices[target_handle]}
        else:
            print("   [WARN] ONLY handle not found in SQL price results.")
            sql_prices = {}
    print(f"   SQL price SKUs returned: {len(sql_prices)}")

    print("3) Fetching Shopify ACTIVE variants (filtered by SQL SKUs)...")
    sku_to_items = shopify_fetch_variants_sku_to_records(
        sql_sku_filter=sql_skus
    )
    print(f"   Shopify SKUs mapped: {len(sku_to_items)}")
    if len(sku_to_items) == 0:
        print("   [DEBUG] Shopify returned ZERO matching active SKUs.")
        return

    if target_handle:
        if target_handle in sku_to_items:
            sku_to_items = {target_handle: sku_to_items[target_handle]}
        else:
            print("   [WARN] ONLY SKU not found in Shopify results.")
            sku_to_items = {}

    shop_skus = set(sku_to_items.keys())

    print("4) Intersection stats...")
    inter = shop_skus & sql_skus
    only_shop = shop_skus - sql_skus
    only_sql = sql_skus - shop_skus
    price_missing_in_sql = inter - set(sql_prices.keys())

    dbg(f"   [DEBUG] Sample Shopify SKUs: {list(shop_skus)[:DEBUG_SAMPLE_SIZE]}")
    dbg(f"   [DEBUG] Sample SQL SKUs: {list(sql_skus)[:DEBUG_SAMPLE_SIZE]}")
    print(f"   SKUs in BOTH: {len(inter)}")
    print(f"   SKUs only in Shopify: {len(only_shop)}")
    print(f"   SKUs only in SQL: {len(only_sql)}")
    if price_missing_in_sql:
        print(
            "   [WARN] SKUs missing SQL price rows: "
            f"{len(price_missing_in_sql)}"
        )
    dbg(f"   [DEBUG] Example only-Shopify: {list(only_shop)[:DEBUG_SAMPLE_SIZE]}")
    dbg(f"   [DEBUG] Example only-SQL: {list(only_sql)[:DEBUG_SAMPLE_SIZE]}")
    dbg(
        f"   [DEBUG] Example missing-price SKUs: "
        f"{list(price_missing_in_sql)[:DEBUG_SAMPLE_SIZE]}"
    )

    planned_inventory_samples = []
    planned_price_samples = []
    total_inventory_planned = 0
    total_price_planned = 0
    missing_location = 0
    missing_inventory_item = 0
    missing_variant_id = 0
    multi_variant_skus = 0
    cache = load_sync_cache(SYNC_CACHE_PATH) if ONLY_CHANGED else {}
    cache_skips = 0
    for sku in inter:
        variants = sku_to_items.get(sku, [])
        if len(variants) > 1:
            multi_variant_skus += 1
        price = sql_prices.get(sku)
        if price is not None:
            for variant in variants:
                variant_id = variant.get("variant_id")
                if not variant_id:
                    missing_variant_id += 1
                    continue
                price_cache_key = f"price:{variant_id}"
                if ONLY_CHANGED and cache.get(price_cache_key) == price:
                    cache_skips += 1
                    continue
                total_price_planned += 1
                if len(planned_price_samples) < DEBUG_SAMPLE_SIZE:
                    planned_price_samples.append((sku, variant_id, price))
        for bodega, qty in (sql_inventory.get(sku) or {}).items():
            location_id = bodega_location_map.get(bodega)
            if not location_id:
                missing_location += 1
                continue
            for variant in variants:
                inv_item_id = variant.get("inventory_item_id")
                if not inv_item_id:
                    missing_inventory_item += 1
                    continue
                cache_key = f"{inv_item_id}:{location_id}"
                if ONLY_CHANGED and str(cache.get(cache_key)) == str(qty):
                    cache_skips += 1
                    continue
                total_inventory_planned += 1
                if len(planned_inventory_samples) < DEBUG_SAMPLE_SIZE:
                    planned_inventory_samples.append(
                        (sku, bodega, location_id, inv_item_id, qty)
                    )

    total_planned = total_price_planned + total_inventory_planned
    print(f"   Planned price update calls: {total_price_planned}")
    print(f"   Planned inventory set calls: {total_inventory_planned}")
    print(f"   Planned total Shopify calls: {total_planned}")
    if multi_variant_skus:
        print(
            "   [WARN] SKUs with multiple variants: "
            f"{multi_variant_skus} (qty will be applied to each variant)"
        )
    if missing_location:
        print(
            "   [WARN] Bodegas missing location_id mapping: "
            f"{missing_location} (will be skipped)"
        )
    if missing_inventory_item:
        print(
            "   [WARN] Variants missing inventory_item_id: "
            f"{missing_inventory_item} inventory updates will be skipped"
        )
    if missing_variant_id:
        print(
            "   [WARN] Variants missing variant_id: "
            f"{missing_variant_id} price updates will be skipped"
        )
    if planned_price_samples:
        print("   Sample planned price updates:")
        for s in planned_price_samples:
            print(f"   - sku={s[0]} variant_id={s[1]} price={s[2]}")
    if planned_inventory_samples:
        print("   Sample planned inventory updates:")
        for s in planned_inventory_samples:
            print(
                f"   - sku={s[0]} bodega={s[1]} "
                f"location_id={s[2]} inv_item_id={s[3]} qty={s[4]}"
            )
    if ONLY_CHANGED:
        print(f"   Cache skips (unchanged): {cache_skips}")

    print("5) Updating Shopify prices and inventory...")
    updates = 0
    price_updates = 0
    inventory_updates = 0
    errors = 0
    price_errors = 0
    inventory_errors = 0
    price_skipped_missing = 0
    inventory_skipped_missing_item = 0
    skipped_missing = 0
    skipped_unchanged = 0
    start_time = time.time()
    stop = False

    for sku in inter:
        if stop:
            break
        variants = sku_to_items.get(sku, [])
        price = sql_prices.get(sku)
        if price is None and DEBUG:
            dbg(f"[WARN] Missing SQL price for sku {sku}")
        if price is not None:
            for variant in variants:
                if stop:
                    break
                variant_id = variant.get("variant_id")
                if not variant_id:
                    price_skipped_missing += 1
                    continue
                price_cache_key = f"price:{variant_id}"
                if ONLY_CHANGED and cache.get(price_cache_key) == price:
                    skipped_unchanged += 1
                    continue
                if DRY_RUN:
                    print(
                        f"   [DRY_RUN] price sku={sku} "
                        f"variant_id={variant_id} price={price}"
                    )
                else:
                    try:
                        shopify_set_variant_price(variant_id, price)
                    except requests.RequestException as e:
                        errors += 1
                        price_errors += 1
                        print(
                            f"   [ERROR] price sku={sku} "
                            f"variant_id={variant_id} price={price} err={e}"
                        )
                    else:
                        price_updates += 1
                        if ONLY_CHANGED:
                            cache[price_cache_key] = price

                updates += 1
                if DRY_RUN:
                    price_updates += 1
                if SHOPIFY_DELAY_SECONDS > 0 and not DRY_RUN:
                    time.sleep(SHOPIFY_DELAY_SECONDS)
                if PROGRESS_EVERY > 0 and updates % PROGRESS_EVERY == 0:
                    elapsed = time.time() - start_time
                    rate = updates / elapsed if elapsed > 0 else 0
                    remaining = total_planned - updates
                    eta = remaining / rate if rate > 0 else 0
                    print(
                        f"   Progress: {updates}/{total_planned} "
                        f"({rate:.2f}/s, ETA {eta/60:.1f} min)"
                    )
                if MAX_UPDATES > 0 and updates >= MAX_UPDATES:
                    print("   Stopping early due to MAX_UPDATES limit.")
                    stop = True
                    break
        for bodega, qty in (sql_inventory.get(sku) or {}).items():
            if stop:
                break
            location_id = bodega_location_map.get(bodega)
            if not location_id:
                skipped_missing += 1
                if DEBUG:
                    dbg(f"[WARN] Missing location_id for bodega {bodega}")
                continue
            for variant in variants:
                inv_item_id = variant.get("inventory_item_id")
                if not inv_item_id:
                    inventory_skipped_missing_item += 1
                    continue
                cache_key = f"{inv_item_id}:{location_id}"
                if ONLY_CHANGED and str(cache.get(cache_key)) == str(qty):
                    skipped_unchanged += 1
                    continue
                if DRY_RUN:
                    print(
                        f"   [DRY_RUN] sku={sku} bodega={bodega} "
                        f"location_id={location_id} inv_item_id={inv_item_id} qty={qty}"
                    )
                else:
                    try:
                        result = shopify_set_inventory_safe(
                            location_id, inv_item_id, qty
                        )
                        if result is None:
                            errors += 1
                            inventory_errors += 1
                        else:
                            inventory_updates += 1
                            if ONLY_CHANGED:
                                cache[cache_key] = qty
                    except requests.RequestException as e:
                        errors += 1
                        inventory_errors += 1
                        print(
                            f"   [ERROR] sku={sku} bodega={bodega} "
                            f"location_id={location_id} inv_item_id={inv_item_id} "
                            f"qty={qty} err={e}"
                        )

                updates += 1
                if DRY_RUN:
                    inventory_updates += 1
                if SHOPIFY_DELAY_SECONDS > 0 and not DRY_RUN:
                    time.sleep(SHOPIFY_DELAY_SECONDS)

                if PROGRESS_EVERY > 0 and updates % PROGRESS_EVERY == 0:
                    elapsed = time.time() - start_time
                    rate = updates / elapsed if elapsed > 0 else 0
                    remaining = total_planned - updates
                    eta = remaining / rate if rate > 0 else 0
                    print(
                        f"   Progress: {updates}/{total_planned} "
                        f"({rate:.2f}/s, ETA {eta/60:.1f} min)"
                    )

                if MAX_UPDATES > 0 and updates >= MAX_UPDATES:
                    print("   Stopping early due to MAX_UPDATES limit.")
                    stop = True
                    break

    if ONLY_CHANGED and not DRY_RUN:
        save_sync_cache(SYNC_CACHE_PATH, cache)

    if DRY_RUN:
        print(
            f"Done (DRY_RUN). Planned calls: {updates}, "
            f"price calls: {price_updates}, inventory calls: {inventory_updates}"
        )
    else:
        print(
            f"Done. Calls attempted: {updates}, price updated: {price_updates}, "
            f"inventory updated: {inventory_updates}, errors: {errors}, "
            f"price errors: {price_errors}, inventory errors: {inventory_errors}, "
            f"price skipped missing variant_id: {price_skipped_missing}, "
            f"inventory skipped missing inventory_item_id: {inventory_skipped_missing_item}, "
            f"missing location mapping: {skipped_missing}, "
            f"unchanged skipped: {skipped_unchanged}"
        )

if __name__ == "__main__":
    main()
