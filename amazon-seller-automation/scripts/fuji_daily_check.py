"""
フジカメラ 日次チェックスクリプト

SP-API経由で新規注文を取得し、概要を表示する。
（FBA中心のため自己発送の発送期限アラートは不要。将来的に返品レポート取得を拡張予定）

実行:
    python scripts/fuji_daily_check.py --days 7
"""

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cp932 環境でも絵文字が出力できるよう UTF-8 化
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import csv

from sp_api.api import Orders, Reports  # noqa: E402
from sp_api.base.exceptions import SellingApiException  # noqa: E402

from scripts.common.sp_api_client import get_client, get_shop_name  # noqa: E402


SHOP = "fuji"
JST = timezone(timedelta(hours=9))
RETURNS_REPORT_TYPE = "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA"


def fetch_orders(days: int):
    client = get_client(SHOP, Orders)
    created_after = (
        (datetime.now(JST) - timedelta(days=days))
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    print(f"[{get_shop_name(SHOP)}] 過去{days}日間の注文を取得中 (CreatedAfter={created_after})...")

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
    return all_orders


def format_date_mmdd(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return f"{dt.astimezone(JST).month}/{dt.astimezone(JST).day}"
    except Exception:
        return iso_str


def main() -> int:
    parser = argparse.ArgumentParser(description="フジカメラ 日次チェック")
    parser.add_argument("--days", type=int, default=7, help="過去N日の注文を検索")
    args = parser.parse_args()

    print("=" * 60)
    print("フジカメラ 日次チェック")
    print("=" * 60)

    orders = fetch_orders(args.days)
    print(f"[{get_shop_name(SHOP)}] 取得件数: {len(orders)}")

    if not orders:
        print("新規注文なし")
        return 0

    # ステータス別集計
    status_count = {}
    for o in orders:
        s = o.get("OrderStatus", "Unknown")
        status_count[s] = status_count.get(s, 0) + 1

    print("\n" + "-" * 60)
    print("ステータス別集計")
    print("-" * 60)
    for s, c in sorted(status_count.items()):
        print(f"  {s}: {c}件")

    # 個別一覧
    print("\n" + "-" * 60)
    print("個別注文一覧")
    print("-" * 60)
    for o in orders:
        channel = o.get("FulfillmentChannel", "")  # AFN=FBA, MFN=自己発送
        print(
            f"  OrderId={o.get('AmazonOrderId', '')} | "
            f"購入日={format_date_mmdd(o.get('PurchaseDate', ''))} | "
            f"状態={o.get('OrderStatus', '')} | "
            f"ch={channel}"
        )

    # 返品レポート確認
    print("\n" + "-" * 60)
    print("返品レポート確認")
    print("-" * 60)
    check_returns_report(days=args.days)

    return 0


def check_returns_report(days: int) -> None:
    """FBA返品レポートを取得して表示。

    既に完了済みの直近レポートがあれば解析して返品内容を表示する。
    なければ「該当期間の完了済みレポートなし」と表示。
    新規レポート作成は時間がかかるため、ここでは実施しない。
    """
    try:
        reports = get_client(SHOP, Reports)
    except Exception as e:
        print(f"  ⚠️ Reports API初期化失敗: {e}")
        return

    try:
        # 直近N日の完了済み返品レポートを検索
        created_since = (
            (datetime.now(JST) - timedelta(days=days))
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
    except SellingApiException as e:
        print(f"  ⚠️ レポート一覧取得失敗: {e}")
        return

    if not report_list:
        print(f"  直近{days}日に完了済みの返品レポートはありません")
        print(f"  （新規作成したい場合は別途 create_report を実行）")
        return

    # 最新のレポートを1件解析
    latest = sorted(
        report_list, key=lambda r: r.get("createdTime", ""), reverse=True
    )[0]
    report_id = latest.get("reportId", "")
    document_id = latest.get("reportDocumentId", "")
    created_time = latest.get("createdTime", "")
    print(f"  最新レポート: reportId={report_id}, createdTime={created_time}")

    if not document_id:
        print("  ⚠️ reportDocumentId が無いためスキップ")
        return

    try:
        doc_resp = reports.get_report_document(document_id)
        doc = doc_resp.payload or {}
        url = doc.get("url", "")
        if not url:
            print("  ⚠️ ダウンロードURL取得失敗")
            return
    except SellingApiException as e:
        print(f"  ⚠️ レポートドキュメント取得失敗: {e}")
        return

    # TSVをダウンロードして解析
    # JPマーケットプレイスのレポートは Windows-31J (cp932) エンコーディング
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:
            raw_bytes = r.read()
            content_type = r.headers.get("content-type", "")
        # content-type から charset を自動判定（fallback: cp932）
        charset = "cp932"
        if "charset=" in content_type.lower():
            charset = content_type.split("charset=")[-1].strip().lower()
            if charset == "windows-31j":
                charset = "cp932"
        try:
            raw = raw_bytes.decode(charset, errors="replace")
        except LookupError:
            raw = raw_bytes.decode("cp932", errors="replace")
    except Exception as e:
        print(f"  ⚠️ レポートダウンロード失敗: {e}")
        return

    lines = raw.splitlines()
    if not lines:
        print("  返品レポートは空です")
        return

    reader = csv.DictReader(lines, delimiter="\t")
    returns = list(reader)
    print(f"  返品件数: {len(returns)}件")
    if not returns:
        return

    # 表形式で出力（返品日・注文番号・SKU・ASIN・FNSKU・商品・返品理由・購入者コメント）
    headers = ["返品日", "注文番号", "SKU", "ASIN", "FNSKU", "商品", "返品理由", "購入者コメント"]
    print()
    print("  | " + " | ".join(headers) + " |")
    print("  |" + "|".join(["---"] * len(headers)) + "|")
    for r in returns:
        date = (r.get("return-date", "") or "").split("T")[0]
        title = (r.get("product-name", "") or "").replace("|", "/").replace("\n", " ")
        comment = (r.get("customer-comments", "") or "(なし)").replace("|", "/").replace("\n", " ")
        row = [
            date,
            r.get("order-id", ""),
            r.get("sku", ""),
            r.get("asin", ""),
            r.get("fnsku", ""),
            title,
            _jp_reason(r.get("reason", "")),
            comment,
        ]
        print("  | " + " | ".join(row) + " |")


# 返品理由の日本語マッピング
_REASON_JP = {
    "DEFECTIVE": "不良品",
    "CUSTOMER_DAMAGED": "顧客破損",
    "DAMAGED_BY_CARRIER": "配送中破損",
    "SWITCHEROO": "すり替え",
    "UNWANTED_ITEM": "不要になった",
    "NOT_AS_DESCRIBED": "説明と異なる",
    "UNAUTHORIZED_PURCHASE": "不正購入",
    "OVERSHIPPED": "過剰配送",
    "WRONG_ITEM": "異なる商品",
    "QUALITY_UNACCEPTABLE": "品質不満",
    "EXPIRED_ITEM": "期限切れ",
    "MISSED_ESTIMATED_DELIVERY": "配達予定超過",
    "MISSING_PARTS": "部品欠損",
    "DID_NOT_LIKE_ITEM": "好みでない",
    "APPAREL_STYLE": "スタイル（アパレル）",
    "APPAREL_TOO_LARGE": "サイズが大きい",
    "APPAREL_TOO_SMALL": "サイズが小さい",
    "NO_REASON_GIVEN": "理由不明",
    "ORDERED_WRONG_ITEM": "誤注文",
}


def _jp_reason(code: str) -> str:
    code = (code or "").strip()
    return _REASON_JP.get(code, code)


if __name__ == "__main__":
    sys.exit(main())
