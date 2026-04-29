"""seller-dashboard Flask アプリケーション"""
import io
import os
import sys
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, login_user, logout_user, current_user

# Windows cp932 対策
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import config
from db import init_db, get_db, get_setting, set_setting
from auth import login_manager, verify_password, ensure_initial_user, has_any_user, create_user
from polling import run_all_polling, run_light_refresh


# 商品コンディション 英語→日本語 マッピング
CONDITION_JP = {
    "new": "新品",
    "new_new": "新品",
    "used_like_new": "ほぼ新品",
    "used_very_good": "非常に良い",
    "used_good": "良い",
    "used_acceptable": "可",
    "collectible_like_new": "コレクター品 ほぼ新品",
    "collectible_very_good": "コレクター品 非常に良い",
    "collectible_good": "コレクター品 良い",
    "collectible_acceptable": "コレクター品 可",
    "refurbished_refurbished": "整備品",
    # Merchant Listings Report の数値コード
    "1": "ほぼ新品",
    "2": "非常に良い",
    "3": "良い",
    "4": "可",
    "5": "コレクター品 ほぼ新品",
    "6": "コレクター品 非常に良い",
    "7": "コレクター品 良い",
    "8": "コレクター品 可",
    "10": "整備品",
    "11": "新品",
}


def condition_jp(cond: str) -> str:
    if not cond:
        return ""
    return CONDITION_JP.get(cond.lower(), cond)


# Amazon 紹介料率の推定（レンズ単体=10%、カメラ本体・その他=8%）
# 判定優先順位:
#   1. レンズキット → カメラ本体扱い（8%）
#   2. カメラ本体キーワード（デジタルカメラ/ミラーレス/一眼レフ等）→ 8%
#   3. 純レンズメーカー名 → 10%
#   4. レンズ系キーワード（レンズ/NIKKOR/M.Zuiko/etc）→ 10%
#   5. デフォルト → 8%
CAMERA_BODY_KEYWORDS = (
    "デジタルカメラ", "ミラーレス", "一眼レフ", "一眼カメラ",
    "コンパクトカメラ", "コンパクトデジ", "コンデジ",
    "防水カメラ", "防水デジタル", "ビデオカメラ", "アクションカム",
    "GoPro", "Action Cam",
)
# 「キット」を含む商品はすべてカメラ本体扱い（レンズキット・ダブルズームキット・
#  ボディキット・トリプルレンズキット など全て該当）
KIT_KEYWORD = "キット"
PURE_LENS_BRANDS = (
    # 英字（大文字比較）。SIGMA はカメラ本体(fp等)もあるが、それらは
    # "デジタルカメラ"/"ミラーレス" キーワードで先に分類される（ルール2）。
    "TAMRON", "SIGMA", "TOKINA", "VOIGTLANDER", "LAOWA",
    "SAMYANG", "ROKINON",
    # 日本語表記
    "シグマ", "タムロン", "トキナー", "フォクトレンダー",
)
LENS_HINT_KEYWORDS = (
    "レンズ", "ﾚﾝｽﾞ", "lens",
    "nikkor", "ニッコール",
    "m.zuiko", "zuiko", "ズイコー",
    "zeiss", "carl zeiss", "ツァイス",
    "エクステンダー", "テレコンバーター", "テレコン",
)


