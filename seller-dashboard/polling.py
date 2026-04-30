"""SP-API から Orders / Inventory / Returns / Financial Events を取得して SQLite に保存"""
import csv
import io
import json
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# amazon-seller-automation の scripts をパスに追加（sp_api_client 等の流用）
_PROJECT_ROOT = Path(__file__).resolve().parent
_ASA_ROOT = _PROJECT_ROOT.parent / "amazon-seller-automation"
sys.path.insert(0, str(_ASA_ROOT))

from sp_api.api import Orders, Reports, Products, Finances, Inventories, CatalogItemsV20220401  # noqa: E402
from sp_api.api.listings_items.listings_items_2021_08_01 import (  # noqa: E402
    ListingsItemsV20210801,
)
from sp_api.base import Marketplaces  # noqa: E402
from sp_api.base.exceptions import SellingApiException  # noqa: E402

from scripts.common.sp_api_client import get_client, get_seller_id  # noqa: E402

from config import MARKETPLACE_ID, SPREADSHEET_ID, GOOGLE_CREDS_PATH, LOGS_DIR, SHOP_KEY  # noqa: E402
from db import get_db  # noqa: E402

SHOP = SHOP_KEY
JST = timezone(timedelta(hours=9))
RETURNS_REPORT_TYPE = "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA"


