"""価格自動調整エンジン（MVP: プリセット5モード + ストッパー）

Phase 1 では DRY RUN のみ（DB に変更候補ログを記録、実際の PATCH は行わない）。
Phase 2 で実際の価格更新（Listings Items PATCH）に拡張予定。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# amazon-seller-automation の scripts をパスに追加
_PROJECT_ROOT = Path(__file__).resolve().parent
_ASA_ROOT = _PROJECT_ROOT.parent / "amazon-seller-automation"
sys.path.insert(0, str(_ASA_ROOT))

from sp_api.api.listings_items.listings_items_2021_08_01 import (
    ListingsItemsV20210801,
)
from sp_api.base import Marketplaces

from config import MARKETPLACE_ID, SHOP_KEY
from db import get_db, get_setting

SHOP = SHOP_KEY


def patch_amazon_price(sku: str, new_price: float) -> tuple[bool, str | None]:
    """SKU の出品価格を Amazon Listings Items API で即時更新。
    戻り値: (成功, エラーメッセージ)。成功時は listing_price と price_updated_at も DB 更新。"""
    from scripts.common.sp_api_client import _load_credentials, get_seller_id
    creds = _load_credentials(SHOP)
    seller_id = get_seller_id(SHOP)
    li = ListingsItemsV20210801(credentials=creds, marketplace=Marketplaces.JP)

    # productType 取得（DBキャッシュ or SP-API）
    product_type = None
    with get_db() as c:
        row = c.execute(
            "SELECT product_type FROM inventory WHERE seller_sku=?", (sku,)
        ).fetchone()
        product_type = row["product_type"] if row else None
    if not product_type:
        try:
            rt = li.get_listings_item(
                sellerId=seller_id, sku=sku,
                marketplaceIds=MARKETPLACE_ID,
                includedData=["summaries"],
            )
            summaries = (rt.payload or {}).get("summaries", []) or []
            if summaries:
                product_type = summaries[0].get("productType")
            if product_type:
                with get_db() as c:
                    c.execute(
                        "UPDATE inventory SET product_type=? WHERE seller_sku=?",
                        (product_type, sku),
                    )
        except Exception as e:
            return False, f"productType 取得失敗: {e}"[:200]
    if not product_type:
        return False, "productType 不明"

    patch_body = {
        "productType": product_type,
        "patches": [{
            "op": "replace",
            "path": "/attributes/purchasable_offer",
            "value": [{
                "marketplace_id": MARKETPLACE_ID,
                "currency": "JPY",
                "our_price": [{"schedule": [{"value_with_tax": float(new_price)}]}]
            }]
        }]
    }
    try:
        resp = li.patch_listings_item(
            sellerId=seller_id, sku=sku,
            marketplaceIds=MARKETPLACE_ID,
            body=patch_body,
        )
    except Exception as e:
        return False, str(e)[:300]
    status = (resp.payload or {}).get("status", "")
    if status in ("ACCEPTED", "VALID"):
        with get_db() as c:
            c.execute(
                "UPDATE inventory SET listing_price=?, price_updated_at=? "
                "WHERE seller_sku=?",
                (float(new_price), datetime.utcnow().isoformat(), sku),
            )
            c.execute("""
                INSERT INTO price_change_log(seller_sku, new_price, reason, executed_at, success, error_message)
                VALUES(?, ?, 'inline update (UI)', ?, 1, NULL)
            """, (sku, float(new_price), datetime.utcnow().isoformat()))
        return True, None
    issues = (resp.payload or {}).get("issues") or []
    err = f"status={status} issues={issues[:3]}"[:400]
    with get_db() as c:
        c.execute("""
            INSERT INTO price_change_log(seller_sku, new_price, reason, executed_at, success, error_message)
            VALUES(?, ?, 'inline update (UI)', ?, 0, ?)
        """, (sku, float(new_price), datetime.utcnow().isoformat(), err))
    return False, err


_COND_ORDER = {"new": 5, "like_new": 4, "very_good": 3, "good": 2, "acceptable": 1}

# Amazon SP-API の condition_id（数値）→ サブコンディション名
# https://developer-docs.amazon.com/sp-api/docs/inventory-and-listings#conditionid
_COND_ID_MAP = {
    "11": "new", "10": "new",
    "1": "like_new",
    "2": "very_good",
    "3": "good",
    "4": "acceptable",
    "5": "acceptable",  # poor は acceptable と同じ最下位ランクで扱う
}


def _normalize_condition(cond: str | None) -> str:
    """コンディション表記を正規化キー（new / like_new / very_good / good / acceptable）に統一。

    対応する入力例:
      - 'New' / 'Used - Very Good' / 'very_good' / 'Used; Good' （文字列）
      - '11' / '2' / '3'（Amazon SP-API condition_id 数値文字列）
      - 不明値 → '' を返す（呼び出し側でコンディションフィルタ無効化）
    """
    if not cond:
        return ""
    s = str(cond).lower().strip().replace(" ", "_").replace("-", "_").replace(";", "_")
    # 数値 ID
    if s in _COND_ID_MAP:
        return _COND_ID_MAP[s]
    # 文字列パターン
    if "new" in s and "like" not in s:
        return "new"
    if "like" in s:
        return "like_new"
    if "very" in s or "very_good" in s:
        return "very_good"
    if "good" in s and "very" not in s:
        return "good"
    if "accept" in s or "可" in s or "poor" in s:
        return "acceptable"
    # 未知（"3" のような数値が _COND_ID_MAP に無い等）はフィルタ無効化のため空
    return ""


def _min_price_from_offers(offers_json_str: str | None,
                           *,
                           fba_only: bool = False,
                           cart_only: bool = False,
                           condition_filter: str | None = None,
                           condition_match: str = "same",
                           exclude_acceptable: bool = True,
                           exclude_seller_id: str | None = None) -> float | None:
    """offers_json から最低価格を計算する。

    Args:
        offers_json_str: inventory.offers_json
        fba_only: True なら FBA 出品のみ
        cart_only: True ならカート獲得オファーのみ（is_cart=True）
        condition_filter: 自分のコンディション。None なら状態フィルタ無効
        condition_match: condition_filter 指定時の比較方法
            "same"           = 完全同一コンディションのみ（既定 = 'XXX_condition' モード用）
            "same_or_better" = 同等以上
        exclude_acceptable: True (既定) なら acceptable / poor のオファーは除外。
            ユーザー方針「最低品質帯は比較対象に含めない（自分の品位が下がるため）」。
    """
    if not offers_json_str:
        return None
    import json as _json
    try:
        offers = _json.loads(offers_json_str)
    except Exception:
        return None
    my_norm = _normalize_condition(condition_filter) if condition_filter else ""
    my_rank = _COND_ORDER.get(my_norm, 0)
    candidates = []
    for o in offers:
        # 自分のオファー除外（自分2出品の価格競争防止）
        if exclude_seller_id and o.get("seller_id") == exclude_seller_id:
            continue
        if fba_only and o.get("fulfillment") != "FBA":
            continue
        if cart_only and not o.get("is_cart"):
            continue
        other_norm = _normalize_condition(o.get("sub_condition"))
        # acceptable / poor を除外
        if exclude_acceptable and other_norm == "acceptable":
            continue
        if my_norm:
            if condition_match == "same":
                if other_norm != my_norm:
                    continue
            else:  # same_or_better
                rank = _COND_ORDER.get(other_norm, 0)
                if rank < my_rank:
                    continue
        t = o.get("total") or o.get("price")
        if t:
            try:
                candidates.append(float(t))
            except (TypeError, ValueError):
                pass
    return min(candidates) if candidates else None


def decide_new_price(inventory_row: dict, rule_row: dict) -> tuple[float | None, str]:
    """SKU の新価格を決定（プリセット5モード + ストッパー）。

    Returns:
        (new_price, reason) — new_price が None なら変更なし
    """
    mode = rule_row.get("mode", "none")
    if mode in (None, "none", ""):
        return None, "mode=none"

    current = inventory_row.get("listing_price")
    if not current:
        return None, "現価格不明"

    target = None
    my_cond = inventory_row.get("product_condition")
    offers_json = inventory_row.get("offers_json")
    # 自分の Seller ID（自分の他出品との価格戦争を防ぐため除外用）
    my_seller_id = rule_row.get("_my_seller_id")
    _kw = {"exclude_seller_id": my_seller_id}
    if mode == "fba_condition":
        # FBA 出品 × 自分のコンディションと完全同一の最低価格（自分除外）
        target = _min_price_from_offers(offers_json, fba_only=True, condition_filter=my_cond, **_kw)
        if not target:
            target = inventory_row.get("min_price_fba")  # フォールバック
    elif mode == "all_condition":
        target = _min_price_from_offers(offers_json, fba_only=False, condition_filter=my_cond, **_kw)
        if not target:
            target = inventory_row.get("min_price_all")
    elif mode == "fba_min":
        target = _min_price_from_offers(offers_json, fba_only=True, **_kw)
        if not target:
            target = inventory_row.get("min_price_fba")
    elif mode == "all_min":
        target = (_min_price_from_offers(offers_json, fba_only=False, **_kw)
                  or inventory_row.get("min_price_all"))
    elif mode == "cart":
        target = _min_price_from_offers(offers_json, cart_only=True, **_kw)
        if not target:
            target = inventory_row.get("cart_price")
    else:
        return None, f"unknown mode: {mode}"

    if not target:
        return None, "競合データなし"

    target = float(target)

    # 追従挙動: settings の match_strategy で「同額 / X円安く / Y%安く」
    strategy = rule_row.get("_match_strategy") or "match"  # match / yen / pct
    offset_yen = float(rule_row.get("_match_offset_yen") or 0)
    offset_pct = float(rule_row.get("_match_offset_pct") or 0)
    if strategy == "yen" and offset_yen > 0:
        target = target - offset_yen
    elif strategy == "pct" and offset_pct > 0:
        target = round(target * (1 - offset_pct / 100))

    # ストッパー適用
    high = rule_row.get("high_stopper")
    low = rule_row.get("low_stopper")
    if high and target > float(high):
        target = float(high)
    if low and target < float(low):
        return None, f"赤字ストッパー ({low}) 未満、変更しない"

    # 1円未満の差は変更なし
    if abs(target - float(current)) < 1:
        return None, "現価格と同等、変更不要"

    return target, f"mode={mode} target={target}"


def run_engine(dry_run: bool = True, apply_updates: bool = False) -> dict:
    """全 SKU の価格調整判定を実行。

    dry_run=True: 判定のみ（DB ログ、SP-API は叩かない）
    apply_updates=True: 実際に SP-API PATCH で価格更新
    """
    result = {
        "started_at": datetime.utcnow().isoformat(),
        "evaluated": 0,
        "would_change": 0,
        "changed": 0,
        "errors": 0,
        "details": [],
    }

    # グローバル設定から取得（自分の Seller ID + 追従挙動）
    try:
        from scripts.common.sp_api_client import get_seller_id
        my_seller_id = get_seller_id(SHOP)
    except Exception:
        my_seller_id = None

    with get_db() as conn:
        # 追従挙動の設定を取得
        match_strategy = (get_setting("match_strategy", "match") or "match")
        match_offset_yen = float(get_setting("match_offset_yen", "0") or 0)
        match_offset_pct = float(get_setting("match_offset_pct", "0") or 0)

        rows = conn.execute("""
            SELECT inv.*, pr.mode, pr.high_stopper, pr.low_stopper, pr.active,
                   cp.cost_price
            FROM price_rules pr
            JOIN inventory inv ON inv.seller_sku = pr.seller_sku
            LEFT JOIN cost_prices cp ON cp.seller_sku = inv.seller_sku
            WHERE pr.active = 1
              AND pr.mode IS NOT NULL AND pr.mode != 'none'
              AND inv.status LIKE 'Active%'
              AND inv.quantity > 0
        """).fetchall()

    li_client = None
    for r in rows:
        result["evaluated"] += 1
        inv = dict(r)
        rule = {
            "mode": r["mode"],
            "high_stopper": r["high_stopper"],
            "low_stopper": r["low_stopper"] or r["cost_price"],  # 赤字ストッパー未指定なら仕入値
            "_my_seller_id": my_seller_id,
            "_match_strategy": match_strategy,
            "_match_offset_yen": match_offset_yen,
            "_match_offset_pct": match_offset_pct,
        }
        new_price, reason = decide_new_price(inv, rule)

        if new_price is None:
            continue
        result["would_change"] += 1

        if dry_run and not apply_updates:
            # DB にログのみ
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO price_change_log(seller_sku, old_price, new_price,
                                                 reason, executed_at, success, error_message)
                    VALUES(?,?,?,?,?,0,'DRY_RUN')
                """, (r["seller_sku"], r["listing_price"], new_price,
                      reason, datetime.utcnow().isoformat()))
            result["details"].append({
                "sku": r["seller_sku"],
                "old": r["listing_price"],
                "new": new_price,
                "reason": reason,
                "dry_run": True,
            })
            continue

        # 実行モード: Listings Items PATCH
        if li_client is None:
            from scripts.common.sp_api_client import _load_credentials, get_seller_id
            creds = _load_credentials(SHOP)
            seller_id = get_seller_id(SHOP)
            li_client = (ListingsItemsV20210801(credentials=creds, marketplace=Marketplaces.JP),
                         seller_id)
        li, seller_id = li_client

        # productType を取得（無ければ SP-API から取って保存）
        product_type = r["product_type"] if "product_type" in r.keys() else None
        if not product_type:
            try:
                rt = li.get_listings_item(
                    sellerId=seller_id, sku=r["seller_sku"],
                    marketplaceIds=MARKETPLACE_ID,
                    includedData=["summaries"],
                )
                summaries = (rt.payload or {}).get("summaries", []) or []
                if summaries:
                    product_type = summaries[0].get("productType")
                if product_type:
                    with get_db() as c:
                        c.execute(
                            "UPDATE inventory SET product_type=? WHERE seller_sku=?",
                            (product_type, r["seller_sku"]),
                        )
            except Exception:
                product_type = None

        try:
            if not product_type:
                raise RuntimeError("productType unknown")
            # Listings Items 2021-08-01 PATCH: purchasable_offer 差し替え
            patch_body = {
                "productType": product_type,
                "patches": [{
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [{
                        "marketplace_id": MARKETPLACE_ID,
                        "currency": "JPY",
                        "our_price": [{
                            "schedule": [{"value_with_tax": float(new_price)}]
                        }]
                    }]
                }]
            }
            resp = li.patch_listings_item(
                sellerId=seller_id, sku=r["seller_sku"],
                marketplaceIds=MARKETPLACE_ID,
                body=patch_body,
            )
            status = (resp.payload or {}).get("status", "")
            if status in ("ACCEPTED", "VALID"):
                success = 1
                err = None
                # DB 側の listing_price も即時更新
                with get_db() as c:
                    c.execute(
                        "UPDATE inventory SET listing_price=?, price_updated_at=? "
                        "WHERE seller_sku=?",
                        (float(new_price), datetime.utcnow().isoformat(), r["seller_sku"]),
                    )
                result["changed"] += 1
            else:
                success = 0
                err = f"status={status}, issues={((resp.payload or {}).get('issues') or [])[:3]}"[:400]
                result["errors"] += 1
        except Exception as e:
            success = 0
            err = str(e)[:400]
            result["errors"] += 1

        with get_db() as conn:
            conn.execute("""
                INSERT INTO price_change_log(seller_sku, old_price, new_price,
                                             reason, executed_at, success, error_message)
                VALUES(?,?,?,?,?,?,?)
            """, (r["seller_sku"], r["listing_price"], new_price, reason,
                  datetime.utcnow().isoformat(), success, err))

        result["details"].append({
            "sku": r["seller_sku"],
            "old": r["listing_price"],
            "new": new_price,
            "reason": reason,
            "success": success,
            "error": err,
        })

    result["finished_at"] = datetime.utcnow().isoformat()
    return result


if __name__ == "__main__":
    r = run_engine(dry_run=True)
    print(json.dumps(r, ensure_ascii=False, indent=2))