def _load_bs_column(year_month: str, auto_inventory: float, auto_profit: float,
                    auto_unlisted: float = 0) -> dict:
    """B/S の1列分を組み立てる（期首 or 期末/任意月）。
    自動項目（棚卸資産・Amazon未上場在庫・当期純利益）は引数の値、その他は balance_sheet テーブル。
    DB に保存値があればそれを優先（ユーザーが固定した値を尊重）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT category, amount, note FROM balance_sheet WHERE year_month=?",
            (year_month,),
        ).fetchall()
        saved = {r["category"]: dict(r) for r in rows}

    sections = []
    totals = {"asset": 0, "liability": 0, "equity": 0}
    auto_value_map = {
        "棚卸資産":         auto_inventory,
        "Amazon未上場在庫": auto_unlisted,
        "当期純利益":       auto_profit,
    }
    for side, subgroup, items in BS_DEFINITION:
        # NOTE: Jinja2 で `dict.items` は組み込み method と衝突するためキー名を `entries` にする
        sec = {"side": side, "subgroup": subgroup, "entries": []}
        for cat, kind in items:
            saved_row = saved.get(cat)
            if kind == "auto":
                # 自動: 保存値があればそれ、無ければ計算値
                if saved_row and saved_row["amount"] is not None:
                    amt = saved_row["amount"] or 0
                else:
                    amt = auto_value_map.get(cat, 0)
                note = (saved_row["note"] if saved_row else "") or ""
            else:
                amt = (saved_row["amount"] if saved_row else 0) or 0
                note = (saved_row["note"] if saved_row else "") or ""
            sec["entries"].append({"category": cat, "kind": kind, "amount": amt, "note": note})
            # 事業主貸は純資産の減算項目（個人への流出。資産扱いだが純資産から控除）
            if cat == "事業主貸":
                totals[side] -= amt
            else:
                totals[side] += amt
        sections.append(sec)
    totals["liability_equity"] = totals["liability"] + totals["equity"]
    totals["balance_diff"] = totals["asset"] - totals["liability_equity"]
    return {"year_month": year_month, "sections": sections, "totals": totals}


def _calc_cumulative_profit(year: int, end_month: int) -> float:
    """指定年の 1/1 〜 end_month末 までの当期純利益（簡易計算）"""
    with get_db() as conn:
        # 売上・仕入・確定手数料を範囲で集計
        start = f"{year:04d}-01-01"
        end_dt = datetime(year, end_month, 1)
        if end_month == 12:
            end = f"{year:04d}-12-31"
        else:
            # 月末
            from calendar import monthrange
            last_day = monthrange(year, end_month)[1]
            end = f"{year:04d}-{end_month:02d}-{last_day:02d}"

        rows = conn.execute("""
            SELECT oi.item_price, oi.quantity_ordered, oi.amazon_fee, oi.amazon_fee_confirmed,
                   oi.title, oi.shipping_price, oi.promotion_discount,
                   cp.cost_price, r.id AS return_id
            FROM orders o
            JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
            LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
            LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
            WHERE substr(o.purchase_date, 1, 10) BETWEEN ? AND ?
              AND o.order_status IN ('Shipped', 'Unshipped')
        """, (start, end)).fetchall()

        rm = get_setting("profit_return_model", "exclude")
        sales = cost = fee_calc = ship_inc = promo = refund = 0
        qty = 0
        for r in rows:
            q = r["quantity_ordered"] or 0
            p = r["item_price"] or 0
            is_ret = bool(r["return_id"])
            if is_ret:
                refund += p * q
                if rm == "exclude":
                    continue
            qty += q
            sales += p * q
            cost += (r["cost_price"] or 0) * q
            ship_inc += r["shipping_price"] or 0
            promo += r["promotion_discount"] or 0
            if r["amazon_fee_confirmed"] and r["amazon_fee"]:
                fee_calc += r["amazon_fee"]
            else:
                t = (r["title"] or "")
                rate = estimate_amazon_fee_rate(t, None)
                fee_calc += round(p * rate) * q

        # 月次経費 + Amazon利用料
        ym_set = []
        cur = datetime(year, 1, 1)
        while cur <= end_dt:
            ym_set.append(cur.strftime("%Y-%m"))
            cur = cur.replace(month=cur.month + 1) if cur.month < 12 else cur.replace(year=cur.year+1, month=1)
        other_exp = amz_from_exp = non_op = 0
        if ym_set:
            ph = ",".join(["?"] * len(ym_set))
            for er in conn.execute(
                f"SELECT category, SUM(amount) AS total FROM expenses WHERE year_month IN ({ph}) GROUP BY category",
                tuple(ym_set),
            ).fetchall():
                amt = er["total"] or 0
                if er["category"] == "Amazon利用料":
                    amz_from_exp += amt
                elif er["category"] == "プラス計上":
                    non_op += amt
                else:
                    other_exp += amt
        amz_total = fee_calc + amz_from_exp
        # 発送代行: 月別 base + per_item × ASIN登録数（販売済み含む全SKU）
        ship_total = 0
        for ym in ym_set:
            eff = ym + "-01"
            sa = conn.execute(
                "SELECT base_fee, per_item_fee FROM shipping_agent_fees "
                "WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
                (eff,)
            ).fetchone()
            m_base = (sa["base_fee"] if sa else 0) or 0
            m_per  = (sa["per_item_fee"] if sa else 0) or 0
            ym_slash = ym.replace("-", "/")
            arow = conn.execute(
                "SELECT COUNT(*) AS n FROM inventory WHERE substr(asin_listed_at,1,7)=?",
                (ym_slash,)
            ).fetchone()
            m_asin = (arow["n"] if arow else 0) or 0
            ship_total += m_base + m_per * m_asin
        refund_ded = refund if rm == "subtract_refund" else 0
        return (sales + ship_inc - cost - amz_total - other_exp - ship_total - promo - refund_ded + non_op)


def _compute_monthly_summary(year: int, month: int) -> dict:
    """指定月のP/L主要項目を集計して返す（月別比較表用）"""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    start_d = f"{year:04d}-{month:02d}-01"
    end_d   = f"{year:04d}-{month:02d}-{last_day:02d}"
    ym = f"{year:04d}-{month:02d}"

    with get_db() as conn:
        # 注文行（Pending/Canceled除外、Return扱いはexcludeモード）
        rows = conn.execute("""
            SELECT oi.item_price, oi.quantity_ordered, oi.amazon_fee, oi.amazon_fee_confirmed,
                   oi.title, oi.shipping_price, oi.promotion_discount,
                   cp.cost_price, r.id AS return_id
            FROM orders o
            JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
            LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
            LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
            WHERE substr(o.purchase_date, 1, 10) BETWEEN ? AND ?
              AND o.order_status IN ('Shipped','Unshipped')
        """, (start_d, end_d)).fetchall()

        sales = cost = amz_fee_calc = ship_inc = promo = qty = 0
        for r in rows:
            if r["return_id"]:
                continue  # excludeモード
            q = r["quantity_ordered"] or 0
            p = r["item_price"] or 0
            sales += p * q
            cost  += (r["cost_price"] or 0) * q
            ship_inc += r["shipping_price"] or 0
            promo += r["promotion_discount"] or 0
            qty += q
            if r["amazon_fee_confirmed"] and r["amazon_fee"]:
                amz_fee_calc += r["amazon_fee"]
            else:
                title = r["title"] or ""
                rate = estimate_amazon_fee_rate(title, None)
                amz_fee_calc += round(p * rate) * q

        # 経費（その月分）
        amz_fee_exp = 0
        ship_other = 0
        other_exp_by_tc = {}
        non_op = 0
        for r in conn.execute(
            "SELECT category, tax_category, SUM(amount) AS amt FROM expenses WHERE year_month=? GROUP BY category, tax_category",
            (ym,)
        ).fetchall():
            cat = r["category"]
            tc  = r["tax_category"]
            amt = r["amt"] or 0
            if cat == "Amazon利用料":
                amz_fee_exp += amt
                tc = tc or "支払手数料"
            elif cat == "プラス計上":
                non_op += amt
                continue
            elif cat == "発送代行その他":
                ship_other += amt
                tc = tc or "荷造運賃"
            if tc and amt:
                other_exp_by_tc[tc] = other_exp_by_tc.get(tc, 0) + amt

        # 発送代行 base + per_item × ASIN登録数
        eff = f"{ym}-01"
        sa = conn.execute(
            "SELECT base_fee, per_item_fee FROM shipping_agent_fees "
            "WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
            (eff,)
        ).fetchone()
        m_base = (sa["base_fee"] if sa else 0) or 0
        m_per  = (sa["per_item_fee"] if sa else 0) or 0
        ym_slash = ym.replace("-", "/")
        arow = conn.execute(
            "SELECT COUNT(*) AS n FROM inventory WHERE substr(asin_listed_at,1,7)=?",
            (ym_slash,)
        ).fetchone()
        m_asin = (arow["n"] if arow else 0) or 0
        ship_auto = m_base + m_per * m_asin

    # tax_cat_breakdown 完成
    if amz_fee_calc:
        other_exp_by_tc["支払手数料"] = other_exp_by_tc.get("支払手数料", 0) + amz_fee_calc
    if ship_auto:
        other_exp_by_tc["荷造運賃"] = other_exp_by_tc.get("荷造運賃", 0) + ship_auto

    sga_total = sum(other_exp_by_tc.values())
    gross_sales = sales + ship_inc
    net_sales = gross_sales - promo
    gross_profit = net_sales - cost
    op_profit = gross_profit - sga_total
    net_profit = op_profit + non_op

    return {
        "ym": ym,
        "month": month,
        "qty": qty,
        "gross_sales": gross_sales,
        "promo": promo,
        "net_sales": net_sales,
        "cost": cost,
        "gross_profit": gross_profit,
        "amz_fee": other_exp_by_tc.get("支払手数料", 0),
        "shipping": other_exp_by_tc.get("荷造運賃", 0),
        "other_sga": sga_total - other_exp_by_tc.get("支払手数料", 0) - other_exp_by_tc.get("荷造運賃", 0),
        "sga_total": sga_total,
        "op_profit": op_profit,
        "non_op": non_op,
        "net_profit": net_profit,
    }


def _build_bs(bs_year: int, bs_month: int, current_inventory_value: float,
              current_unlisted_value: float = 0) -> dict:
    """期首(1/1) + 選択月末 の2列 B/S を組み立てる"""
    ym_start = f"{bs_year:04d}-01"
    ym_end = f"{bs_year:04d}-{bs_month:02d}"

    # 期首: 棚卸資産 = 前年12月末の保存値（あれば）、無ければ 0
    with get_db() as conn:
        prev = conn.execute(
            "SELECT amount FROM balance_sheet WHERE year_month=? AND category='棚卸資産'",
            (f"{bs_year-1:04d}-12",),
        ).fetchone()
    start_inv = (prev["amount"] if prev else 0) or 0
    start_profit = 0  # 期首時点 = 0

    # 期首 Amazon未上場在庫: 保存値があればそれ、無ければ 0
    # （期中の動的計算は期末に対してのみ意味を持つ。期首は手入力で固定）
    start_unlisted = 0  # _load_bs_column 内で saved_row があればそちらを優先

    end_inv = current_inventory_value  # 選択月末 = 現在の在庫評価
    end_profit = _calc_cumulative_profit(bs_year, bs_month)
    end_unlisted = current_unlisted_value  # 選択月末 = 現在の未上場在庫評価

    return {
        "year": bs_year,
        "month": bs_month,
        "ym_start": ym_start,
        "ym_end": ym_end,
        "start": _load_bs_column(ym_start, start_inv, start_profit, start_unlisted),
        "end":   _load_bs_column(ym_end,   end_inv,   end_profit,   end_unlisted),
    }


def _build_pl(kpi: dict, tax_breakdown: dict, non_op_income: float) -> dict:
    """損益計算書 (P/L) を組み立てる。
       純売上高    = 総売上高 + 送料/ギフト入金 - 売上値引(プロモ割引)
       売上総利益  = 純売上高 - 売上原価
       営業利益    = 売上総利益 - 販管費
       当期純利益  = 営業利益 + 営業外収益(プラス計上) - 返金損失(subtract_refundモード時)
    """
    gross_sales = (kpi.get("sales_total") or 0) + (kpi.get("shipping_income") or 0)
    promo = kpi.get("promotion_total") or 0
    sales = gross_sales - promo  # 純売上高
    cost_of_sales = kpi.get("cost_total") or 0
    gross_profit = sales - cost_of_sales
    # 販売費及び一般管理費 = 費目別合計（仕入高は除く、それは売上原価扱い）
    sga_items = [(tc, amt) for tc, amt in
                 sorted(tax_breakdown.items(), key=lambda x: -x[1]) if amt > 0]
    sga_total = sum(amt for _, amt in sga_items)
    operating_profit = gross_profit - sga_total
    net_profit = operating_profit + non_op_income
    return {
        "gross_sales": gross_sales,
        "promo_discount": promo,
        "sales": sales,
        "cost_of_sales": cost_of_sales,
        "gross_profit": gross_profit,
        "sga_items": sga_items,
        "sga_total": sga_total,
        "operating_profit": operating_profit,
        "non_op_income": non_op_income,
        "net_profit": net_profit,
        "refund_count": kpi.get("refund_count") or 0,
        "refund_amount": kpi.get("refund_amount") or 0,
    }


def estimate_amazon_fee_rate(title: str, product_type: str | None = None) -> float:
    """Amazon 紹介料率を推定。確定値が取れない時の簡易推定。
    レンズ=10%、カメラ本体・その他=8%。"""
    pt = (product_type or "").upper()
    if "PHOTOGRAPHIC_LENS" in pt or "CAMERA_LENS" == pt:
        return 0.10
    t = title or ""
    upper_t = t.upper()
    lower_t = t.lower()
    # 1. 「キット」を含むものはカメラ本体扱い（最優先）
    if KIT_KEYWORD in t:
        return 0.08
    # 2. カメラ本体の明示キーワード
    if any(k in t for k in CAMERA_BODY_KEYWORDS):
        return 0.08
    # 3. 純レンズメーカー名（英字は大文字、日本語はそのまま）
    if any(b in upper_t for b in PURE_LENS_BRANDS if b.isascii()):
        return 0.10
    if any(b in t for b in PURE_LENS_BRANDS if not b.isascii()):
        return 0.10
    # 4. レンズ系キーワード（英字は小文字比較で大文字小文字無視）
    if any(k in lower_t for k in LENS_HINT_KEYWORDS if k.isascii()):
        return 0.10
    if any(k in t for k in LENS_HINT_KEYWORDS if not k.isascii()):
        return 0.10
    return 0.08


# ================================================================
# 経費の費目（青色申告 勘定科目）と表示項目の定義
# ================================================================
# 確定申告で使う標準勘定科目（その他経費の選択肢として提示）
TAX_CATEGORIES = [
    "給料賃金", "外注工賃", "減価償却費", "地代家賃", "利子割引料",
    "租税公課", "荷造運賃", "水道光熱費", "旅費交通費", "通信費",
    "広告宣伝費", "接待交際費", "損害保険料", "修繕費", "消耗品費",
    "福利厚生費", "雑費", "支払手数料",
]

# 経費画面の表示項目定義（表示名はユーザー慣れたものを維持、内部に費目を持たせる）
# (表示名, 費目, デフォルトメモ, 種別)
# 種別: "fixed"=固定費目, "auto"=自動集計, "user"=ユーザー選択, "income"=プラス計上
EXPENSE_DEF = [
    # 既存項目（表示名は維持、内部費目を割り当て）
    ("人件費",       "給料賃金",   "アルバイト・パート給与",         "fixed"),
    ("交通費",       "旅費交通費", "仕入・出張のための交通費",       "fixed"),
    ("送料",         "荷造運賃",   "発送代行とは別。自社梱包分の送料","fixed"),
    ("梱包材",       "荷造運賃",   "ダンボール・緩衝材等",           "fixed"),
    ("消耗品",       "消耗品費",   "事務用品・印刷用紙等",           "fixed"),
    ("通信費",       "通信費",     "ネット回線・電話代",             "fixed"),
    ("税金",         "租税公課",   "事業税・印紙等",                 "fixed"),
    # 新規追加項目（物販で一般的な費目）
    ("外注工賃",     "外注工賃",   "委託・外部業者への支払い",        "fixed"),
    ("水道光熱費",   "水道光熱費", "事務所分（按分含む）",            "fixed"),
    ("地代家賃",     "地代家賃",   "事務所家賃",                      "fixed"),
    ("広告宣伝費",   "広告宣伝費", "Amazon スポンサード等",           "fixed"),
    ("損害保険料",   "損害保険料", "在庫保険・火災保険",              "fixed"),
    ("接待交際費",   "接待交際費", "取引先との会食等",                "fixed"),
    ("修繕費",       "修繕費",     "PC・機材修理等",                  "fixed"),
    ("福利厚生費",   "福利厚生費", "従業員向け費用",                  "fixed"),
    # 自動集計
    ("Amazon利用料", "支払手数料", "Finances API 自動集計",           "auto"),
    # ユーザー自由項目（費目はドロップダウンで選択）
    ("その他経費①", None,         "",                                 "user"),
    ("その他経費②", None,         "",                                 "user"),
    ("その他経費③", None,         "",                                 "user"),
    # 収入扱い
    ("プラス計上",   None,         "利益に加算（売上以外の収入源）",   "income"),
]


# ================================================================
# 貸借対照表 (B/S) の項目定義（青色申告フォーマット準拠）
# ================================================================
# (side, subgroup, [(category, kind), ...])
# kind: "user"=ユーザー入力 / "auto"=システム計算
BS_DEFINITION = [
    ("asset", "流動資産", [
        ("現金",                "user"),
        ("普通預金",            "user"),
        ("当座預金",            "user"),
        ("定期預金",            "user"),
        ("売掛金",              "user"),
        ("棚卸資産",            "auto"),   # = Amazon Active 在庫の cost_price × quantity（自動）
        ("Amazon未上場在庫",    "user"),   # = 手入力（💡推奨ボタンで auto 計算値を反映可）
        ("前払金",              "user"),
        ("仮払金",              "user"),
    ]),
    ("asset", "固定資産", [
        ("建物",         "user"),
        ("車両運搬具",   "user"),
        ("工具器具備品", "user"),
        ("土地",         "user"),
        ("投資有価証券", "user"),
    ]),
    ("liability", "流動負債", [
        ("買掛金",       "user"),
        ("未払金",       "user"),
        ("未払消費税",   "user"),
        ("短期借入金",   "user"),
        ("預り金",       "user"),
        ("前受金",       "user"),
    ]),
    ("liability", "固定負債", [
        ("長期借入金",   "user"),
    ]),
    ("equity", "純資産", [
        ("元入金",       "user"),
        ("事業主借",     "user"),
        ("事業主貸",     "user"),
        ("当期純利益",   "auto"),  # = P/L の当期純利益
    ]),
]


# 状態ソート順（新品→ほぼ新品→非常に良い→良い→可→コレクター→整備品）
CONDITION_ORDER = {
    "新品": 0,
    "ほぼ新品": 1,
    "非常に良い": 2,
    "良い": 3,
    "可": 4,
    "コレクター品 ほぼ新品": 5,
    "コレクター品 非常に良い": 6,
    "コレクター品 良い": 7,
    "コレクター品 可": 8,
    "整備品": 9,
}


def _start_scheduler(app):
    """APScheduler を起動して定期 Polling を実行"""
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

    def job():
        with app.app_context():
            try:
                result = run_all_polling(days=7)
                app.logger.info(f"Polling done: {result['details']}")
            except Exception as e:
                app.logger.error(f"Polling error: {e}")

    def price_job():
        """価格自動調整: 有効 SKU に対して実行。auto_price_apply=1 のときのみ PATCH"""
        with app.app_context():
            try:
                from price_engine import run_engine
                apply = get_setting("auto_price_apply", "0") == "1"
                result = run_engine(dry_run=not apply, apply_updates=apply)
                app.logger.info(
                    f"Price engine: evaluated={result['evaluated']} "
                    f"would_change={result['would_change']} changed={result['changed']} "
                    f"errors={result['errors']}"
                )
            except Exception as e:
                app.logger.error(f"Price engine error: {e}")

    def cleanup_job():
        """polling_log の古いレコード削除（DB肥大化防止）。
        Polling 5分間隔で年10万件以上溜まるので、保持期間を超えたら削除。"""
        with app.app_context():
            try:
                from datetime import datetime as _dt2, timedelta as _td2
                cutoff = (_dt2.utcnow() - _td2(days=config.POLLING_LOG_RETAIN_DAYS)).isoformat()
                with get_db() as conn:
                    cur = conn.execute(
                        "DELETE FROM polling_log WHERE started_at < ?", (cutoff,)
                    )
                    deleted = cur.rowcount
                    # SQLite ファイルを縮小（VACUUM は別接続が必要）
                if deleted:
                    app.logger.info(f"polling_log cleanup: {deleted} rows deleted (cutoff={cutoff})")
            except Exception as e:
                app.logger.error(f"polling_log cleanup error: {e}")

    # Polling/Price Engine 間隔は config.py で集中管理（DBのpolling_intervalは未使用）。
    # Flask再起動でジョブの「初回」が間隔後になるため、起動直後にも1回走らせる。
    from datetime import datetime as _dt, timedelta as _td
    scheduler.add_job(job, "interval", minutes=config.POLLING_INTERVAL_MIN, id="polling",
                      replace_existing=True, next_run_time=_dt.now() + _td(seconds=30))
    scheduler.add_job(price_job, "interval", minutes=config.PRICE_ENGINE_INTERVAL_MIN, id="price_engine",
                      replace_existing=True, next_run_time=_dt.now() + _td(seconds=60))
    # polling_log クリーンアップ: 1日1回実行（起動5分後に1回目）
    scheduler.add_job(cleanup_job, "interval", days=1, id="cleanup_polling_log",
                      replace_existing=True, next_run_time=_dt.now() + _td(minutes=5))
    scheduler.start()
    app.scheduler = scheduler


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    # Remember-me cookie は30日。Cloudflare Access 経由の HTTPS 前提で Secure
    app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure 属性は付けない: localhost(http)/Cloudflare(https) 両方で動作させるため

    login_manager.init_app(app)

    # DB 初期化 & 初期ユーザー作成
    init_db()
    ensure_initial_user()

    # DBの設定値を環境変数に流し込む（設定画面で入力した値を SP-API/Sheets から透過利用）
    # .env が優先（既存環境は壊さない）
    from env_bootstrap import bootstrap_env_from_db
    bootstrap_env_from_db()

    # テンプレート全体にショップ名・最終同期時刻を注入
    @app.context_processor
    def _inject_shop():
        last_sync = ""
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT finished_at FROM polling_log "
                    "WHERE success=1 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row["finished_at"]:
                    # ISO形式（UTC保存） → JST に変換して "MM/DD HH:MM" に整形
                    try:
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        # naive な ISO 文字列を UTC として解釈し JST へ
                        dt_utc = _dt.fromisoformat(row["finished_at"].split(".")[0]).replace(tzinfo=_tz.utc)
                        dt_jst = dt_utc.astimezone(_tz(_td(hours=9)))
                        last_sync = dt_jst.strftime("%m/%d %H:%M")
                    except Exception:
                        last_sync = row["finished_at"][:16].replace("T", " ")
        except Exception:
            pass
        # 静的ファイルのキャッシュバスター: app.css の mtime を使う
        asset_ver = ""
        try:
            import os as _os
            css_path = _os.path.join(_os.path.dirname(__file__), "static", "css", "app.css")
            asset_ver = str(int(_os.path.getmtime(css_path)))
        except Exception:
            pass

        return {
            "shop_name": get_setting("shop_name", "My Shop") or "My Shop",
            "last_sync": last_sync,
            "asset_ver": asset_ver,
        }

    # ==========================================================
    # ==========================================================
    # キャッシュ制御: HTML（テンプレート）は常に最新を配信
    # ==========================================================
    @app.after_request
    def _no_cache_html(response):
        # text/html だけ no-cache（static は app.css?v=mtime のキャッシュバスタで OK）
        ctype = response.headers.get("Content-Type", "")
        if ctype.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # 認証
    # ==========================================================
    @app.before_request
    def _require_setup():
        """初期セットアップ未完了（user 0件）なら /setup へ強制リダイレクト。
        ただし /setup と静的ファイルは除外。"""
        if request.path.startswith("/static/") or request.path == "/setup":
            return None
        if not has_any_user():
            return redirect(url_for("setup"))
        return None

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """初回セットアップ画面: 管理者ユーザー＆ショップ名を作成。
        既に user がいれば /login へリダイレクト（再実行不可）。"""
        if has_any_user():
            return redirect(url_for("login"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            password2 = request.form.get("password_confirm") or ""
            shop_name = (request.form.get("shop_name") or "").strip()
            errors = []
            if len(username) < 3:
                errors.append("ユーザー名は3文字以上にしてください")
            if len(password) < 6:
                errors.append("パスワードは6文字以上にしてください")
            if password != password2:
                errors.append("パスワード（確認）が一致しません")
            if len(shop_name) < 1:
                errors.append("ショップ名を入力してください")
            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template("setup.html",
                                       username=username, shop_name=shop_name)
            # 作成
            create_user(username, password)
            from db import set_setting as _ss
            _ss("shop_name", shop_name)
            flash(f"✓ セットアップ完了。{username} でログインしてください", "success")
            return redirect(url_for("login"))
        return render_template("setup.html", username="admin", shop_name="My Shop")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = verify_password(
                request.form.get("username", ""),
                request.form.get("password", ""),
            )
            if user:
                remember = bool(request.form.get("remember"))
                login_user(user, remember=remember)
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("ユーザー名かパスワードが違います", "danger")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ==========================================================
    # ダッシュボード
    # ==========================================================
    @app.route("/")
    @login_required
    def dashboard():
        today = datetime.now().date().isoformat()
        with get_db() as conn:
            # 今月の売上・販売数・仕入・利益（プライスター方式 / 売上分析と同じ式）
            # Pending用に inventory.listing_price も取得（item_price が NULL/0 のときの推定価格）
            stats_rows = conn.execute("""
                SELECT oi.item_price, oi.quantity_ordered, oi.amazon_fee, oi.amazon_fee_confirmed,
                       oi.shipping_price, oi.promotion_discount,
                       oi.title, cp.cost_price, r.id AS return_id, o.order_status,
                       inv.listing_price AS listing_price
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
                LEFT JOIN inventory inv ON inv.seller_sku = oi.seller_sku
                LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                WHERE o.order_status IN ('Shipped', 'Pending', 'Unshipped')
                  AND substr(o.purchase_date, 1, 7) = substr(?, 1, 7)
            """, (today,)).fetchall()
            # 返品計算モード: exclude=返品を集計から除外（再出品で在庫に戻る想定）
            #                   subtract_refund=プライスター方式（総計から返金額控除）
            return_model = get_setting("profit_return_model", "exclude")
            qty_total = sales_total = cost_total = amz_fee = refund_amt = 0
            shipping_income = 0  # 売上分析と同じ式に揃える: 送料・ギフト入金
            promotion_total = 0  # プロモーション割引（控除）
            return_count = 0
            pending_qty = 0
            pending_sales = 0  # 価格分かる Pending 分のみ加算（参考値）
            for r in stats_rows:
                q = r["quantity_ordered"] or 0
                p = r["item_price"] or 0
                # Pending 注文（金額未確定、キャンセル可能性あり）は本集計対象外
                # ただし「保留中の販売」KPI には別途集計（item_price が NULL/0 なら listing_price で補完）
                if r["order_status"] == "Pending":
                    pending_qty += q
                    p_pending = p if p else (r["listing_price"] or 0)
                    pending_sales += p_pending * q
                    continue
                is_return = bool(r["return_id"])
                if is_return:
                    return_count += 1
                    if return_model == "exclude":
                        continue  # 完全除外
                    refund_amt += p * q
                qty_total += q
                sales_total += p * q
                cost_total += (r["cost_price"] or 0) * q
                shipping_income += r["shipping_price"] or 0
                promotion_total += r["promotion_discount"] or 0
                if r["amazon_fee_confirmed"] and r["amazon_fee"]:
                    amz_fee += r["amazon_fee"]
                else:
                    rate = estimate_amazon_fee_rate(r["title"] or "", None)
                    amz_fee += round(p * rate) * q
            ym = datetime.now().strftime("%Y-%m")
            # 売上分析と同じ式に揃える:
            # - other:   Amazon利用料・プラス計上を除く経費
            # - amz_fee_from_expense: Amazon利用料（FBA保管料・サブスク等の月次経費）
            other = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses "
                "WHERE year_month=? AND category NOT IN ('Amazon利用料','プラス計上')",
                (ym,),
            ).fetchone()[0] or 0
            amz_fee_from_expense = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses "
                "WHERE year_month=? AND category='Amazon利用料'",
                (ym,),
            ).fetchone()[0] or 0
            # 発送代行: 売上分析と同じく「base + per_item × その月の ASIN 登録数」
            ship_row = conn.execute(
                "SELECT base_fee, per_item_fee FROM shipping_agent_fees "
                "WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
                (ym + "-01",),
            ).fetchone()
            ship_base_val = ((ship_row["base_fee"] if ship_row else 0) or 0)
            ship_per_val = ((ship_row["per_item_fee"] if ship_row else 0) or 0)
            ym_slash = ym.replace("-", "/")
            asin_registered_in_month = conn.execute(
                "SELECT COUNT(*) FROM inventory "
                "WHERE substr(asin_listed_at, 1, 7) = ?",
                (ym_slash,),
            ).fetchone()[0] or 0
            ship_total = ship_base_val + ship_per_val * asin_registered_in_month
            # 売上分析と同じ式: 売上 + 送料 − 仕入 − Amazon手数料 − 経費 − 発送代行 − プロモ − 返金
            amz_fee_total = amz_fee + amz_fee_from_expense
            profit = (sales_total + shipping_income
                      - cost_total - amz_fee_total - other - ship_total
                      - promotion_total - refund_amt)
            # 現在の在庫数（Active かつ qty>0 の SKU 数 ＝ 出品中商品数）
            inventory_count = conn.execute("""
                SELECT COUNT(*) AS c FROM inventory
                WHERE status LIKE 'Active%' AND quantity > 0
            """).fetchone()["c"]
            stats = {"sales_total": sales_total, "qty_total": qty_total,
                     "cost_total": cost_total, "profit": profit,
                     "inventory_count": inventory_count,
                     "pending_qty": pending_qty, "pending_sales": pending_sales}

            returns_count = conn.execute(
                "SELECT COUNT(*) AS c FROM returns WHERE return_date > date('now', '-30 days')"
            ).fetchone()["c"]

            # 当月の日別売上＋利益（全日付で 0 埋め、累計線用）
            now = datetime.now()
            first = now.replace(day=1)
            if now.month == 12:
                next_first = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_first = now.replace(month=now.month + 1, day=1)
            days_in_month = (next_first - first).days

            # 日別の売上・仕入・手数料・数量を集計（返品は return_model に従う、Pending は除外）
            daily_rows = conn.execute("""
                SELECT substr(o.purchase_date, 1, 10) AS day,
                       oi.item_price, oi.quantity_ordered,
                       oi.amazon_fee, oi.amazon_fee_confirmed, oi.title,
                       oi.shipping_price, oi.promotion_discount,
                       cp.cost_price, r.id AS return_id, o.order_status
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
                LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                WHERE o.order_status IN ('Shipped', 'Pending', 'Unshipped')
                  AND substr(o.purchase_date, 1, 7) = substr(?, 1, 7)
            """, (today,)).fetchall()
            rm = get_setting("profit_return_model", "exclude")
            by_day_agg = {}  # day -> {sales, qty, cost, fee}
            for row in daily_rows:
                day = row["day"]
                # Pending は売価未確定のため集計外
                if row["order_status"] == "Pending":
                    continue
                is_ret = bool(row["return_id"])
                if is_ret and rm == "exclude":
                    continue
                q = row["quantity_ordered"] or 0
                p = row["item_price"] or 0
                if row["amazon_fee_confirmed"] and row["amazon_fee"]:
                    fee = row["amazon_fee"]
                else:
                    rate = estimate_amazon_fee_rate(row["title"] or "", None)
                    fee = round(p * rate) * q
                b = by_day_agg.setdefault(day, {"sales":0,"qty":0,"cost":0,"fee":0,"ship_in":0,"promo":0,"refund":0})
                b["sales"]   += p * q
                b["qty"]     += q
                b["cost"]    += (row["cost_price"] or 0) * q
                b["fee"]     += fee
                b["ship_in"] += row["shipping_price"] or 0
                b["promo"]   += row["promotion_discount"] or 0
                if is_ret:
                    b["refund"] += p * q
            # 固定費（発送代行・その他経費）は日割りで按分
            other = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses "
                "WHERE year_month=? AND category NOT IN ('Amazon利用料','プラス計上')",
                (ym,),
            ).fetchone()[0] or 0
            ship_row = conn.execute(
                "SELECT per_item_fee, base_fee FROM shipping_agent_fees ORDER BY effective_from DESC LIMIT 1"
            ).fetchone()
            ship_base_val = ((ship_row["base_fee"] if ship_row else 0) or 0)
            ship_per_val  = ((ship_row["per_item_fee"] if ship_row else 0) or 0)
            per_day_fixed = (ship_base_val + other) / days_in_month

            this_month = []
            cum_sales = 0
            cum_profit = 0
            for d in range(1, days_in_month + 1):
                day_iso = first.replace(day=d).strftime("%Y-%m-%d")
                b = by_day_agg.get(day_iso, {"sales":0,"qty":0,"cost":0,"fee":0,"ship_in":0,"promo":0,"refund":0})
                refund_deduction = b["refund"] if rm == "subtract_refund" else 0
                # 売上分析と同じ式に揃える: + 送料、− プロモ
                day_profit = (b["sales"] + b["ship_in"]
                              - b["cost"] - b["fee"]
                              - ship_per_val * b["qty"] - per_day_fixed
                              - b["promo"] - refund_deduction)
                cum_sales  += b["sales"]
                cum_profit += day_profit
                this_month.append({
                    "day": day_iso, "d": d,
                    "sales": b["sales"], "qty": b["qty"],
                    "profit": round(day_profit),
                    "cum": cum_sales, "cum_profit": round(cum_profit),
                })

            # 最新の売れた商品10件（Pendingは item_price が NULL/0 の場合 inventory.listing_price で補完）
            recent_sold = conn.execute("""
                SELECT o.purchase_date, oi.title, oi.seller_sku,
                       COALESCE(NULLIF(oi.item_price, 0), inv.listing_price) AS item_price,
                       (oi.item_price IS NULL OR oi.item_price = 0) AS price_estimated,
                       o.order_status
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN inventory inv ON inv.seller_sku = oi.seller_sku
                WHERE o.order_status IN ('Shipped', 'Pending', 'Unshipped')
                ORDER BY o.purchase_date DESC LIMIT 10
            """).fetchall()

        return render_template(
            "dashboard.html",
            stats=stats,
            returns_count=returns_count,
            recent_sold=recent_sold,
            this_month=this_month,
            year_month=datetime.now().strftime("%Y年%-m月") if os.name != 'nt' else datetime.now().strftime("%Y年%m月").replace("年0", "年"),
        )

    # ==========================================================
    # 注文一覧
    # ==========================================================
    @app.route("/orders")
    @login_required
    def orders():
        status = request.args.get("status", "all")
        period = request.args.get("period", "this")  # this / prev / days / all
        period_days = int(request.args.get("days", "30"))
        page = max(1, int(request.args.get("page", "1") or "1"))
        per_page = 100

        status_filter = ""
        if status == "unshipped":
            status_filter = "AND o.order_status = 'Unshipped'"
        elif status == "shipped":
            status_filter = "AND o.order_status = 'Shipped'"
        elif status == "pending":
            status_filter = "AND o.order_status = 'Pending'"
        elif status == "canceled":
            status_filter = "AND o.order_status IN ('Canceled','Cancelled')"
        elif status == "return":
            status_filter = "AND r.id IS NOT NULL"

        # 期間絞り込み
        now_dt = datetime.now()
        if period == "this":
            start_d = now_dt.replace(day=1).strftime("%Y-%m-%d")
            end_d = now_dt.strftime("%Y-%m-%d")
            date_where = "substr(o.purchase_date, 1, 10) BETWEEN ? AND ?"
            params = [start_d, end_d]
        elif period == "prev":
            first_this = now_dt.replace(day=1)
            prev_last = first_this - timedelta(days=1)
            start_d = prev_last.replace(day=1).strftime("%Y-%m-%d")
            end_d = prev_last.strftime("%Y-%m-%d")
            date_where = "substr(o.purchase_date, 1, 10) BETWEEN ? AND ?"
            params = [start_d, end_d]
        elif period == "all":
            date_where = "1=1"
            params = []
        else:  # days (過去N日)
            date_where = "o.purchase_date > datetime('now', '-' || ? || ' days')"
            params = [period_days]

        with get_db() as conn:
            # 総件数（現在のフィルタ適用後）
            count_row = conn.execute(f"""
                SELECT COUNT(*) AS cnt
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                WHERE {date_where} {status_filter}
            """, params).fetchone()
            total_count = count_row["cnt"] if count_row else 0
            total_pages = max(1, (total_count + per_page - 1) // per_page)

            # 内訳カウント（期間フィルタのみ適用、status フィルタは外して全カテゴリ集計）
            breakdown_rows = conn.execute(f"""
                SELECT
                  SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END) AS returns,
                  SUM(CASE WHEN r.id IS NULL AND o.order_status IN ('Shipped','Unshipped') THEN 1 ELSE 0 END) AS confirmed,
                  SUM(CASE WHEN r.id IS NULL AND o.order_status = 'Pending' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN r.id IS NULL AND o.order_status IN ('Canceled','Cancelled') THEN 1 ELSE 0 END) AS canceled,
                  COUNT(*) AS total
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                WHERE {date_where}
            """, params).fetchone()
            count_breakdown = {
                "total": (breakdown_rows["total"] if breakdown_rows else 0) or 0,
                "confirmed": (breakdown_rows["confirmed"] if breakdown_rows else 0) or 0,
                "pending": (breakdown_rows["pending"] if breakdown_rows else 0) or 0,
                "canceled": (breakdown_rows["canceled"] if breakdown_rows else 0) or 0,
                "returns": (breakdown_rows["returns"] if breakdown_rows else 0) or 0,
            }
            page = min(page, total_pages)
            offset = (page - 1) * per_page

            rows = conn.execute(f"""
                SELECT o.amazon_order_id, o.purchase_date, o.order_status,
                       oi.asin, oi.seller_sku, oi.title, oi.item_price, oi.quantity_ordered,
                       oi.amazon_fee, oi.amazon_fee_confirmed, oi.condition,
                       cp.cost_price,
                       r.id AS return_id, r.return_date, r.reason AS return_reason,
                       inv.asin_listed_at, inv.listing_price AS inv_listing_price,
                       (SELECT per_item_fee FROM shipping_agent_fees ORDER BY effective_from DESC LIMIT 1) AS shipping_agent_per_item
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
                LEFT JOIN inventory inv ON inv.seller_sku = oi.seller_sku
                LEFT JOIN returns r
                       ON r.amazon_order_id = o.amazon_order_id
                      AND r.seller_sku = oi.seller_sku
                WHERE {date_where}
                  {status_filter}
                ORDER BY o.purchase_date DESC
                LIMIT ? OFFSET ?
            """, params + [per_page, offset]).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            title = (d.get("title") or "")
            price = d.get("item_price") or 0
            # Pending で item_price が NULL/0 のときは inventory.listing_price を価格基準に使う
            price_for_fee = price if price else (d.get("inv_listing_price") or 0)
            # Amazon 手数料: SP-API Finances 確定値を優先、無ければ品種推定
            if d.get("amazon_fee_confirmed") and d.get("amazon_fee"):
                amz_fee = d.get("amazon_fee") or 0
                d["_fee_confirmed"] = True
            else:
                rate = estimate_amazon_fee_rate(title, None)
                amz_fee = round(price_for_fee * rate)
                d["_fee_confirmed"] = False
                d["_fee_rate"] = rate
            shipping_fee = d.get("shipping_agent_per_item") or 0
            cost = d.get("cost_price") or 0
            is_return = bool(d.get("return_id"))

            status_val = d.get("order_status") or ""
            is_pending = status_val == "Pending"
            is_canceled = status_val in ("Canceled", "Cancelled")
            if is_return:
                # 返品: Amazon手数料は通常 REVERSAL_REIMBURSEMENT で返金される（実質0）
                # 発送代行手数料は売り手負担（売上時の往路 + 場合によっては返送復路）
                # 商品は再出品で在庫に戻るため仕入は損失計上しない
                d["_display_status"] = "Return"
                d["_display_price"] = None
                d["_display_cost"] = None
                # 表示する手数料 = 発送代行（往路 1回、復路は推定が困難なので除外）
                # Amazon手数料は返金前提で 0 扱い
                d["_amz_fee_for_return"] = amz_fee  # 表示用に保持（返金見込み注記）
                d["_display_fee"] = shipping_fee
                # 粗利益 = -発送代行のみ（Amazon手数料は返金される前提）
                d["_display_profit"] = -shipping_fee if shipping_fee else 0
            elif is_pending or is_canceled:
                # Canceled: 何もかも未確定 → 全部 None
                # Pending: 推定値（listing_price/cost_price/手数料推定）でかっこ書き表示
                d["_display_status"] = status_val
                if is_canceled:
                    d["_display_price"] = None
                    d["_display_cost"] = None
                    d["_display_profit"] = None
                    d["_display_fee"] = None
                else:
                    fallback = d.get("inv_listing_price") or 0
                    eff_price = price if price else fallback
                    d["_display_price"] = eff_price if eff_price else None
                    d["_price_estimated"] = (not price) and bool(fallback)
                    d["_display_cost"] = cost if cost else None
                    d["_display_fee"] = amz_fee + shipping_fee
                    if eff_price and cost:
                        d["_display_profit"] = eff_price - amz_fee - shipping_fee - cost
                    else:
                        d["_display_profit"] = None
            else:
                d["_display_status"] = d.get("order_status")
                d["_display_price"] = price
                d["_display_cost"] = cost
                d["_display_profit"] = price - amz_fee - shipping_fee - cost
                d["_display_fee"] = amz_fee + shipping_fee
            d["_amz_fee"] = amz_fee
            d["_shipping_fee"] = shipping_fee
            d["_is_return"] = is_return

            # ASIN登録日と販売日数（注文日 − ASIN登録日）
            listed_raw = d.get("asin_listed_at") or ""
            d["_listed_short"] = listed_raw[:10].replace("/", "-") if listed_raw else None
            try:
                if listed_raw and d.get("purchase_date"):
                    # listed: "2026/02/02 16:27:54 JST" / purchase_date: "2026-02-15T..."
                    listed_dt = datetime.strptime(listed_raw[:10], "%Y/%m/%d")
                    purchased_dt = datetime.strptime(d["purchase_date"][:10], "%Y-%m-%d")
                    d["_days_to_sell"] = (purchased_dt - listed_dt).days
                else:
                    d["_days_to_sell"] = None
            except Exception:
                d["_days_to_sell"] = None

            items.append(d)

        return render_template("orders.html", orders=items, status=status, days=period_days, period=period,
                               page=page, total_pages=total_pages, total_count=total_count, per_page=per_page,
                               count_breakdown=count_breakdown)

    # ==========================================================
    # 在庫一覧（大幅拡張）
    # ==========================================================
    @app.route("/inventory")
    @login_required
    def inventory():
        # Active 出品 かつ 在庫あり のみ表示（在庫切れ・Inactive は除外）
        hide_zero = True
        zero_filter = "WHERE inv.status LIKE 'Active%' AND inv.quantity > 0"
        try:
            diverge_threshold = int(get_setting("price_diverge_threshold", "1000") or "1000")
        except Exception:
            diverge_threshold = 1000
        with get_db() as _c:
            _row = _c.execute("SELECT MIN(substr(purchase_date,1,10)) FROM orders").fetchone()
            orders_since = (_row[0] if _row and _row[0] else "—")
            # 仕入値未記入の SKU（Active+qty>0 が対象、cost_prices が NULL or 0）
            missing_cost = _c.execute("""
                SELECT inv.seller_sku
                FROM inventory inv
                LEFT JOIN cost_prices cp ON cp.seller_sku = inv.seller_sku
                WHERE inv.status LIKE 'Active%' AND inv.quantity > 0
                  AND (cp.cost_price IS NULL OR cp.cost_price = 0)
                ORDER BY inv.seller_sku
            """).fetchall()
            missing_cost_skus = [r["seller_sku"] for r in missing_cost]

        with get_db() as conn:
            rows = conn.execute(f"""
                SELECT inv.*, cp.cost_price, pr.mode, pr.high_stopper, pr.low_stopper,
                       -- 発送代行手数料の最新値
                       (SELECT per_item_fee FROM shipping_agent_fees ORDER BY effective_from DESC LIMIT 1) AS shipping_agent_per_item,
                       -- 該当 SKU の Amazon 手数料平均（過去の実売データより）
                       (SELECT AVG(amazon_fee) FROM order_items
                        WHERE seller_sku = inv.seller_sku AND amazon_fee_confirmed=1) AS avg_amazon_fee,
                       -- 同 ASIN の過去手数料率の平均（手数料/売価）
                       (SELECT AVG(amazon_fee * 1.0 / NULLIF(item_price, 0))
                        FROM order_items
                        WHERE asin = inv.asin AND amazon_fee_confirmed=1
                          AND item_price > 0) AS asin_fee_rate,
                       -- 同 ASIN の累計販売個数（DB の orders 取得期間内）
                       (SELECT COALESCE(SUM(oi2.quantity_ordered), 0)
                        FROM order_items oi2
                        JOIN orders o2 ON o2.amazon_order_id = oi2.amazon_order_id
                        WHERE oi2.asin = inv.asin
                          AND o2.order_status IN ('Shipped','Unshipped')) AS asin_sold_count,
                       -- 同 ASIN の最終販売日
                       (SELECT MAX(substr(o3.purchase_date, 1, 10))
                        FROM order_items oi3
                        JOIN orders o3 ON o3.amazon_order_id = oi3.amazon_order_id
                        WHERE oi3.asin = inv.asin
                          AND o3.order_status IN ('Shipped','Unshipped')) AS asin_last_sold,
                       -- カート獲得判定: 出品価格が cart_price とほぼ一致
                       (CASE WHEN inv.listing_price IS NOT NULL AND inv.cart_price IS NOT NULL
                              AND ABS(inv.listing_price - inv.cart_price) < 50
                             THEN 1 ELSE 0 END) AS has_cart
                FROM inventory inv
                LEFT JOIN cost_prices cp ON cp.seller_sku = inv.seller_sku
                LEFT JOIN price_rules pr ON pr.seller_sku = inv.seller_sku
                {zero_filter}
                ORDER BY inv.updated_at DESC
            """).fetchall()

        # Python 側で利益計算
        items = []
        for r in rows:
            d = dict(r)
            listing = d.get("listing_price") or 0
            confirmed_fee = d.get("avg_amazon_fee") or 0
            asin_rate = d.get("asin_fee_rate") or 0
            # Amazon 手数料: 優先順位
            #   ① 同 SKU の過去確定手数料平均（中古は基本ヒットしない）
            #   ② 同 ASIN の過去手数料率平均 × 現出品価格
            #   ③ タイトルからの品種推定（8% / 10%）
            if confirmed_fee and confirmed_fee > 0:
                amazon_fee = confirmed_fee
                fee_source = "SKU実績"
            elif asin_rate and asin_rate > 0 and listing:
                amazon_fee = round(listing * asin_rate)
                fee_source = f"ASIN実績({asin_rate*100:.1f}%)"
            else:
                rate = estimate_amazon_fee_rate(d.get("title"), d.get("product_type"))
                amazon_fee = round(listing * rate) if listing else 0
                fee_source = f"推定({int(rate*100)}%)"
            shipping_fee = d.get("shipping_agent_per_item") or 0
            cost = d.get("cost_price") or 0
            profit = listing - amazon_fee - shipping_fee - cost
            profit_rate = (profit / listing * 100) if listing > 0 else 0
            d["_amazon_fee"] = amazon_fee
            d["_amazon_fee_source"] = fee_source
            d["_profit"] = profit
            d["_profit_rate"] = profit_rate
            d["_condition_jp"] = condition_jp(d.get("product_condition", ""))
            d["_condition_order"] = CONDITION_ORDER.get(d["_condition_jp"], 99)
            d["_amazon_url"] = f"https://www.amazon.co.jp/dp/{d.get('asin', '')}/" if d.get("asin") else ""
            d["_keepa_url"] = f"https://keepa.com/#!product/5-{d.get('asin', '')}" if d.get("asin") else ""
            # 最低価格の表示値: カート価格を優先、無ければ「良い以上」の最安値
            if d.get("cart_price"):
                d["_display_min"] = d["cart_price"]
                d["_min_source"] = "cart"
            else:
                d["_display_min"] = d.get("min_price_all")
                d["_min_source"] = "min"
            # 同コンディションの最安値と出品価格の乖離チェック・出品者数
            import json as _json
            same_cond_min = None
            offer_count_total = 0
            offer_count_same = 0
            try:
                offers = _json.loads(d.get("offers_json") or "[]")
                offer_count_total = len(offers)
                my_sub = {"1":"like_new","2":"very_good","3":"good","4":"acceptable","11":"new"}.get(
                    str(d.get("product_condition") or "").lower(), ""
                )
                same = [o for o in offers
                        if (o.get("sub_condition") or "").lower() == my_sub]
                offer_count_same = len(same)
                if same:
                    same_cond_min = min(o.get("total") or 0 for o in same)
            except Exception:
                pass
            d["_offer_count"] = offer_count_total
            d["_offer_count_same"] = offer_count_same
            d["_same_cond_min"] = same_cond_min
            d["_price_diverged"] = bool(
                listing and same_cond_min and (listing - same_cond_min) >= diverge_threshold
            )

            # 売れ行きランク（確率モデル）
            # p_per_day = (90日販売数/90) / 出品数
            # S: P(30日)>=70%, A: P(60日)>=70%, B: P(90日)>=70%, C: それ未満
            sales_90d = d.get("keepa_sales_90d")
            d["_rank"] = "?"
            d["_rank_p30"] = d["_rank_p60"] = d["_rank_p90"] = None
            if sales_90d and offer_count_total:
                p_per_day = (sales_90d / 90) / offer_count_total
                p30 = 1 - (1 - p_per_day) ** 30
                p60 = 1 - (1 - p_per_day) ** 60
                p90 = 1 - (1 - p_per_day) ** 90
                d["_rank_p30"] = p30
                d["_rank_p60"] = p60
                d["_rank_p90"] = p90
                if p30 >= 0.7:
                    d["_rank"] = "S"
                elif p60 >= 0.7:
                    d["_rank"] = "A"
                elif p90 >= 0.7:
                    d["_rank"] = "B"
                else:
                    d["_rank"] = "C"

            items.append(d)

        return render_template("inventory.html", inventory=items, hide_zero=hide_zero,
                               orders_since=orders_since,
                               missing_cost_skus=missing_cost_skus)

    # ==========================================================
    # 在庫画面のインライン編集
    # ==========================================================
    @app.route("/inventory/<sku>/update", methods=["POST"])
    @login_required
    def inventory_update(sku):
        """在庫画面からの編集（価格/仕入れ/追従モード/ストッパー）"""
        field = request.json.get("field") if request.is_json else request.form.get("field")
        value = request.json.get("value") if request.is_json else request.form.get("value")

        try:
            with get_db() as conn:
                if field == "listing_price":
                    new_price = float(value)
                    mode = get_setting("inline_price_apply_mode", "manual")
                    if mode == "auto":
                        # 即時 Amazon PATCH
                        from price_engine import patch_amazon_price
                        ok, err = patch_amazon_price(sku, new_price)
                        if not ok:
                            return jsonify({"error": f"Amazon 反映失敗: {err}",
                                             "db_only": True}), 502
                    else:
                        # manual: DB のみ更新、後でまとめて反映
                        now = datetime.now().isoformat()
                        conn.execute(
                            "UPDATE inventory SET listing_price=?, price_updated_at=? "
                            "WHERE seller_sku=?",
                            (new_price, now, sku),
                        )
                        # 「未反映」フラグとして price_change_log に記録
                        conn.execute("""
                            INSERT INTO price_change_log(seller_sku, new_price, reason, executed_at, success, error_message)
                            VALUES(?, ?, 'manual_pending', ?, 0, 'PENDING_PUSH')
                        """, (sku, new_price, now))
                elif field == "cost_price":
                    conn.execute("""
                        INSERT INTO cost_prices(seller_sku, cost_price, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(seller_sku) DO UPDATE SET cost_price=excluded.cost_price, updated_at=excluded.updated_at
                    """, (sku, float(value), datetime.now().isoformat()))
                elif field in ("mode", "high_stopper", "low_stopper"):
                    # upsert price_rules
                    col_val = None if value in ("", None) else (value if field == "mode" else float(value))
                    conn.execute(f"""
                        INSERT INTO price_rules(seller_sku, {field}, active, updated_at)
                        VALUES(?, ?, 1, ?)
                        ON CONFLICT(seller_sku) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at
                    """, (sku, col_val, datetime.now().isoformat()))
                else:
                    return jsonify({"error": "unknown field"}), 400
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ==========================================================
    # 経費管理
    # ==========================================================
    @app.route("/expenses", methods=["GET", "POST"])
    @login_required
    def expenses():
        year_month = request.args.get("ym", datetime.now().strftime("%Y-%m"))

        if request.method == "POST":
            ym = request.form.get("year_month")
            # expenses upsert
            # 全カテゴリを EXPENSE_DEF + 発送代行その他 で処理
            # （発送代行の 基本料・1商品手数料 は shipping_agent_fees テーブルへ
            #   その他手数料は expenses テーブル category="発送代行その他" へ）
            categories_to_save = [(d[0], d[1], d[3]) for d in EXPENSE_DEF]
            categories_to_save.append(("発送代行その他", "荷造運賃", "fixed"))
            for category, fixed_tax_cat, kind in categories_to_save:
                amount = request.form.get(f"amt_{category}", "0").replace(",", "")
                try:
                    amount = float(amount) if amount else 0
                except ValueError:
                    amount = 0
                note = (request.form.get(f"note_{category}") or "").strip()
                # 費目: 固定項目はマップから、ユーザー項目はフォームから取得
                if kind == "user":
                    user_tc = (request.form.get(f"tax_{category}") or "").strip() or None
                    tax_cat = user_tc if user_tc in TAX_CATEGORIES else None
                else:
                    tax_cat = fixed_tax_cat  # None for "income"
                with get_db() as conn:
                    is_auto_cat = (kind == "auto")
                    conn.execute("""
                        INSERT INTO expenses(year_month, category, amount, auto_calculated,
                                              repeat_monthly, note, tax_category)
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(year_month, category) DO UPDATE SET
                          amount = CASE WHEN excluded.auto_calculated=1 THEN expenses.amount ELSE excluded.amount END,
                          repeat_monthly = excluded.repeat_monthly,
                          note = excluded.note,
                          tax_category = excluded.tax_category
                    """, (ym, category, amount, 1 if is_auto_cat else 0,
                          1 if request.form.get(f"rep_{category}") else 0, note, tax_cat))

            # 発送代行 per-item fee（月別レコードを upsert、項目別の毎月繰り返しフラグ付き）
            per_item = request.form.get("per_item_fee", "0").replace(",", "")
            base_fee = request.form.get("base_fee", "0").replace(",", "")
            rep_base = 1 if request.form.get("rep_base_fee") else 0
            rep_per_item = 1 if request.form.get("rep_per_item_fee") else 0
            try:
                per_item_f = float(per_item) if per_item else 0
                base_f = float(base_fee) if base_fee else 0
                with get_db() as conn:
                    eff_from = f"{ym}-01"
                    conn.execute("""
                        INSERT INTO shipping_agent_fees(effective_from, base_fee, per_item_fee,
                                                         repeat_base, repeat_per_item)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(effective_from) DO UPDATE SET
                            base_fee=excluded.base_fee,
                            per_item_fee=excluded.per_item_fee,
                            repeat_base=excluded.repeat_base,
                            repeat_per_item=excluded.repeat_per_item
                    """, (eff_from, base_f, per_item_f, rep_base, rep_per_item))
            except ValueError:
                pass

            flash(f"{ym} の経費を保存しました", "success")
            return redirect(url_for("expenses", ym=ym))

        # GET 表示
        with get_db() as conn:
            expense_rows = conn.execute(
                "SELECT category, amount, repeat_monthly, note FROM expenses WHERE year_month=?",
                (year_month,)
            ).fetchall()
            ex_map = {r["category"]: dict(r) for r in expense_rows}

            # 「毎月繰り返し」自動継承:
            # 当月に該当カテゴリの行がなく、過去のいずれかの月で repeat_monthly=1 が
            # ある場合、その最新値を当月のデフォルトとして表示。保存時に当月レコードが作成される。
            inherited_rows = conn.execute("""
                SELECT category, amount, repeat_monthly, note FROM expenses e1
                WHERE repeat_monthly = 1
                  AND year_month < ?
                  AND year_month = (
                    SELECT MAX(year_month) FROM expenses e2
                    WHERE e2.category = e1.category
                      AND e2.repeat_monthly = 1
                      AND e2.year_month < ?
                  )
            """, (year_month, year_month)).fetchall()
            for r in inherited_rows:
                if r["category"] not in ex_map:
                    ex_map[r["category"]] = {**dict(r), "_inherited": True}

            # 発送代行: 基本料・1商品手数料は項目別に repeat フラグで継承
            eff = f"{year_month}-01"
            sa_self = conn.execute(
                "SELECT * FROM shipping_agent_fees WHERE effective_from=?",
                (eff,)
            ).fetchone()
            sa = {"effective_from": eff, "base_fee": 0, "per_item_fee": 0,
                  "repeat_base": 0, "repeat_per_item": 0}
            base_inherited = False
            per_item_inherited = False
            if sa_self:
                sa.update(dict(sa_self))
            # 基本料の継承（当月レコードに値がない / 0 の場合）
            if not sa_self or not (sa_self["base_fee"] or 0):
                row = conn.execute(
                    "SELECT base_fee, repeat_base FROM shipping_agent_fees "
                    "WHERE effective_from<? AND repeat_base=1 "
                    "ORDER BY effective_from DESC LIMIT 1",
                    (eff,),
                ).fetchone()
                if row:
                    sa["base_fee"] = row["base_fee"]
                    sa["repeat_base"] = 1
                    base_inherited = True
            # 1商品手数料の継承
            if not sa_self or not (sa_self["per_item_fee"] or 0):
                row = conn.execute(
                    "SELECT per_item_fee, repeat_per_item FROM shipping_agent_fees "
                    "WHERE effective_from<? AND repeat_per_item=1 "
                    "ORDER BY effective_from DESC LIMIT 1",
                    (eff,),
                ).fetchone()
                if row:
                    sa["per_item_fee"] = row["per_item_fee"]
                    sa["repeat_per_item"] = 1
                    per_item_inherited = True

        return render_template(
            "expenses.html",
            year_month=year_month,
            expense_map=ex_map,
            shipping_agent=sa,
            base_inherited=base_inherited,
            per_item_inherited=per_item_inherited,
            expense_def=EXPENSE_DEF,
            tax_categories=TAX_CATEGORIES,
        )

    # ==========================================================
    # 返品一覧
    # ==========================================================
    @app.route("/returns")
    @login_required
    def returns_page():
        period_days = int(request.args.get("days", "90"))
        with get_db() as conn:
            rows = conn.execute("""
                SELECT r.return_date, r.amazon_order_id, r.seller_sku, r.asin, r.fnsku,
                       r.quantity, r.reason, r.detailed_disposition,
                       r.fulfillment_center_id, r.customer_comments,
                       oi.title
                FROM returns r
                LEFT JOIN order_items oi
                       ON oi.amazon_order_id = r.amazon_order_id
                      AND oi.seller_sku = r.seller_sku
                WHERE r.return_date > datetime('now', '-' || ? || ' days')
                ORDER BY r.return_date DESC
            """, (period_days,)).fetchall()
        return render_template("returns.html", returns=rows, days=period_days)

    # ==========================================================
    # 売上分析
    # ==========================================================
    @app.route("/analytics")
    @app.route("/accounting")
    @login_required
    def analytics():
        """プライスター風の集計画面 兼 決算画面。
        /analytics: KPI・売上推移チャート（メイン）
        /accounting: 費目別集計・P/L・B/S（メイン）
        期間プリセット: 当月(this) / 前月(prev) / カスタム (from&to)"""
        preset = request.args.get("preset", "this")
        # アクティブタブの保持（期間変更してもタブが維持されるように）
        tab = request.args.get("tab")
        if tab not in ("monthly", "daily", "dow", "hour"):
            # 未指定: 年間 → 月別、それ以外 → 日別
            tab = "monthly" if preset == "year" else "daily"
        now = datetime.now()
        if preset == "prev":
            first_this = now.replace(day=1)
            prev_last  = first_this - timedelta(days=1)
            start_date = prev_last.replace(day=1).strftime("%Y-%m-%d")
            end_date   = prev_last.strftime("%Y-%m-%d")
        elif preset == "year":
            # 当年: 1月1日〜今日（月別グラフで12ヶ月分の棒が出る）
            start_date = now.replace(month=1, day=1).strftime("%Y-%m-%d")
            end_date   = now.strftime("%Y-%m-%d")
        elif preset == "custom":
            start_date = request.args.get("from") or now.replace(day=1).strftime("%Y-%m-%d")
            end_date   = request.args.get("to")   or now.strftime("%Y-%m-%d")
        else:  # this
            start_date = now.replace(day=1).strftime("%Y-%m-%d")
            end_date   = now.strftime("%Y-%m-%d")

        with get_db() as conn:
            # （日別/月別/曜日別/時間帯別のデータは下部で Python 集計するため SQL グループは不要）
            params = (start_date, end_date)

            # KPI の素データ（Pending は価格未確定のため除外、Unshipped は含める）
            rows = conn.execute(f"""
                SELECT oi.item_price, oi.quantity_ordered, oi.amazon_fee, oi.amazon_fee_confirmed,
                       oi.title, oi.shipping_price, oi.promotion_discount,
                       cp.cost_price, r.id AS return_id,
                       o.purchase_date, o.order_status
                FROM orders o
                JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                LEFT JOIN cost_prices cp ON cp.seller_sku = oi.seller_sku
                LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                WHERE substr(o.purchase_date, 1, 10) BETWEEN ? AND ?
                  AND o.order_status IN ('Shipped', 'Unshipped')
            """, params).fetchall()

            # 発送代行手数料: per_item は注文画面・在庫画面の表示用に「最新値」を1つ取得
            ship_row = conn.execute(
                "SELECT per_item_fee, base_fee FROM shipping_agent_fees "
                "ORDER BY effective_from DESC LIMIT 1"
            ).fetchone()
            ship_per_item = (ship_row["per_item_fee"] if ship_row else 0) or 0
            ship_base     = (ship_row["base_fee"] if ship_row else 0) or 0

            # 期間に含まれる year-month の expenses 合計
            # 月を計算
            ym_set = set()
            cur = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            while cur <= end:
                ym_set.add(cur.strftime("%Y-%m"))
                # 次の月へ
                y, m = cur.year, cur.month
                cur = cur.replace(year=(y+1 if m == 12 else y), month=(1 if m == 12 else m+1), day=1)

            # 月別の shipping_agent_fees（base_fee, per_item_fee）と
            # 月別の ASIN登録数（販売済み含む全SKU）を取得して
            # ship_total = Σ (base + per_item × ASIN登録数_in_month)
            # ※注: 注文画面の per-order 代行手数料は最新の per_item_fee 単一値を使用
            ship_total = 0
            for ym in sorted(ym_set):
                # その月有効の base_fee, per_item_fee（effective_from <= 月初）
                eff = ym + "-01"
                sa = conn.execute(
                    "SELECT base_fee, per_item_fee FROM shipping_agent_fees "
                    "WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
                    (eff,)
                ).fetchone()
                m_base = (sa["base_fee"] if sa else 0) or 0
                m_per  = (sa["per_item_fee"] if sa else 0) or 0
                # その月にASIN登録された全SKU数（Active+Inactive）
                ym_slash = ym.replace("-", "/")
                arow = conn.execute(
                    "SELECT COUNT(*) AS n FROM inventory "
                    "WHERE substr(asin_listed_at,1,7)=?",
                    (ym_slash,)
                ).fetchone()
                m_asin = (arow["n"] if arow else 0) or 0
                ship_total += m_base + m_per * m_asin

            other_exp = 0
            amz_fee_from_expense = 0
            non_op_income = 0  # 営業外収益（プラス計上）
            tax_cat_breakdown = {}  # 費目別合計（確定申告用）
            if ym_set:
                placeholders = ",".join(["?"] * len(ym_set))
                exp_rows = conn.execute(
                    f"SELECT category, tax_category, SUM(amount) AS total FROM expenses "
                    f"WHERE year_month IN ({placeholders}) GROUP BY category, tax_category",
                    tuple(ym_set),
                ).fetchall()
                for er in exp_rows:
                    amt = er["total"] or 0
                    if er["category"] == "Amazon利用料":
                        amz_fee_from_expense += amt
                    elif er["category"] == "プラス計上":
                        non_op_income += amt
                    else:
                        other_exp += amt
                    # 費目別集計（プラス計上は除外、自動Amazon利用料は支払手数料に含める）
                    tc = er["tax_category"]
                    if not tc and er["category"] == "Amazon利用料":
                        tc = "支払手数料"
                    # 注: 負の値も含める（割引調整など。プラス計上のみ別経路）
                    if tc and amt and er["category"] != "プラス計上":
                        tax_cat_breakdown[tc] = tax_cat_breakdown.get(tc, 0) + amt

            # 棚卸資産（自動）: Amazon Active 在庫のみ評価。
            # Amazon にまだ上場していない物理在庫は B/S の「Amazon未上場在庫」欄
            # （ユーザー手入力）で別途管理する。
            inv_value_row = conn.execute("""
                SELECT COALESCE(SUM(cp.cost_price * inv.quantity), 0) AS val
                FROM inventory inv
                LEFT JOIN cost_prices cp ON cp.seller_sku = inv.seller_sku
                WHERE inv.status LIKE 'Active%' AND inv.quantity > 0
                  AND cp.cost_price > 0
            """).fetchone()
            inventory_value = inv_value_row["val"] if inv_value_row else 0

            # 仕入れ台帳依存(sale_flag/sale_date)を使った推奨計算は廃止。
            # 代わりに「Web内の B/S データだけから算出される期末差額（使途不明金）」を
            # Amazon未上場在庫の推奨値として表示する（_build_bs 後に計算）。

            # 返金: returns テーブルの数量
            refund_rows = conn.execute(
                "SELECT COUNT(*) AS cnt FROM returns r "
                "WHERE substr(r.return_date, 1, 10) BETWEEN ? AND ?",
                params,
            ).fetchone()
            refund_cnt = refund_rows["cnt"] if refund_rows else 0

        # 返品計算モード（設定で切替）
        # exclude: 返品を集計から除外（再出品で在庫に戻る前提）
        # subtract_refund: プライスター方式（総計に含めた上で返金額を控除）
        return_model = get_setting("profit_return_model", "exclude")
        qty_total       = 0
        sales_total     = 0
        cost_total      = 0
        amz_fee_calc    = 0
        refund_amount   = 0
        refund_count    = 0
        shipping_income = 0
        promotion_total = 0
        # 時系列バケット（各キー: sales/qty/profit/cost/fee）
        by_day = {}
        by_month = {}
        by_dow = {}
        by_hour = {}

        def _bump(bucket, key, sales, qty, cost, fee, ship, promo):
            b = bucket.setdefault(key, {"sales": 0, "qty": 0, "cost": 0, "fee": 0, "ship": 0, "promo": 0})
            b["sales"] += sales; b["qty"] += qty; b["cost"] += cost
            b["fee"] += fee;   b["ship"]  += ship; b["promo"] += promo

        for r in rows:
            q = r["quantity_ordered"] or 0
            p = r["item_price"] or 0
            is_return = bool(r["return_id"])
            if is_return:
                refund_count += 1
                refund_amount += p * q
                if return_model == "exclude":
                    continue  # 売上・仕入・手数料に含めない
            qty_total += q
            sales_total += p * q
            row_sales = p * q
            row_cost = (r["cost_price"] or 0) * q
            row_ship = r["shipping_price"] or 0
            row_promo = r["promotion_discount"] or 0
            cost_total += row_cost
            shipping_income += row_ship
            promotion_total += row_promo
            # Amazon 手数料: 確定値があれば優先、無ければ estimate_amazon_fee_rate で詳細推定
            # （キット優先=本体扱い8% / 純レンズブランド=10% / その他レンズ=10% / 本体=8%）
            if r["amazon_fee_confirmed"] and r["amazon_fee"]:
                row_fee = r["amazon_fee"]
            else:
                title = (r["title"] or "")
                rate = estimate_amazon_fee_rate(title, None)
                row_fee = round(p * rate) * q
            amz_fee_calc += row_fee
            # グラフ用バケット（時系列別）
            pd_str = r["purchase_date"] or ""
            if pd_str:
                day_key = pd_str[:10]
                month_key = pd_str[:7]
                try:
                    dt = datetime.strptime(pd_str[:19], "%Y-%m-%dT%H:%M:%S")
                    dow_key = str(dt.weekday())  # 0=月 ... 6=日（ISO）
                    # SQLiteの strftime('%w') は 0=日曜 なので揃える
                    dow_key = str((dt.weekday() + 1) % 7)
                    hour_key = dt.strftime("%H")
                except Exception:
                    dow_key = hour_key = None
                _bump(by_day, day_key, row_sales, q, row_cost, row_fee, row_ship, row_promo)
                _bump(by_month, month_key, row_sales, q, row_cost, row_fee, row_ship, row_promo)
                if dow_key is not None:
                    _bump(by_dow, dow_key, row_sales, q, row_cost, row_fee, row_ship, row_promo)
                if hour_key is not None:
                    _bump(by_hour, hour_key, row_sales, q, row_cost, row_fee, row_ship, row_promo)

        # Amazon 手数料合計:
        #   amz_fee_calc        = 個別注文の Item Commission/FBA手数料（確定値 or レート推定）
        #   amz_fee_from_expense = 月次の Amazon利用料（サブスクリプション・FBA保管料・
        #                          返品手数料・取り出し手数料等、Finances API から自動集計）
        #   両者は別カテゴリの手数料なので合算する。
        amz_fee_total = amz_fee_calc + amz_fee_from_expense
        # 期間日数
        days_in_period = max(1, (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1)
        # 発送代行: ship_total は上で月別 base + per_item × ASIN登録数 で算出済み
        # その他経費（発送代行も含める）
        other_exp_total = other_exp + ship_total

        # 利益 = 売上+送料 − 仕入 − 手数料 − その他経費 − プロモ − 返金
        # exclude モードでは refund_amount=0 相当（ループで除外済み）
        # subtract_refund モードでは refund_amount が差し引かれる（プライスター方式）
        refund_deduction = refund_amount if return_model == "subtract_refund" else 0
        profit = (sales_total + shipping_income
                  - cost_total - amz_fee_total - other_exp_total
                  - promotion_total - refund_deduction)
        kpi = {
            "qty_total": qty_total,
            "sales_total": sales_total,
            "shipping_income": shipping_income,
            "inventory_value": inventory_value,
            "cost_total": cost_total,
            "amazon_fee_total": amz_fee_total,
            "other_expense": other_exp_total,
            "profit": profit,
            "avg_qty_per_day": round(qty_total / days_in_period, 2),
            "avg_price_per_unit": round(sales_total / qty_total, 2) if qty_total else 0,
            "avg_cost_per_unit": round(cost_total / qty_total, 2) if qty_total else 0,
            "refund_amount": refund_amount,
            "refund_count": refund_count,
            "promotion_total": promotion_total,
            "profit_rate": round(profit / sales_total * 100, 2) if sales_total else 0,
        }

        # P/L 用に order_items 由来の Amazon手数料 と 発送代行手数料 を費目別集計に追加
        # （tax_cat_breakdown には expenses テーブル分しか入っていないため、
        #   このまま _build_pl に渡すと販管費が大幅に欠落する）
        if amz_fee_calc > 0:
            tax_cat_breakdown["支払手数料"] = (
                tax_cat_breakdown.get("支払手数料", 0) + amz_fee_calc
            )
        if ship_total > 0:
            tax_cat_breakdown["荷造運賃"] = (
                tax_cat_breakdown.get("荷造運賃", 0) + ship_total
            )

        # --- グラフ用データ生成（売上・販売数・利益・累計売上）---
        # 行別の経費・発送代行を日次配分するための単位
        # 「その他経費」は月按分で表示月の日別に均等割り、発送代行(base) は月按分、per_item は実績数量ベース
        def _profit_of(b, exp_per_day=0):
            """バケットから利益を算出。exp_per_day=この期間単位に按分した固定費"""
            p = (b["sales"] + b["ship"]
                 - b["cost"] - b["fee"] - b["promo"] - exp_per_day)
            # per_item 発送代行
            p -= ship_per_item * b["qty"]
            return p

        # 期間の固定費（発送代行 base_fee × 月数 + other_exp）を日数で均等割り
        fixed_total_for_period = ship_base * len(ym_set) + other_exp
        per_day_exp = fixed_total_for_period / days_in_period if days_in_period else 0

        # 日別（全日付 0 埋め＋累計）
        daily = []
        cum_s = 0; cum_p = 0
        cur = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        while cur <= end_dt:
            key = cur.strftime("%Y-%m-%d")
            b = by_day.get(key, {"sales":0,"qty":0,"cost":0,"fee":0,"ship":0,"promo":0})
            prof = _profit_of(b, per_day_exp)
            cum_s += b["sales"]
            cum_p += prof
            daily.append({
                "k": f"{cur.day}日",
                "sales": b["sales"], "qty": b["qty"],
                "profit": round(prof), "cum": cum_s, "cum_profit": round(cum_p),
            })
            cur += timedelta(days=1)

        # 月別
        monthly = []
        for k in sorted(by_month.keys()):
            b = by_month[k]
            # 月別は base_fee をその月分だけ＆other_exp は月按分、qty × per_item
            per_month_exp = (ship_base + (other_exp / max(1, len(ym_set))))
            prof = _profit_of(b, per_month_exp)
            monthly.append({"k": k, "sales": b["sales"], "qty": b["qty"], "profit": round(prof)})

        # 曜日別（0=日～6=土）: 固定費は按分しない（集計目的）
        dow_labels = ["日", "月", "火", "水", "木", "金", "土"]
        dow_mapped = []
        for i in range(7):
            b = by_dow.get(str(i), {"sales":0,"qty":0,"cost":0,"fee":0,"ship":0,"promo":0})
            prof = _profit_of(b, 0)  # 曜日別は固定費按分しない
            dow_mapped.append({"k": dow_labels[i], "sales": b["sales"], "qty": b["qty"], "profit": round(prof)})

        # 時間帯別（00〜23）
        hour = []
        for h in range(24):
            hk = f"{h:02d}"
            b = by_hour.get(hk, {"sales":0,"qty":0,"cost":0,"fee":0,"ship":0,"promo":0})
            prof = _profit_of(b, 0)
            hour.append({"k": f"{h}時", "sales": b["sales"], "qty": b["qty"], "profit": round(prof)})

        # パスで描画するテンプレートを切替（同一データを2画面に分けて表示）
        _tpl = "accounting.html" if request.path == "/accounting" else "analytics.html"

        # ===== 売上分析の追加グラフ（円グラフ2つ） =====
        sold_buckets = {"~30日": 0, "30-60日": 0, "60-90日": 0, "90-180日": 0, "180日超": 0}
        rank_buckets = {"S": 0, "A": 0, "B": 0, "C": 0, "?": 0}
        if request.path != "/accounting":
            try:
                with get_db() as _c:
                    # 1. 販売スピード分布：ASIN登録日 → 販売日 の経過日数で集計
                    # 右上の期間フィルタ（start_date 〜 end_date）で絞り込む
                    # （Pending除外、return_model='exclude'なら返品も除外）
                    _sql_sold = """
                        SELECT o.purchase_date, oi.quantity_ordered AS q,
                               r.id AS return_id, inv.asin_listed_at
                        FROM orders o
                        JOIN order_items oi ON oi.amazon_order_id = o.amazon_order_id
                        LEFT JOIN inventory inv ON inv.seller_sku = oi.seller_sku
                        LEFT JOIN returns r ON r.amazon_order_id = o.amazon_order_id AND r.seller_sku = oi.seller_sku
                        WHERE o.order_status IN ('Shipped', 'Unshipped')
                          AND substr(o.purchase_date, 1, 10) BETWEEN ? AND ?
                    """
                    import re as _re
                    def _parse_listed_at(s):
                        if not s:
                            return None
                        m = _re.match(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", str(s))
                        if not m:
                            return None
                        try:
                            return datetime(int(m[1]), int(m[2]), int(m[3])).date()
                        except ValueError:
                            return None
                    for _r in _c.execute(_sql_sold, (start_date, end_date)):
                        if return_model == "exclude" and _r["return_id"]:
                            continue
                        listed = _parse_listed_at(_r["asin_listed_at"])
                        if not listed:
                            continue
                        try:
                            purchased = datetime.fromisoformat(_r["purchase_date"][:10]).date()
                        except (TypeError, ValueError):
                            continue
                        d = (purchased - listed).days
                        if d < 0:
                            continue  # データ異常（販売日 < 登録日）はスキップ
                        q = _r["q"] or 0
                        if d <= 30:
                            sold_buckets["~30日"] += q
                        elif d <= 60:
                            sold_buckets["30-60日"] += q
                        elif d <= 90:
                            sold_buckets["60-90日"] += q
                        elif d <= 180:
                            sold_buckets["90-180日"] += q
                        else:
                            sold_buckets["180日超"] += q

                    # 2. 現在の Active 在庫の売れ行きランク分布
                    _sql_inv = """
                        SELECT inv.keepa_sales_90d, inv.offers_json
                        FROM inventory inv
                        WHERE inv.status LIKE 'Active%' AND inv.quantity > 0
                    """
                    import json as _j
                    for _r in _c.execute(_sql_inv):
                        s = _r["keepa_sales_90d"]
                        try:
                            n = len(_j.loads(_r["offers_json"] or "[]"))
                        except Exception:
                            n = 0
                        if not s or not n:
                            rank_buckets["?"] += 1
                            continue
                        p = (s / 90) / n
                        p30 = 1 - (1 - p) ** 30
                        p60 = 1 - (1 - p) ** 60
                        p90 = 1 - (1 - p) ** 90
                        if p30 >= 0.7:
                            rank_buckets["S"] += 1
                        elif p60 >= 0.7:
                            rank_buckets["A"] += 1
                        elif p90 >= 0.7:
                            rank_buckets["B"] += 1
                        else:
                            rank_buckets["C"] += 1
            except Exception as _e:
                app.logger.warning(f"analytics extra charts: {_e}")

        # B/S オブジェクト + 使途不明金（期末差額）から推奨値を計算
        bs_obj = _build_bs(
            int(request.args.get("bs_year", now.year)),
            int(request.args.get("bs_month", now.month)),
            inventory_value,
        )
        # 期末の現在の Amazon未上場在庫の値を取得
        _cur_unlisted = 0
        for _sec in bs_obj["end"]["sections"]:
            for _it in _sec["entries"]:
                if _it["category"] == "Amazon未上場在庫":
                    _cur_unlisted = _it["amount"] or 0
                    break
        # 推奨値 = 現在値 - 期末差額（差額0なら現在値のまま）
        unlisted_balance_hint = _cur_unlisted - bs_obj["end"]["totals"]["balance_diff"]
        # 決算ページ: 月別比較表のデータを準備（B/S年と同じ年）
        monthly_summaries = []
        if request.path == "/accounting":
            _bs_year = int(request.args.get("bs_year", now.year))
            _last_month = now.month if _bs_year == now.year else 12
            for _m in range(1, _last_month + 1):
                monthly_summaries.append(_compute_monthly_summary(_bs_year, _m))
        return render_template(
            _tpl,
            preset=preset,
            start_date=start_date,
            end_date=end_date,
            kpi=kpi,
            daily=daily,
            monthly=monthly,
            dow=dow_mapped,
            hour=hour,
            return_model=return_model,
            tab=tab,
            tax_breakdown=sorted(tax_cat_breakdown.items(), key=lambda x: -x[1]),
            pl=_build_pl(kpi, tax_cat_breakdown, non_op_income),
            bs=bs_obj,
            unlisted_balance_hint=unlisted_balance_hint,
            unlisted_diff=bs_obj["end"]["totals"]["balance_diff"],
            bs_years=list(range(now.year - 4, now.year + 1)),
            bs_months=list(range(1, 13)),
            monthly_summaries=monthly_summaries,
            sold_buckets=sold_buckets,
            rank_buckets=rank_buckets,
        )

    # ==========================================================
    # 価格自動調整設定
    # ==========================================================
    @app.route("/price-rules", methods=["GET", "POST"])
    @login_required
    def price_rules():
        if request.method == "POST":
            sku = request.form.get("sku")
            mode = request.form.get("mode", "none")
            high = request.form.get("high_stopper") or None
            low = request.form.get("low_stopper") or None
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO price_rules(seller_sku, mode, high_stopper, low_stopper, active, updated_at)
                    VALUES(?, ?, ?, ?, 1, ?)
                    ON CONFLICT(seller_sku) DO UPDATE SET
                      mode=excluded.mode, high_stopper=excluded.high_stopper,
                      low_stopper=excluded.low_stopper, updated_at=excluded.updated_at
                """, (sku, mode, high, low, datetime.utcnow().isoformat()))
            flash(f"{sku} の価格ルールを保存しました", "success")
            return redirect(url_for("price_rules"))

        with get_db() as conn:
            rules = conn.execute("""
                SELECT pr.*, inv.asin, inv.title, inv.listing_price, cp.cost_price
                FROM price_rules pr
                LEFT JOIN inventory inv ON inv.seller_sku = pr.seller_sku
                LEFT JOIN cost_prices cp ON cp.seller_sku = pr.seller_sku
                ORDER BY pr.updated_at DESC
            """).fetchall()
        return render_template("price_rules.html", rules=rules)

    # ==========================================================
    # 設定
    # ==========================================================
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        """設定画面は再認証必須。session['settings_verified_until'] が現在時刻より
        未来の場合のみ表示。過ぎたら password 再入力を求める。"""
        from auth import change_password, change_username, verify_password
        from flask import session
        import time
        # 再認証入口（POST form_type=verify）
        if request.method == "POST" and request.form.get("form_type") == "verify":
            pwd = request.form.get("verify_password", "")
            if verify_password(current_user.username, pwd):
                # 15 分有効
                session["settings_verified_until"] = int(time.time()) + 15 * 60
                flash("認証しました", "success")
                return redirect(url_for("settings"))
            flash("パスワードが一致しません", "danger")
            return render_template("settings_verify.html")
        verified_until = session.get("settings_verified_until", 0)
        if verified_until < int(time.time()):
            return render_template("settings_verify.html")

        if request.method == "POST":
            form_type = request.form.get("form_type")
            if form_type == "shop":
                name = (request.form.get("shop_name") or "").strip()
                if name:
                    set_setting("shop_name", name)
                    flash("ショップ名を保存しました", "success")
                else:
                    flash("ショップ名を入力してください", "danger")
            elif form_type == "spapi":
                # SP-API 認証情報（settings テーブルに保存）
                for k in ("sp_api_refresh_token", "sp_api_lwa_app_id",
                          "sp_api_lwa_client_secret", "sp_api_seller_id"):
                    v = request.form.get(k, "").strip()
                    if v and not v.startswith("********"):  # マスク状態なら保存しない
                        set_setting(k, v)
                flash("SP-API 認証情報を保存しました", "success")
            elif form_type == "sheets":
                for k in ("sheet_url", "sheet_sku_col", "sheet_cost_col"):
                    v = request.form.get(k, "").strip()
                    set_setting(k, v)
                # サービスアカウントJSON のアップロード処理
                creds_file = request.files.get("google_creds_file")
                if creds_file and creds_file.filename:
                    try:
                        # JSONバリデーション
                        import json as _json
                        content = creds_file.read()
                        parsed = _json.loads(content)
                        if parsed.get("type") != "service_account":
                            flash("アップロードされたファイルがサービスアカウントJSONではありません", "danger")
                        else:
                            # 保存先（DB配下）
                            from config import BASE_DIR
                            creds_dir = BASE_DIR / "secrets"
                            creds_dir.mkdir(exist_ok=True)
                            creds_path = creds_dir / "google_creds.json"
                            with open(creds_path, "wb") as f:
                                f.write(content)
                            set_setting("google_creds_path", str(creds_path))
                            # 環境変数にも即反映（再起動不要）
                            os.environ["GOOGLE_CREDS_PATH"] = str(creds_path)
                            flash(f"✓ サービスアカウントJSON を保存しました（{parsed.get('client_email','')}）", "success")
                    except Exception as e:
                        flash(f"JSONアップロードエラー: {e}", "danger")
                flash("スプレッドシート設定を保存しました", "success")
            elif form_type == "keepa":
                v = request.form.get("keepa_api_key", "").strip()
                if v and not v.startswith("********"):
                    set_setting("keepa_api_key", v)
                # 即時同期トリガ
                if request.form.get("sync_now") == "1":
                    try:
                        from polling import sync_keepa_sales
                        n = sync_keepa_sales(stale_hours=0)
                        flash(f"Keepa 同期完了: {n} ASIN を更新しました", "success")
                    except Exception as e:
                        flash(f"Keepa 同期エラー: {e}", "danger")
                else:
                    flash("Keepa API key を保存しました", "success")
            elif form_type == "password":
                cur = request.form.get("current_password", "")
                new_ = request.form.get("new_password", "")
                confirm = request.form.get("confirm_password", "")
                if new_ != confirm:
                    flash("新しいパスワードと確認が一致しません", "danger")
                else:
                    ok, msg = change_password(current_user.username, cur, new_)
                    flash(msg, "success" if ok else "danger")
            elif form_type == "username":
                cur = request.form.get("current_password_u", "")
                new_u = request.form.get("new_username", "")
                ok, msg = change_username(current_user.username, cur, new_u)
                flash(msg, "success" if ok else "danger")
            elif form_type == "profit_logic":
                rm = request.form.get("return_model", "exclude")
                if rm not in ("exclude", "subtract_refund"):
                    rm = "exclude"
                set_setting("profit_return_model", rm)
                flash("利益計算ロジックを保存しました", "success")
            elif form_type == "price_diverge":
                try:
                    thr = max(0, int(request.form.get("price_diverge_threshold", "1000")))
                except ValueError:
                    thr = 1000
                set_setting("price_diverge_threshold", str(thr))
                flash(f"価格乖離しきい値を ¥{thr:,} で保存しました", "success")
            elif form_type == "inline_apply":
                m = request.form.get("inline_price_apply_mode", "manual")
                if m not in ("auto", "manual"):
                    m = "manual"
                set_setting("inline_price_apply_mode", m)
                label = "編集と同時に Amazon へ即時反映" if m == "auto" else "「価格更新」ボタンでまとめて反映"
                flash(f"価格反映方式を「{label}」に変更しました", "success")
            elif form_type == "price_engine":
                apply = "1" if request.form.get("auto_price_apply") == "on" else "0"
                set_setting("auto_price_apply", apply)
                # 追従挙動の保存
                strategy = request.form.get("match_strategy", "match")
                if strategy not in ("match", "yen", "pct"):
                    strategy = "match"
                set_setting("match_strategy", strategy)
                # オフセット値（match の時は使われないが値は保持）
                try:
                    yen = max(0, int(request.form.get("match_offset_yen") or 0))
                except (TypeError, ValueError):
                    yen = 0
                try:
                    pct = max(0.0, float(request.form.get("match_offset_pct") or 0))
                except (TypeError, ValueError):
                    pct = 0.0
                set_setting("match_offset_yen", str(yen))
                set_setting("match_offset_pct", str(pct))
                flash(
                    "価格自動調整を " + ("有効化" if apply == "1" else "無効化")
                    + f" / 追従: {strategy}"
                    + (f" -{yen}円" if strategy == 'yen' else f" -{pct}%" if strategy == 'pct' else ""),
                    "success",
                )
            return redirect(url_for("settings"))

        def mask(v):
            if not v:
                return ""
            return "********" + v[-4:] if len(v) > 4 else "****"

        ctx = {
            "spapi": {
                "sp_api_refresh_token": mask(get_setting("sp_api_refresh_token", "")),
                "sp_api_lwa_app_id":    mask(get_setting("sp_api_lwa_app_id", "")),
                "sp_api_lwa_client_secret": mask(get_setting("sp_api_lwa_client_secret", "")),
                "sp_api_seller_id":     get_setting("sp_api_seller_id", "") or "",
            },
            "sheet_url":     get_setting("sheet_url", "") or "",
            "sheet_sku_col": get_setting("sheet_sku_col", "A") or "A",
            "sheet_cost_col": get_setting("sheet_cost_col", "K") or "K",
            "google_creds_path_status": (
                get_setting("google_creds_path", "") or os.getenv("GOOGLE_CREDS_PATH", "")
            ).split("/")[-1].split("\\")[-1] if (get_setting("google_creds_path", "") or os.getenv("GOOGLE_CREDS_PATH", "")) else "",
            "keepa_api_key": mask(get_setting("keepa_api_key", "")),
            "keepa_updated_at": get_setting("keepa_last_sync", ""),
            "auto_price_apply": get_setting("auto_price_apply", "0") == "1",
            "match_strategy": get_setting("match_strategy", "match"),
            "match_offset_yen": int(get_setting("match_offset_yen", "0") or 0),
            "match_offset_pct": float(get_setting("match_offset_pct", "0") or 0),
            "profit_return_model": get_setting("profit_return_model", "exclude"),
            "price_diverge_threshold": get_setting("price_diverge_threshold", "1000"),
            "inline_price_apply_mode": get_setting("inline_price_apply_mode", "manual"),
            "current_username": current_user.username,
        }
        return render_template("settings.html", **ctx)

    # ==========================================================
    # Polling 手動実行
    # ==========================================================
    @app.route("/polling/run", methods=["POST"])
    @login_required
    def polling_run():
        """価格更新ボタン:
          - 競合最低価格を再取得（read）
          - 手動モードで保留になっている価格変更を Amazon に PATCH（write）
        """
        msgs = []
        # 1) 保留中の手動価格変更を Amazon に反映
        try:
            with get_db() as conn:
                pending = conn.execute("""
                    SELECT id, seller_sku, new_price FROM price_change_log
                    WHERE reason='manual_pending' AND success=0
                    ORDER BY id ASC
                """).fetchall()
            if pending:
                from price_engine import patch_amazon_price
                pushed, failed = 0, 0
                for p in pending:
                    ok, err = patch_amazon_price(p["seller_sku"], p["new_price"])
                    with get_db() as conn:
                        if ok:
                            conn.execute(
                                "UPDATE price_change_log SET success=1, error_message=NULL "
                                "WHERE id=?", (p["id"],)
                            )
                            pushed += 1
                        else:
                            conn.execute(
                                "UPDATE price_change_log SET error_message=? WHERE id=?",
                                ((err or "")[:300], p["id"]),
                            )
                            failed += 1
                msg = f"✓ Amazon 反映 {pushed}件" + (f" / 失敗 {failed}件" if failed else "")
                msgs.append(msg)
        except Exception as e:
            msgs.append(f"反映エラー: {e}")

        # 2) 軽量更新（競合最低価格）
        try:
            r = run_light_refresh()
            if "error" in r:
                msgs.append(f"価格更新エラー: {r['error']}")
            else:
                msgs.append(f"最低価格更新 {r.get('competitive', 0)}件")
        except Exception as e:
            msgs.append(f"価格更新エラー: {e}")

        flash(" / ".join(msgs), "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/polling/full", methods=["POST"])
    @login_required
    def polling_full():
        """全体同期（Orders/Inventory/Returns/Finances も含む、時間かかる）"""
        try:
            r = run_all_polling(days=60)
            flash(f"全体同期完了: {r['details']}", "success")
        except Exception as e:
            flash(f"全体同期エラー: {e}", "danger")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/cost-prices/sync", methods=["POST"])
    @login_required
    def sync_cost_prices_now():
        """仕入れ台帳を読み直して cost_prices テーブルを再同期する。
        在庫一覧の警告バナーから呼ばれる軽量同期エンドポイント。"""
        try:
            from polling import sync_cost_prices
            n = sync_cost_prices()
            flash(f"✓ 仕入値を再同期しました（{n} 件処理）", "success")
        except Exception as e:
            flash(f"仕入値同期エラー: {e}", "danger")
        return redirect(request.referrer or url_for("inventory"))

    @app.route("/balance-sheet/save", methods=["POST"])
    @login_required
    def save_balance_sheet():
        """B/S 期首・期末 2列分を保存。"""
        ym_start = (request.form.get("ym_start") or "").strip()
        ym_end   = (request.form.get("ym_end") or "").strip()
        if not ym_start or not ym_end:
            flash("年月が指定されていません", "danger")
            return redirect(request.referrer or url_for("analytics"))

        def _save_col(suffix: str, ym: str, conn) -> None:
            for side, subgroup, items in BS_DEFINITION:
                for cat, kind in items:
                    raw = (request.form.get(f"bs_{suffix}_amt_{cat}") or "").replace(",", "").strip()
                    if raw == "":
                        continue  # 未入力はスキップ（既存値維持 / auto は計算値）
                    try:
                        amt = float(raw)
                    except ValueError:
                        continue
                    note = (request.form.get(f"bs_{suffix}_note_{cat}") or "").strip()
                    conn.execute("""
                        INSERT INTO balance_sheet(year_month, side, subgroup, category, amount, note)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(year_month, category) DO UPDATE SET
                            side=excluded.side, subgroup=excluded.subgroup,
                            amount=excluded.amount, note=excluded.note
                    """, (ym, side, subgroup, cat, amt, note))

        with get_db() as conn:
            _save_col("start", ym_start, conn)
            _save_col("end",   ym_end,   conn)
        flash(f"✓ {ym_start} と {ym_end} の貸借対照表を保存しました", "success")
        return redirect(request.referrer or url_for("analytics"))

    @app.route("/balance-sheet/fit-capital", methods=["POST"])
    @login_required
    def bs_fit_capital():
        """元入金フィット: 「資産合計 − 負債合計 − その他純資産項目」を元入金に設定して
        貸借をバランスさせる（差額調整）。期首/期末どちらか一方の列に対して実行。"""
        ym = (request.form.get("ym") or "").strip()
        column_label = (request.form.get("label") or "").strip()  # 表示用
        if not ym:
            flash("年月が指定されていません", "danger")
            return redirect(request.referrer or url_for("analytics"))

        # その列の現在の値を取得（自動値の計算も再現する必要あり）
        # 簡単のため、_load_bs_column と同じロジックで値を組み立て、
        # その時点での auto_inventory / auto_profit を求める。
        try:
            yr = int(ym.split("-")[0]); mo = int(ym.split("-")[1])
        except Exception:
            flash("年月フォーマットが不正", "danger"); return redirect(request.referrer or url_for("analytics"))

        # auto values を計算
        if mo == 1:
            # 期首: 棚卸資産 = 前年末保存値、利益 = 0
            with get_db() as conn:
                prev = conn.execute(
                    "SELECT amount FROM balance_sheet WHERE year_month=? AND category='棚卸資産'",
                    (f"{yr-1:04d}-12",),
                ).fetchone()
            auto_inv = (prev["amount"] if prev else 0) or 0
            auto_profit = 0
        else:
            # その他月: 棚卸資産 = 現在の在庫評価、利益 = 累計
            with get_db() as conn:
                inv_row = conn.execute("""
                    SELECT COALESCE(SUM(cp.cost_price * inv.quantity), 0) AS val
                    FROM inventory inv
                    LEFT JOIN cost_prices cp ON cp.seller_sku = inv.seller_sku
                    WHERE inv.status LIKE 'Active%' AND inv.quantity > 0
                      AND cp.cost_price > 0
                """).fetchone()
            auto_inv = inv_row["val"] if inv_row else 0
            auto_profit = _calc_cumulative_profit(yr, mo)

        col = _load_bs_column(ym, auto_inv, auto_profit)
        # 元入金以外の純資産合計
        equity_excl_capital = 0
        for sec in col["sections"]:
            if sec["side"] != "equity":
                continue
            for item in sec["entries"]:
                if item["category"] != "元入金":
                    # 事業主貸はマイナス計上（資産項目的扱いを純資産でマイナス）
                    if item["category"] == "事業主貸":
                        equity_excl_capital -= item["amount"]
                    else:
                        equity_excl_capital += item["amount"]
        # 事業主貸の処理: 通常は純資産から控除なので上の処理で既に -=

        capital = col["totals"]["asset"] - col["totals"]["liability"] - equity_excl_capital

        # balance_sheet テーブルに元入金として upsert
        with get_db() as conn:
            conn.execute("""
                INSERT INTO balance_sheet(year_month, side, subgroup, category, amount, note)
                VALUES(?, 'equity', '純資産', '元入金', ?, ?)
                ON CONFLICT(year_month, category) DO UPDATE SET
                    side='equity', subgroup='純資産',
                    amount=excluded.amount, note=excluded.note
            """, (ym, capital, "自動フィット（参考値）"))
        flash(
            f"✓ {column_label or ym} の元入金を ¥{int(capital):,} にセット → 貸借差額 0 に調整",
            "success",
        )
        return redirect(request.referrer or url_for("analytics"))

    @app.route("/balance-sheet/copy-from-prev", methods=["POST"])
    @login_required
    def bs_copy_from_prev():
        """期首列に「前年12月末」のデータをコピー。"""
        try:
            year = int(request.form.get("year"))
        except (TypeError, ValueError):
            flash("年が指定されていません", "danger")
            return redirect(request.referrer or url_for("analytics"))
        prev_ym = f"{year-1:04d}-12"
        cur_ym  = f"{year:04d}-01"
        with get_db() as conn:
            rows = conn.execute(
                "SELECT side, subgroup, category, amount, note FROM balance_sheet WHERE year_month=?",
                (prev_ym,),
            ).fetchall()
            if not rows:
                flash(f"{prev_ym} のデータがありません", "warning")
                return redirect(request.referrer or url_for("analytics"))
            for r in rows:
                conn.execute("""
                    INSERT INTO balance_sheet(year_month, side, subgroup, category, amount, note)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(year_month, category) DO UPDATE SET
                        side=excluded.side, subgroup=excluded.subgroup,
                        amount=excluded.amount, note=excluded.note
                """, (cur_ym, r["side"], r["subgroup"], r["category"], r["amount"], r["note"]))
        flash(f"✓ {prev_ym} の B/S を {cur_ym}（期首）にコピーしました", "success")
        return redirect(request.referrer or url_for("analytics"))

    # ==========================================================
    # 価格調整エンジン 手動実行（DRY RUN）
    # ==========================================================
    @app.route("/price-engine/run", methods=["POST"])
    @login_required
    def price_engine_run():
        from price_engine import run_engine
        dry_run = request.form.get("apply") != "1"
        try:
            r = run_engine(dry_run=dry_run, apply_updates=not dry_run)
            mode_str = "DRY RUN" if dry_run else "本番実行"
            flash(
                f"[{mode_str}] 評価{r['evaluated']}件 / 変更候補{r['would_change']}件 / "
                f"変更済{r['changed']}件 / エラー{r['errors']}件",
                "info" if dry_run else "success",
            )
        except Exception as e:
            flash(f"価格調整エンジン エラー: {e}", "danger")
        return redirect(url_for("price_rules"))

    # ==========================================================
    # 価格変更ログ
    # ==========================================================
    @app.route("/price-engine/log")
    @login_required
    def price_engine_log():
        with get_db() as conn:
            logs = conn.execute("""
                SELECT * FROM price_change_log ORDER BY executed_at DESC LIMIT 100
            """).fetchall()
        return render_template("price_log.html", logs=logs)

    return app


if __name__ == "__main__":
    app = create_app()
    _start_scheduler(app)
    print(f"🌐 http://localhost:{config.PORT}  で起動")
    print("    初回アクセスでログイン画面が出ます（admin / 起動ログのパスワード）")
    print("    Polling は起動時 + 10分間隔（固定）/ Price Engine は 15分間隔")
    # debug=False にしないと APScheduler が reloader で二重起動する
    app.run(host=config.HOST, port=config.PORT, debug=False)