# ================================================================
# Orders
# ================================================================
def sync_orders(days: int = 60) -> int:
    """SP-API Orders を取得して DB に upsert。戻り値 = 取得件数"""
    client = get_client(SHOP, Orders)
    created_after = (
        (datetime.now(JST) - timedelta(days=days))
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    all_orders = []
    next_token = None
    while True:
        kwargs = {"CreatedAfter": created_after}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = client.get_orders(**kwargs)
        payload = resp.payload or {}
        all_orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        if not next_token:
            break

    now_iso = datetime.utcnow().isoformat()
    with get_db() as conn:
        for o in all_orders:
            order_id = o.get("AmazonOrderId")
            conn.execute("""
                INSERT INTO orders(amazon_order_id, purchase_date, order_status,
                                   fulfillment_channel, marketplace_id, item_price_total,
                                   shipping_price, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(amazon_order_id) DO UPDATE SET
                    order_status=excluded.order_status,
                    fulfillment_channel=excluded.fulfillment_channel,
                    item_price_total=excluded.item_price_total,
                    updated_at=excluded.updated_at
            """, (
                order_id,
                o.get("PurchaseDate"),
                o.get("OrderStatus"),
                o.get("FulfillmentChannel"),
                MARKETPLACE_ID,
                float(o.get("OrderTotal", {}).get("Amount", 0)) if o.get("OrderTotal") else 0,
                0,
                now_iso,
            ))

            # 注文商品も取得
            try:
                items_resp = client.get_order_items(order_id)
                items = (items_resp.payload or {}).get("OrderItems", [])
                for it in items:
                    conn.execute("""
                        INSERT INTO order_items(order_item_id, amazon_order_id, asin, seller_sku,
                                                title, quantity_ordered, item_price, condition,
                                                shipped_quantity)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(order_item_id) DO UPDATE SET
                            quantity_ordered=excluded.quantity_ordered,
                            item_price=excluded.item_price
                    """, (
                        it.get("OrderItemId"),
                        order_id,
                        it.get("ASIN"),
                        it.get("SellerSKU"),
                        it.get("Title"),
                        it.get("QuantityOrdered"),
                        float(it.get("ItemPrice", {}).get("Amount", 0)) if it.get("ItemPrice") else 0,
                        it.get("ConditionId"),
                        it.get("QuantityShipped", 0),
                    ))
            except SellingApiException:
                pass
    return len(all_orders)


# ================================================================
# Returns
# ================================================================
def sync_returns(days: int = 30) -> int:
    """FBA 返品レポートを取得して DB に upsert。
    Reports API の createdSince は最大 90 日前まで、それより古いものは強制的に 90 日に丸める。"""
    reports = get_client(SHOP, Reports)
    effective_days = min(days, 89)  # Amazon は 90 日制限
    created_since = (
        (datetime.now(JST) - timedelta(days=effective_days))
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    resp = reports.get_reports(
        reportTypes=[RETURNS_REPORT_TYPE],
        processingStatuses=["DONE"],
        createdSince=created_since,
        pageSize=10,
    )
    report_list = (resp.payload or {}).get("reports", [])
    if not report_list:
        return 0

    latest = sorted(report_list, key=lambda r: r.get("createdTime", ""), reverse=True)[0]
    doc_id = latest.get("reportDocumentId", "")
    if not doc_id:
        return 0

    doc = reports.get_report_document(doc_id).payload
    url = doc.get("url", "")
    if not url:
        return 0

    # TSV ダウンロード（Windows-31J エンコーディング想定）
    with urllib.request.urlopen(url, timeout=30) as r:
        raw_bytes = r.read()
        content_type = r.headers.get("content-type", "")
    charset = "cp932"
    if "charset=" in content_type.lower():
        cs = content_type.split("charset=")[-1].strip().lower()
        if cs == "windows-31j":
            cs = "cp932"
        charset = cs
    raw = raw_bytes.decode(charset, errors="replace")
    lines = raw.splitlines()
    if not lines:
        return 0

    reader = csv.DictReader(lines, delimiter="\t")
    rows = list(reader)
    with get_db() as conn:
        for r in rows:
            conn.execute("""
                INSERT OR REPLACE INTO returns(
                    return_date, amazon_order_id, seller_sku, asin, fnsku, quantity,
                    reason, detailed_disposition, fulfillment_center_id, customer_comments
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                r.get("return-date"),
                r.get("order-id"),
                r.get("sku"),
                r.get("asin"),
                r.get("fnsku"),
                int(r.get("quantity") or 0),
                r.get("reason"),
                r.get("detailed-disposition"),
                r.get("fulfillment-center-id"),
                r.get("customer-comments"),
            ))
    return len(rows)


# ================================================================
# Inventory (Listings Items)
# ================================================================
def sync_inventory() -> int:
    """Merchant Listings Report から全出品を取得して DB に upsert（Active のみ保存）"""
    import time
    reports = get_client(SHOP, Reports)
    # 1. レポート作成要求
    create = reports.create_report(
        reportType="GET_MERCHANT_LISTINGS_ALL_DATA",
        marketplaceIds=[MARKETPLACE_ID],
    )
    report_id = create.payload.get("reportId")
    if not report_id:
        return 0
    # 2. 完了待ち（最大 2 分）
    doc_id = None
    for _ in range(24):
        time.sleep(5)
        info = reports.get_report(report_id)
        status = info.payload.get("processingStatus")
        if status == "DONE":
            doc_id = info.payload.get("reportDocumentId")
            break
        if status in ("CANCELLED", "FATAL"):
            return 0
    if not doc_id:
        return 0
    # 3. ドキュメント取得
    doc = reports.get_report_document(doc_id, download=True, decrypt=True)
    content = doc.payload.get("document", "") or ""
    lines = content.splitlines()
    if not lines:
        return 0
    header = lines[0].lstrip("\ufeff").split("\t")

    def idx(name):
        try:
            return header.index(name)
        except ValueError:
            return -1

    name_i   = idx("商品名")
    sku_i    = idx("出品者SKU")
    price_i  = idx("価格")
    qty_i    = idx("数量")          # MFN 向け
    fba_qty_i = idx("在庫数")        # FBA 向け（参考値、基本 FBA API で上書き）
    cond_i   = idx("コンディション")
    fc_i     = idx("フルフィルメント・チャンネル")
    status_i = idx("ステータス")
    asin_i   = idx("商品ID")
    listed_i = idx("出品日")        # ASIN 登録日

    now_iso = datetime.utcnow().isoformat()
    imported = 0
    with get_db() as conn:
        # 旧 Active 行を一旦 Inactive に倒し、今回含まれなければ除外できるようにする
        # （完全同期するため）
        for ln in lines[1:]:
            cols = ln.split("\t")
            if len(cols) < len(header):
                cols += [""] * (len(header) - len(cols))
            status_raw = cols[status_i] if status_i >= 0 else ""
            # Inactive も保存（販売済みSKUの asin_listed_at を保持するため）
            # 在庫一覧画面は status='Active%' AND quantity>0 でフィルタ
            sku = cols[sku_i]
            if not sku:
                continue
            fc = cols[fc_i] if fc_i >= 0 else ""
            fulfillment = "AFN" if "AMAZON" in fc else "DEFAULT"
            try:
                price = float(cols[price_i]) if cols[price_i] else 0
            except Exception:
                price = 0
            # quantity: MFN は 数量、FBA はレポートの 在庫数 を仮置き（後で FBA API が上書き）
            try:
                qty = int(cols[qty_i] or 0) if fulfillment == "DEFAULT" else int(cols[fba_qty_i] or 0)
            except Exception:
                qty = 0

            listed_at = cols[listed_i] if listed_i >= 0 else None
            conn.execute("""
                INSERT INTO inventory(seller_sku, asin, title, product_condition,
                                      fulfillment_channel, quantity, listing_price,
                                      status, updated_at, asin_listed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(seller_sku) DO UPDATE SET
                    asin=excluded.asin,
                    title=excluded.title,
                    product_condition=excluded.product_condition,
                    fulfillment_channel=excluded.fulfillment_channel,
                    listing_price=excluded.listing_price,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    asin_listed_at=COALESCE(excluded.asin_listed_at, inventory.asin_listed_at)
            """, (
                sku,
                cols[asin_i] if asin_i >= 0 else None,
                cols[name_i] if name_i >= 0 else None,
                cols[cond_i] if cond_i >= 0 else None,
                fulfillment,
                qty,
                price,
                status_raw,
                now_iso,
                listed_at,
            ))
            imported += 1
    return imported


# ================================================================
# FBA Inventory（在庫数量）
# ================================================================
def sync_fba_quantities() -> int:
    """FBA 在庫レポートから数量を取得し upsert。
    getInventorySummaries API はページング不全で 50 SKU しか返さないため、レポート方式を採用。
    優先: GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA (列が豊富)
    フォールバック: GET_AFN_INVENTORY_DATA (レート制限に強い)"""
    import time
    reports = get_client(SHOP, Reports)

    def fetch(report_type):
        """レート制限対策: 直近 24h の DONE レポートを先に探し、なければ新規作成"""
        doc_id = None
        # 1) 既存の最新 DONE レポートを再利用
        try:
            listed = reports.get_reports(
                reportTypes=[report_type],
                marketplaceIds=[MARKETPLACE_ID],
                processingStatuses=["DONE"],
                pageSize=1,
            )
            items = (listed.payload or {}).get("reports", []) or []
            if items:
                doc_id = items[0].get("reportDocumentId")
        except Exception:
            pass
        # 2) 無ければ新規作成
        if not doc_id:
            try:
                c = reports.create_report(reportType=report_type, marketplaceIds=[MARKETPLACE_ID])
            except Exception:
                return None
            rid = (c.payload or {}).get("reportId")
            if not rid:
                return None
            for _ in range(36):
                time.sleep(5)
                info = reports.get_report(rid)
                st = info.payload.get("processingStatus")
                if st == "DONE":
                    doc_id = info.payload.get("reportDocumentId"); break
                if st in ("CANCELLED", "FATAL"):
                    return None
        if not doc_id:
            return None
        doc = reports.get_report_document(doc_id, download=True, decrypt=True)
        content = doc.payload.get("document", "") or ""
        return content.splitlines()

    lines = fetch("GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
    used_report = "MYI"
    if not lines or len(lines) < 2:
        lines = fetch("GET_AFN_INVENTORY_DATA")
        used_report = "AFN"
    if not lines or len(lines) < 2:
        return 0

    header = lines[0].lstrip("\ufeff").split("\t")

    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return -1

    agg = {}
    if used_report == "MYI":
        sku_i  = col("sku")
        asin_i = col("asin")
        name_i = col("product-name")
        cond_i = col("condition")
        ful_i  = col("afn-fulfillable-quantity")
        for ln in lines[1:]:
            cols = ln.split("\t")
            if len(cols) < len(header):
                cols += [""] * (len(header) - len(cols))
            sku = cols[sku_i] if sku_i >= 0 else ""
            if not sku:
                continue
            try: qty = int(cols[ful_i] or 0) if ful_i >= 0 else 0
            except Exception: qty = 0
            if sku in agg:
                agg[sku]["qty"] += qty
            else:
                agg[sku] = {
                    "qty": qty,
                    "asin": cols[asin_i] if asin_i >= 0 else None,
                    "name": cols[name_i] if name_i >= 0 else None,
                    "cond": cols[cond_i] if cond_i >= 0 else None,
                }
    else:  # AFN: seller-sku / ... / Warehouse-Condition-code / Quantity Available
        sku_i  = col("seller-sku")
        asin_i = col("asin")
        cond_i = col("condition-type")
        wh_i   = col("Warehouse-Condition-code")
        qty_i  = col("Quantity Available")
        for ln in lines[1:]:
            cols = ln.split("\t")
            if len(cols) < len(header):
                cols += [""] * (len(header) - len(cols))
            sku = cols[sku_i] if sku_i >= 0 else ""
            if not sku:
                continue
            # SELLABLE のみ集計（UNSELLABLE は販売不可）
            wh = cols[wh_i] if wh_i >= 0 else ""
            if wh != "SELLABLE":
                continue
            try: qty = int(cols[qty_i] or 0) if qty_i >= 0 else 0
            except Exception: qty = 0
            if sku in agg:
                agg[sku]["qty"] += qty
            else:
                agg[sku] = {
                    "qty": qty,
                    "asin": cols[asin_i] if asin_i >= 0 else None,
                    "name": None,
                    "cond": cols[cond_i] if cond_i >= 0 else None,
                }

    now_iso = datetime.utcnow().isoformat()
    updated = 0
    with get_db() as conn:
        for sku, d in agg.items():
            fulfillable = d["qty"]
            asin = d["asin"]
            product_name = d["name"]
            condition = d["cond"]
            # INSERT OR UPDATE: 既存行なら quantity のみ更新、無ければスケルトンを作成
            exists = conn.execute(
                "SELECT 1 FROM inventory WHERE seller_sku=?", (sku,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE inventory SET quantity=?, asin=COALESCE(?, asin), updated_at=? WHERE seller_sku=?",
                    (fulfillable, asin, now_iso, sku)
                )
            else:
                conn.execute("""
                    INSERT INTO inventory(seller_sku, asin, title, product_condition,
                                          fulfillment_channel, quantity, listing_price,
                                          status, updated_at)
                    VALUES(?, ?, ?, ?, 'AFN', ?, NULL, 'DISCOVERABLE', ?)
                """, (sku, asin, product_name, condition, fulfillable, now_iso))
            updated += 1
    return updated


# ================================================================
# Keepa API: ASIN 全体販売数推定（30/90/180 日）
# ================================================================
def sync_keepa_sales(asins: list[str] | None = None, limit: int | None = None,
                     stale_hours: int = 24) -> int:
    """Keepa Product API から全 seller を含む販売数推定を取得し inventory に保存。
    用途: 在庫一覧の「販売数 自分/全体」表示で全体側を埋めるため。

    引数:
        asins: 対象 ASIN リスト。None なら DB の Active+qty>0 から自動取得。
        limit: 同期件数上限（None=全件）。
        stale_hours: 直近この時間以内に更新済みの ASIN はスキップ（API トークン節約）。

    戻り値: 更新件数。
    """
    import json as _json
    from db import get_setting
    api_key = get_setting("keepa_api_key", "")
    if not api_key:
        return 0  # 未設定時は何もしない

    if asins is None:
        with get_db() as c:
            rows = c.execute(
                "SELECT DISTINCT asin FROM inventory "
                "WHERE asin IS NOT NULL AND asin != '' "
                "  AND status LIKE 'Active%' AND quantity > 0 "
                "  AND (keepa_updated_at IS NULL OR "
                "       datetime(keepa_updated_at) < datetime('now', ?))",
                (f"-{stale_hours} hours",),
            ).fetchall()
        asins = [r["asin"] for r in rows]
    if limit:
        asins = asins[:limit]
    if not asins:
        return 0

    updated = 0
    # Keepa は 1リクエストで最大 100 ASIN まとめ可能
    for i in range(0, len(asins), 100):
        chunk = asins[i:i+100]
        url = "https://api.keepa.com/product"
        params = {
            "key": api_key,
            "domain": "5",  # Amazon.co.jp
            "asin": ",".join(chunk),
            "stats": "180",  # 180日統計が含まれる
            "history": "1",  # csv[3] (Sales Rank 時系列) を取得
        }
        try:
            req = urllib.request.Request(
                url + "?" + urllib.parse.urlencode(params),
                headers={
                    "User-Agent": "seller-dashboard/1.0",
                    "Accept-Encoding": "gzip",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                # Keepa API はデフォルトで gzip 圧縮レスポンスを返す
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    import gzip
                    raw = gzip.decompress(raw)
                payload = _json.loads(raw.decode("utf-8"))
        except Exception as e:
            (LOGS_DIR / "keepa_error.log").write_text(
                f"{datetime.utcnow().isoformat()} chunk={chunk[:5]} err={type(e).__name__}: {e}\n",
                encoding="utf-8",
            )
            continue
        products = payload.get("products") or []
        now_iso = datetime.utcnow().isoformat()
        with get_db() as c:
            for p in products:
                asin = p.get("asin")
                stats = p.get("stats") or {}
                d30 = stats.get("salesRankDrops30")
                d90 = stats.get("salesRankDrops90")
                d180 = stats.get("salesRankDrops180")
                # BSR 履歴: csv[3] = AMAZON Sales Rank (time, rank, time, rank, ...)
                # time は Keepa minutes (epoch_sec / 60 - 21564000)
                bsr_current = None
                bsr_history = []
                csv = p.get("csv") or []
                if len(csv) > 3 and csv[3]:
                    sr = csv[3]
                    # 日次にダウンサンプリング: 同じ日付の最後の値だけ採用
                    by_date = {}
                    for j in range(0, len(sr) - 1, 2):
                        km = sr[j]
                        rank = sr[j + 1]
                        if rank is None or rank < 0:
                            continue
                        ts = (km + 21564000) * 60
                        try:
                            date_str = datetime.utcfromtimestamp(ts).date().isoformat()
                        except (OSError, ValueError):
                            continue
                        by_date[date_str] = rank
                    bsr_history = [{"date": d, "rank": r} for d, r in sorted(by_date.items())]
                    if bsr_history:
                        bsr_current = bsr_history[-1]["rank"]
                # current 配列からも取得（より新鮮な可能性）
                cur = p.get("current") or []
                if len(cur) > 3 and cur[3] and cur[3] > 0:
                    bsr_current = cur[3]
                bsr_json = _json.dumps(bsr_history, ensure_ascii=False) if bsr_history else None
                c.execute(
                    "UPDATE inventory SET keepa_sales_30d=?, keepa_sales_90d=?, "
                    "  keepa_sales_180d=?, keepa_updated_at=?, "
                    "  bsr_current=?, bsr_history_json=?, bsr_updated_at=? "
                    "WHERE asin=?",
                    (d30, d90, d180, now_iso, bsr_current, bsr_json, now_iso, asin),
                )
                updated += 1
    return updated


# ================================================================
# Keepa: トークン残量チェック（polling から共有 API キーで使う）
# ================================================================
def keepa_tokens_left() -> int | None:
    """Keepa /token API で現在のトークン残量を取得。失敗時は None。"""
    import json as _json
    from db import get_setting
    api_key = get_setting("keepa_api_key", "")
    if not api_key:
        return None
    try:
        url = f"https://api.keepa.com/token?key={api_key}"
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            data = _json.loads(raw.decode("utf-8"))
        return int(data.get("tokensLeft", 0) or 0)
    except Exception:
        return None


# ================================================================
# 市場 BSR 取得（カメラ＋レンズ売れ筋トップN を 24h 均等取得）
# ================================================================
def sync_market_bsr_one() -> dict:
    """市場 BSR インクリメンタル同期: 1 ASIN だけ取得して DB に保存。
    APScheduler が間隔指定で呼び出す前提（24h ÷ N 件分間隔）。
    戻り値: {"status": "ok"|"skip_disabled"|"skip_low_token"|"skip_no_target"|"error",
              "asin": str|None, "ym_score_updated": bool, ...}
    """
    import json as _json
    from db import get_setting
    result = {"status": "skip", "asin": None}
    if get_setting("market_bsr_enabled", "0") != "1":
        result["status"] = "skip_disabled"
        return result

    api_key = get_setting("keepa_api_key", "")
    if not api_key:
        result["status"] = "skip_no_api_key"
        return result

    # トークン残量チェック（refresh_ProductsList と共存するため余裕を残す）
    tokens = keepa_tokens_left()
    if tokens is not None and tokens < 50:
        result["status"] = "skip_low_token"
        result["tokens_left"] = tokens
        return result

    try:
        top_n = int(get_setting("market_bsr_top_n", "200") or 200)
    except Exception:
        top_n = 200
    top_n = max(50, min(500, top_n))

    # トップNのうち最も古い更新の ASIN を1件選ぶ
    with get_db() as conn:
        row = conn.execute(
            "SELECT asin FROM market_bsr_meta "
            "WHERE rank_in_category IS NOT NULL AND rank_in_category <= ? "
            "ORDER BY (bsr_updated_at IS NULL) DESC, bsr_updated_at ASC, rank_in_category ASC "
            "LIMIT 1",
            (top_n,),
        ).fetchone()
    if not row:
        result["status"] = "skip_no_target"
        return result
    asin = row["asin"]
    result["asin"] = asin

    # Keepa Product API で1 ASIN 取得
    url = "https://api.keepa.com/product"
    params = {
        "key": api_key,
        "domain": "5",
        "asin": asin,
        "stats": "180",
        "history": "1",
    }
    try:
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "seller-dashboard/1.0", "Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            payload = _json.loads(raw.decode("utf-8"))
    except Exception as e:
        (LOGS_DIR / "market_bsr_error.log").write_text(
            f"{datetime.utcnow().isoformat()} asin={asin} err={type(e).__name__}: {e}\n",
            encoding="utf-8",
        )
        result["status"] = "error"
        result["error"] = str(e)[:200]
        return result

    products = payload.get("products") or []
    if not products:
        result["status"] = "no_product"
        # 失敗でも updated_at は進めて先に進む（永遠に詰まるのを防止）
        with get_db() as conn:
            conn.execute(
                "UPDATE market_bsr_meta SET bsr_updated_at=? WHERE asin=?",
                (datetime.utcnow().isoformat(), asin),
            )
        return result

    p = products[0]
    title = p.get("title")
    csv = p.get("csv") or []
    bsr_current = None
    bsr_history = []
    if len(csv) > 3 and csv[3]:
        sr = csv[3]
        by_date = {}
        for j in range(0, len(sr) - 1, 2):
            km = sr[j]
            rank = sr[j + 1]
            if rank is None or rank < 0:
                continue
            ts = (km + 21564000) * 60
            try:
                date_str = datetime.utcfromtimestamp(ts).date().isoformat()
            except (OSError, ValueError):
                continue
            by_date[date_str] = rank
        bsr_history = [{"date": d, "rank": r} for d, r in sorted(by_date.items())]
        if bsr_history:
            bsr_current = bsr_history[-1]["rank"]
    cur = p.get("current") or []
    if len(cur) > 3 and cur[3] and cur[3] > 0:
        bsr_current = cur[3]
    # 価格（New 価格 csv[1]）
    price = None
    if len(cur) > 1 and cur[1] and cur[1] > 0:
        price = int(cur[1])

    bsr_json = _json.dumps(bsr_history, ensure_ascii=False) if bsr_history else None
    now_iso = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE market_bsr_meta SET title=?, current_price=?, bsr_current=?, "
            "  bsr_history_json=?, bsr_updated_at=? WHERE asin=?",
            (title, price, bsr_current, bsr_json, now_iso, asin),
        )
        # history テーブルにも upsert
        for h in bsr_history[-90:]:  # 直近90日のみ正規化テーブルに反映（参照用）
            conn.execute(
                "INSERT OR REPLACE INTO market_bsr_history(asin, date, rank, fetched_at) "
                "VALUES(?, ?, ?, ?)",
                (asin, h["date"], h["rank"], now_iso),
            )
    result["status"] = "ok"
    result["history_points"] = len(bsr_history)
    result["bsr_current"] = bsr_current

    # スコアキャッシュ再計算
    try:
        recompute_market_score_cache()
        result["score_recomputed"] = True
    except Exception as e:
        result["score_recomputed_error"] = str(e)[:200]
    return result


def recompute_market_score_cache() -> int:
    """market_bsr_meta から市場活況スコアを再計算し market_score_cache に保存。
    戻り値=書き込まれた月数。"""
    from market_score import compute_market_score
    with get_db() as conn:
        rows = [r["bsr_history_json"] for r in conn.execute(
            "SELECT bsr_history_json FROM market_bsr_meta "
            "WHERE bsr_history_json IS NOT NULL AND bsr_history_json != '[]'"
        ).fetchall()]
    ym_score = compute_market_score(rows)
    now_iso = datetime.utcnow().isoformat()
    with get_db() as conn:
        for ym, v in ym_score.items():
            conn.execute(
                "INSERT INTO market_score_cache(ym, score, median_bsr, raw_score, asin_count, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ym) DO UPDATE SET "
                "  score=excluded.score, median_bsr=excluded.median_bsr, "
                "  raw_score=excluded.raw_score, asin_count=excluded.asin_count, "
                "  updated_at=excluded.updated_at",
                (ym, v.get("score"), v.get("median_bsr"),
                 v.get("raw"), v.get("asin_count"), now_iso),
            )
    return len(ym_score)


# ================================================================
# Catalog Items 画像取得（画像 URL 欠落分を補完）
# ================================================================
def sync_catalog_images(limit: int | None = None) -> int:
    """inventory.main_image_url が NULL の ASIN について Catalog Items API
    (v2022-04-01) で画像 URL を取得し埋める。戻り値=更新件数。"""
    cat = get_client(SHOP, CatalogItemsV20220401)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT asin FROM inventory "
            "WHERE asin IS NOT NULL AND asin != '' "
            "  AND (main_image_url IS NULL OR main_image_url = '') "
            "  AND status LIKE 'Active%'"
        ).fetchall()
    asins = [r["asin"] for r in rows]
    if limit:
        asins = asins[:limit]
    if not asins:
        return 0

    updated = 0
    for asin in asins:
        try:
            resp = cat.get_catalog_item(
                asin=asin,
                marketplaceIds=[MARKETPLACE_ID],
                includedData=["images"],
            )
            payload = resp.payload or {}
            images = payload.get("images") or []
            # images は marketplace ごとのリスト、各要素に images 配列
            img_url = None
            for block in images:
                imgs = block.get("images") or []
                if imgs:
                    # 最初の画像を採用（MAIN variant 優先）
                    main = next((i for i in imgs if i.get("variant") == "MAIN"), imgs[0])
                    img_url = main.get("link")
                    if img_url:
                        break
            if img_url:
                with get_db() as c:
                    c.execute(
                        "UPDATE inventory SET main_image_url=? WHERE asin=?",
                        (img_url, asin),
                    )
                updated += 1
        except Exception:
            continue
    return updated


# ================================================================
# 出品一覧（Offers）- 最低価格ツールチップ用
# ================================================================
def sync_offers(asins: list[str] | None = None, limit: int = 60, stale_hours: int = 2) -> int:
    """各 ASIN の出品一覧を取得し offers_json に保存。
    stale_hours 以内に同期済みのものはスキップ。"""
    import json, time as _t
    products = get_client(SHOP, Products)
    if asins is None:
        with get_db() as c:
            rows = c.execute(
                "SELECT DISTINCT asin FROM inventory "
                "WHERE asin IS NOT NULL AND asin != '' "
                "  AND status LIKE 'Active%' AND quantity > 0 "
                "  AND (offers_updated_at IS NULL OR "
                "       datetime(offers_updated_at) < datetime('now', ?))",
                (f"-{stale_hours} hours",),
            ).fetchall()
        asins = [r["asin"] for r in rows][:limit]
    updated = 0
    for asin in asins:
        try:
            resp = products.get_item_offers(asin=asin, item_condition="Used")
            payload = resp.payload or {}
            offers = payload.get("Offers", []) or []
            parsed = []
            for o in offers:
                lp = (o.get("ListingPrice") or {}).get("Amount")
                shp = (o.get("Shipping") or {}).get("Amount") or 0
                pts = ((o.get("Points") or {}).get("PointsNumber")) or 0
                sub = (o.get("SubCondition") or "").lower()
                fulfillment = "FBA" if o.get("IsFulfilledByAmazon") else "FBM"
                # 自分のオファーかどうか（自分2出品の価格戦争防止用）
                seller_id = o.get("SellerId") or ""
                parsed.append({
                    "price": float(lp) if lp is not None else None,
                    "shipping": float(shp) if shp else 0,
                    "points": pts,
                    "total": (float(lp) if lp else 0) + (float(shp) if shp else 0),
                    "sub_condition": sub,   # new / like_new / very_good / good / acceptable
                    "fulfillment": fulfillment,
                    "is_cart": bool(o.get("IsBuyBoxWinner")),
                    "seller_id": seller_id,
                })
            parsed.sort(key=lambda x: x["total"] or 0)
            # min_price_all は「良い以上（= acceptable を除外）」の最安値。
            # ツールチップ表示用の offers_json は全件保持。
            qualifying = [o for o in parsed
                          if (o.get("sub_condition") or "").lower() != "acceptable"]
            min_ok = qualifying[0]["total"] if qualifying else (parsed[0]["total"] if parsed else None)
            now_iso = datetime.utcnow().isoformat()
            with get_db() as c:
                c.execute(
                    "UPDATE inventory SET offers_json=?, offers_updated_at=?, "
                    "  min_price_all=COALESCE(?, min_price_all) "
                    "WHERE asin=?",
                    (json.dumps(parsed, ensure_ascii=False),
                     now_iso,
                     min_ok,
                     asin),
                )
            updated += 1
            _t.sleep(1.1)  # レート制限対策（item_offers は 1 req/sec ）
        except Exception:
            _t.sleep(2.0)  # 失敗時も少し待つ
            continue
    return updated


# ================================================================
# 競合価格
# ================================================================
def sync_competitive_prices(asins: list) -> int:
    """競合のカート価格のみを取得して inventory に反映。
    min_price_all は sync_offers が中古限定で計算するため、ここでは更新しない
    （get_competitive_pricing_for_asins は新品・中古混在で返すので、min を取ると
    新品価格を拾ってしまう不具合があった）。"""
    if not asins:
        return 0
    products = get_client(SHOP, Products)
    updated = 0
    for i in range(0, len(asins), 20):
        chunk = asins[i:i+20]
        try:
            resp = products.get_competitive_pricing_for_asins(asin_list=chunk)
        except Exception:
            continue
        data = resp.payload if hasattr(resp, "payload") else []

        with get_db() as conn:
            for entry in (data or []):
                asin = entry.get("ASIN")
                pc = entry.get("Product", {}).get("CompetitivePricing", {})
                prices = pc.get("CompetitivePrices", [])
                if not prices:
                    continue
                # Featured Buy Box price（競合価格 id=1）
                cart_price = None
                for p in prices:
                    lp = p.get("Price", {}).get("ListingPrice", {})
                    amt = lp.get("Amount")
                    if amt and p.get("competitivePriceId") == "1":
                        cart_price = float(amt)
                        break
                conn.execute(
                    "UPDATE inventory SET cart_price=? WHERE asin=?",
                    (cart_price, asin),
                )
                updated += 1
    return updated


# ================================================================
# 仕入れ価格（Google Sheets 仕入れ台帳 同期）
# ================================================================
def sync_cost_prices() -> int:
    """仕入れ台帳 A7:Q を読んで cost_prices テーブルに upsert。
    列: A=SKU, E=ASIN, I=仕入日, J=仕入先, K=原価, P=販売(済), Q=販売日
    (販売日は 棚卸資産の時点評価に必須)"""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        return 0

    if not GOOGLE_CREDS_PATH.exists():
        return 0

    creds = service_account.Credentials.from_service_account_file(
        str(GOOGLE_CREDS_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    svc = build("sheets", "v4", credentials=creds)
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="'仕入れ台帳'!A7:Q"
    ).execute()
    rows = res.get("values", [])
    now_iso = datetime.utcnow().isoformat()
    count = 0

    def _norm_date(s: str) -> str:
        """'2025/12/31' or '2025/4/5' → '2025-12-31' / '2025-04-05'"""
        s = (s or "").strip()
        m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", s)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return s

    with get_db() as conn:
        for i, row in enumerate(rows, start=7):
            if len(row) < 11:
                continue
            sku = row[0].strip() if row[0] else ""
            asin = row[4].strip() if len(row) > 4 and row[4] else ""
            date = _norm_date(row[8] if len(row) > 8 else "")
            supplier = row[9].strip() if len(row) > 9 and row[9] else ""
            cost_str = row[10] if len(row) > 10 else ""
            sale_flag = (row[15].strip() if len(row) > 15 and row[15] else "")  # P列「販売」
            sale_date = _norm_date(row[16] if len(row) > 16 else "")             # Q列「販売日」
            if not sku:
                continue
            try:
                cost = float(
                    str(cost_str).replace("¥", "").replace(",", "").strip() or 0
                )
            except ValueError:
                cost = 0
            conn.execute("""
                INSERT INTO cost_prices(seller_sku, asin, cost_price, supplier,
                                        purchase_date, ledger_row, updated_at,
                                        sale_date, sale_flag)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(seller_sku) DO UPDATE SET
                    asin=excluded.asin,
                    cost_price=excluded.cost_price,
                    supplier=excluded.supplier,
                    purchase_date=excluded.purchase_date,
                    ledger_row=excluded.ledger_row,
                    updated_at=excluded.updated_at,
                    sale_date=excluded.sale_date,
                    sale_flag=excluded.sale_flag
            """, (sku, asin, cost, supplier, date, i, now_iso, sale_date, sale_flag))
            count += 1
        # 注: SKU は一意キーのため、接尾辞付き SKU（例: ABC-123-1）を別 SKU として
        # ユーザーが手動登録するのが正。自動継承はせず、
        # 仕入れ値未記入の SKU は在庫一覧上部の警告で可視化する。
    return count


# ================================================================
# Financial Events（Amazon 手数料）
# ================================================================
def sync_financial_events(days: int = 14) -> int:
    """Finances API から手数料情報を取得し以下を更新:
    - order_items.amazon_fee（確定）、shipping_price、promotion_discount（注文別の送料・プロモ）
    - expenses テーブルの 'Amazon利用料'（月別）= Subscription/Storage/RemovalFee など全サービス系手数料

    戻り値: order_items 更新件数
    """
    fin = get_client(SHOP, Finances)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    shipments, service_fees, storage_fees, adjustments, removals = [], [], [], [], []
    next_token = None
    while True:
        kwargs = {"PostedAfter": since, "MaxResultsPerPage": 100}
        if next_token:
            kwargs["NextToken"] = next_token
        try:
            resp = fin.list_financial_events(**kwargs)
        except Exception:
            break
        payload = (resp.payload or {}).get("FinancialEvents", {}) if resp.payload else {}
        shipments.extend(payload.get("ShipmentEventList", []) or [])
        service_fees.extend(payload.get("ServiceFeeEventList", []) or [])
        # StorageFeeEventList は月1回程度だが拾う
        for ev in (payload.get("ServiceFeeEventList", []) or []):
            pass  # 既に上で格納
        # Subscription fee を含む
        adjustments.extend(payload.get("AdjustmentEventList", []) or [])
        removals.extend(payload.get("RemovalShipmentEventList", []) or [])
        # StorageFee は別イベント
        for key in ("AffordabilityExpenseEventList", "PayWithAmazonEventList"):
            pass
        next_token = (resp.payload or {}).get("NextToken")
        if not next_token:
            break

    # ----- Shipment Events: order_items 手数料・送料・プロモ分離 -----
    updated = 0
    # Amazon 利用料累計 (year_month -> 合計)
    amz_by_ym: dict[str, float] = {}

    def add_amz(posted_date: str | None, amount: float):
        if not posted_date:
            return
        ym = posted_date[:7]
        amz_by_ym[ym] = amz_by_ym.get(ym, 0) + abs(float(amount or 0))

    with get_db() as conn:
        for ev in shipments:
            order_id = ev.get("AmazonOrderId")
            posted = ev.get("PostedDate")
            items = ev.get("ShipmentItemList", []) or []
            for item in items:
                order_item_id = item.get("OrderItemId")
                fee_total = shipping = promo = 0.0
                # ItemFeeList: Commission, FBA関連
                for fee in (item.get("ItemFeeList") or []):
                    amt = abs(float((fee.get("FeeAmount") or {}).get("CurrencyAmount", 0) or 0))
                    fee_total += amt
                # ItemChargeList: Principal / Shipping / Gift / Tax 等
                for chg in (item.get("ItemChargeList") or []):
                    ctype = chg.get("ChargeType")
                    camt = float((chg.get("ChargeAmount") or {}).get("CurrencyAmount", 0) or 0)
                    if ctype == "ShippingCharge":
                        shipping += camt
                    elif ctype in ("GiftWrap", "GiftWrapTax"):
                        shipping += camt  # ギフト入金も送料に含める
                # PromotionList
                for p in (item.get("PromotionList") or []):
                    pamt = float((p.get("PromotionAmount") or {}).get("CurrencyAmount", 0) or 0)
                    promo += abs(pamt)
                if order_item_id:
                    conn.execute(
                        "UPDATE order_items SET amazon_fee=?, amazon_fee_confirmed=?, "
                        "  shipping_price=?, promotion_discount=? "
                        "WHERE order_item_id=?",
                        (fee_total, 1 if fee_total > 0 else 0,
                         shipping, promo, order_item_id),
                    )
                    if fee_total > 0:
                        updated += 1

                conn.execute("""
                    INSERT INTO financial_events(amazon_order_id, event_type, posted_date,
                                                 fee_type, amount, currency, raw_json)
                    VALUES(?,?,?,?,?,?,?)
                """, (
                    order_id, "Shipment", posted, "ItemFeeList", fee_total, "JPY",
                    json.dumps(item, ensure_ascii=False)[:3000],
                ))

        # ----- ServiceFeeEvent (月額サブスクリプション、返品手数料など) -----
        for ev in service_fees:
            posted = ev.get("PostedDate")
            fee_total = 0
            for fee in (ev.get("FeeList") or []):
                amt = abs(float((fee.get("FeeAmount") or {}).get("CurrencyAmount", 0) or 0))
                fee_total += amt
            add_amz(posted, fee_total)
            conn.execute("""
                INSERT INTO financial_events(event_type, posted_date, fee_type, amount, currency, raw_json)
                VALUES(?,?,?,?,?,?)
            """, ("ServiceFee", posted, ev.get("FeeReason") or "ServiceFee",
                  fee_total, "JPY",
                  json.dumps(ev, ensure_ascii=False)[:3000]))

        # ----- RemovalShipmentEvent (FBA 取り出し手数料) -----
        for ev in removals:
            posted = ev.get("PostedDate")
            fee_total = 0
            for item in (ev.get("RemovalShipmentItemList") or []):
                amt = abs(float((item.get("TotalAmount") or {}).get("CurrencyAmount", 0) or 0))
                fee_total += amt
            add_amz(posted, fee_total)
            conn.execute("""
                INSERT INTO financial_events(event_type, posted_date, fee_type, amount, currency, raw_json)
                VALUES(?,?,?,?,?,?)
            """, ("Removal", posted, "RemovalShipment",
                  fee_total, "JPY",
                  json.dumps(ev, ensure_ascii=False)[:3000]))

        # ----- AdjustmentEvent (返品関連手数料など) -----
        for ev in adjustments:
            posted = ev.get("PostedDate")
            reason = ev.get("AdjustmentType") or "Adjustment"
            amt = abs(float((ev.get("AdjustmentAmount") or {}).get("CurrencyAmount", 0) or 0))
            add_amz(posted, amt)
            conn.execute("""
                INSERT INTO financial_events(event_type, posted_date, fee_type, amount, currency, raw_json)
                VALUES(?,?,?,?,?,?)
            """, ("Adjustment", posted, reason, amt, "JPY",
                  json.dumps(ev, ensure_ascii=False)[:3000]))

        # ----- expenses テーブルへ月次 Amazon利用料を upsert -----
        for ym, total in amz_by_ym.items():
            conn.execute("""
                INSERT INTO expenses(year_month, category, amount, auto_calculated, repeat_monthly, note)
                VALUES(?, 'Amazon利用料', ?, 1, 0, 'Finances API 自動集計')
                ON CONFLICT(year_month, category) DO UPDATE SET
                  amount=excluded.amount,
                  auto_calculated=1,
                  note=excluded.note
            """, (ym, total))

    return updated


# ================================================================
# 軽量更新: 最低価格のみ
# ================================================================
def run_light_refresh() -> dict:
    """競合最低価格のみ高速更新（在庫・注文は触らない）"""
    result = {"started_at": datetime.utcnow().isoformat()}
    try:
        with get_db() as conn:
            asins = [r["asin"] for r in conn.execute(
                "SELECT DISTINCT asin FROM inventory WHERE asin IS NOT NULL AND asin!=''"
            ).fetchall()]
        n = sync_competitive_prices(asins)
        result["competitive"] = n
    except Exception as e:
        result["error"] = str(e)[:400]
    result["finished_at"] = datetime.utcnow().isoformat()
    return result


# ================================================================
# 全体 Polling
# ================================================================
def run_all_polling(days: int = 60) -> dict:
    """全種を順に同期。結果サマリーを返す"""
    result = {"started_at": datetime.utcnow().isoformat(), "details": {}}
    log_msg = []

    for name, fn in [
        ("orders", lambda: sync_orders(days=days)),
        ("inventory", sync_inventory),
        ("fba_quantities", sync_fba_quantities),
        ("returns", lambda: sync_returns(days=30)),
        ("cost_prices", sync_cost_prices),
        ("financial_events", lambda: sync_financial_events(days=max(days, 14))),
    ]:
        try:
            n = fn()
            result["details"][name] = n
            log_msg.append(f"{name}={n}")
        except Exception as e:
            result["details"][name] = f"ERROR: {type(e).__name__}: {e}"
            log_msg.append(f"{name}=ERROR")
            # トレースをログファイルへ
            (LOGS_DIR / f"polling_error_{name}.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )

    # 競合価格（在庫の ASIN 一覧に対して）
    try:
        with get_db() as conn:
            asins = [r["asin"] for r in conn.execute(
                "SELECT DISTINCT asin FROM inventory WHERE asin IS NOT NULL AND asin!=''"
            ).fetchall()]
        n = sync_competitive_prices(asins)
        result["details"]["competitive"] = n
        log_msg.append(f"competitive={n}")
    except Exception as e:
        result["details"]["competitive"] = f"ERROR: {e}"
        log_msg.append("competitive=ERROR")

    # 出品一覧（最低価格ツールチップ用） — 未更新/古いもののみ同期
    try:
        n = sync_offers(limit=60, stale_hours=2)
        result["details"]["offers"] = n
        log_msg.append(f"offers={n}")
    except Exception as e:
        result["details"]["offers"] = f"ERROR: {e}"
        log_msg.append("offers=ERROR")

    # Catalog Items 画像補完（main_image_url が空なら）
    try:
        n = sync_catalog_images(limit=40)
        result["details"]["catalog_images"] = n
        log_msg.append(f"images={n}")
    except Exception as e:
        result["details"]["catalog_images"] = f"ERROR: {e}"
        log_msg.append("images=ERROR")

    # Keepa 全体販売数（API key 設定済みなら）
    try:
        n = sync_keepa_sales(stale_hours=24)
        result["details"]["keepa_sales"] = n
        log_msg.append(f"keepa={n}")
    except Exception as e:
        result["details"]["keepa_sales"] = f"ERROR: {e}"
        log_msg.append("keepa=ERROR")

    result["finished_at"] = datetime.utcnow().isoformat()

    # polling_log へ記録
    with get_db() as conn:
        conn.execute("""
            INSERT INTO polling_log(started_at, finished_at, target, success, message)
            VALUES(?, ?, 'all', ?, ?)
        """, (result["started_at"], result["finished_at"],
              1 if "ERROR" not in " ".join(log_msg) else 0,
              ", ".join(log_msg)))

    return result


if __name__ == "__main__":
    # 単体実行用
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    # スタンドアロン実行時もDBの設定値を環境変数に流す
    from env_bootstrap import bootstrap_env_from_db
    bootstrap_env_from_db()
    print("Polling 実行開始...")
    r = run_all_polling(days=7)
    print(json.dumps(r, ensure_ascii=False, indent=2))
