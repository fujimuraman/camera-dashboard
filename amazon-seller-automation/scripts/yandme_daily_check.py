"""
You and Me 日次チェックスクリプト

SP-API経由で新規注文を取得し、
1. スプレッドシートに転記（21列構造、ヘッダー2行目、データ3行目〜）
2. 発送期限3日前以内の未発送注文をアラートとして出力

実行:
    python scripts/yandme_daily_check.py --days 7           # 過去7日の新規注文を検索（デフォルト）
    python scripts/yandme_daily_check.py --dry-run          # 転記せず結果だけ表示
    python scripts/yandme_daily_check.py --commit           # スプレッドシートに実際に書き込む

デフォルトは dry-run。明示的に --commit を指定した場合のみスプレッドシートへ書き込む。
"""

import argparse
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cp932 環境でも絵文字が出力できるよう UTF-8 化
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sp_api.api import Orders  # noqa: E402
from sp_api.api.catalog_items.catalog_items_2022_04_01 import (  # noqa: E402
    CatalogItemsV20220401,
)
from sp_api.base import Marketplaces  # noqa: E402
from sp_api.base.exceptions import SellingApiException  # noqa: E402

from scripts.common.sp_api_client import get_client, get_shop_name  # noqa: E402


SHOP = "yandme"
JST = timezone(timedelta(hours=9))

# You and Me スプレッドシート設定
SPREADSHEET_ID = os.getenv(
    "YANDME_SPREADSHEET_ID", "REDACTED_SHEET_ID"
)
SHEET_NAME = "シート1"  # ワークシート名（スプレッドシート名「輸入台帳」のデフォルトシート）
HEADER_ROW = 2
DATA_START_ROW = 3  # ヘッダー直下

# 店舗名の固定値（備考欄との区別のため改行入り）
SHOP_NAME_LABEL = "輸入雑貨店\nYou and Me"

# 商品画像URLの直URL生成パターン（SP-API Catalog取得失敗時のフォールバック）
# media.amazon.com の一般的なサムネイルURL形式
def _fallback_image_url(asin: str) -> str:
    # サムネイル用の小さな画像
    return f"https://images-fe.ssl-images-amazon.com/images/P/{asin}.09._SCLZZZZZZZ_.jpg"

# スプレッドシートのカラム構成（1列目は空白、Noは2列目=B列）
# 0-indexed リストとして返す形
# A B C D E F G H I J K L M N O P Q R S T U V
# (空) No 注文日 ショップ名 ショップNo 郵便番号 住所1 住所2 住所3 名前1 名前2 電話番号
#      注文番号 ASIN 商品 画像 画像URL 追跡番号 パッケージID 数量 書類添付 備考欄


def fetch_orders(days: int):
    """過去days日分の注文を取得。"""
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
        orders = payload.get("Orders", [])
        all_orders.extend(orders)
        next_token = payload.get("NextToken")
        if not next_token:
            break

    return all_orders


def fetch_order_detail(order_id: str):
    """注文の商品アイテムと配送先を取得。"""
    client_items = get_client(SHOP, Orders)
    client_addr = get_client(SHOP, Orders)

    items_resp = client_items.get_order_items(order_id)
    items = (items_resp.payload or {}).get("OrderItems", [])

    # 配送先住所（PIIアクセス可能なロールが必要）
    address = {}
    try:
        addr_resp = client_addr.get_order_address(order_id)
        address = (addr_resp.payload or {}).get("ShippingAddress", {})
    except SellingApiException as e:
        print(f"  ⚠️ ShippingAddress取得失敗 (OrderId={order_id}): {e}")

    return items, address


def fetch_image_url(asin: str) -> str:
    """ASINから商品のサムネイル画像URLを取得。

    SP-API Catalog Items で画像情報を取得し、サムネイル（小さい画像）を返す。
    失敗したら Amazon 画像サーバの直URLパターンにフォールバック。
    """
    if not asin:
        return ""
    try:
        catalog = get_client(SHOP, CatalogItemsV20220401)
        resp = catalog.get_catalog_item(
            asin=asin,
            marketplaceIds=Marketplaces.JP.marketplace_id,
            includedData="images",
        )
        payload = resp.payload or {}
        images_groups = payload.get("images", [])
        # 最初のMarketplaceの最初の画像を取る
        for group in images_groups:
            imgs = group.get("images", [])
            if imgs:
                # variant=SMALL, MAIN, THUMB などあり。小さいものを優先
                for variant_pref in ("SMALL", "THUMB", "MAIN"):
                    for img in imgs:
                        if img.get("variant") == variant_pref:
                            return img.get("link", "")
                # 見つからなければ最初を返す
                return imgs[0].get("link", "")
    except SellingApiException as e:
        print(f"  ⚠️ Catalog画像取得失敗 (ASIN={asin}): {e}")
    except Exception as e:
        print(f"  ⚠️ Catalog画像取得エラー (ASIN={asin}): {e}")

    # フォールバック
    return _fallback_image_url(asin)


def format_date_mmdd(iso_str: str) -> str:
    """ISO形式日時を mm/dd に変換。"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_jst = dt.astimezone(JST)
        return f"{dt_jst.month}/{dt_jst.day}"
    except Exception:
        return iso_str


def build_spreadsheet_row(order: dict, items: list, address: dict) -> list:
    """1注文→スプレッドシート1行（A〜V, 22セル）に変換。

    - 複数商品の注文は代表1件として1行にまとめる（最初のアイテム）
    - 空白＝空文字列
    - G列に住所全体（都道府県+市区町村+住所1+住所2）を連結、H列は空白
    """
    purchase_date = format_date_mmdd(order.get("PurchaseDate", ""))
    order_id = order.get("AmazonOrderId", "")

    # 商品情報（代表1件）
    asin = items[0].get("ASIN", "") if items else ""
    title = items[0].get("Title", "") if items else ""
    image_url = fetch_image_url(asin) if asin else ""

    # 配送先
    postal = address.get("PostalCode", "") or ""
    state = address.get("StateOrRegion", "") or ""
    city = address.get("City", "") or ""
    addr1 = address.get("AddressLine1", "") or ""
    addr2 = address.get("AddressLine2", "") or ""
    # G列：住所全体を連結
    full_address = "".join([state, city, addr1, addr2])
    name1 = address.get("Name", "") or ""
    phone = address.get("Phone", "") or ""

    # 備考欄（発送期限 mm/dd）
    latest_ship = order.get("LatestShipDate", "")
    deadline_mmdd = format_date_mmdd(latest_ship)
    note = (
        f"配達期限:{deadline_mmdd}\n"
        f"MyUSから楽ロジに配送中\n"
        f"出荷通知(mm/dd)"
    )

    return [
        "",               # A (空)
        "",               # B No
        purchase_date,    # C 注文日
        SHOP_NAME_LABEL,  # D ショップ名
        "",               # E ショップNo
        postal,           # F 郵便番号
        full_address,     # G 住所1（住所全体連結）
        "",               # H 住所2（空白）
        "",               # I 住所3（空白）
        name1,            # J 名前1
        "",               # K 名前2（空白）
        phone,            # L 電話番号
        order_id,         # M 注文番号
        asin,             # N ASIN
        title,            # O 商品
        "",               # P 画像
        image_url,        # Q 画像URL（Catalogから取得）
        "",               # R 追跡番号
        "",               # S パッケージID
        "",               # T 数量
        "",               # U 書類添付
        note,             # V 備考欄
    ]


def build_ship_deadline_notifications(orders: list) -> list:
    """全ての未発送注文について、発送期限までの残り日数を報告する。

    Returns:
        [{order_id, purchase_date, latest_ship_date, days_left, status, asin, title}, ...]
        （期限が近い順）
    """
    notifications = []
    now_jst = datetime.now(JST)

    for order in orders:
        status = order.get("OrderStatus", "")
        if status not in ("Unshipped", "PartiallyShipped"):
            continue
        latest_ship_str = order.get("LatestShipDate", "")
        if not latest_ship_str:
            continue
        try:
            latest_ship_dt = datetime.fromisoformat(
                latest_ship_str.replace("Z", "+00:00")
            ).astimezone(JST)
        except Exception:
            continue

        # 残り日数（時間差）を日数単位で
        delta = latest_ship_dt - now_jst
        # 切り捨てずに端数含めて判断
        days_left = delta.days if delta.total_seconds() >= 0 else -((-delta).days + (1 if (-delta).seconds else 0))

        notifications.append({
            "order_id": order.get("AmazonOrderId", ""),
            "purchase_date": order.get("PurchaseDate", ""),
            "latest_ship_date": latest_ship_str,
            "days_left": days_left,
            "status": status,
        })

    notifications.sort(key=lambda x: x["latest_ship_date"])
    return notifications


def format_days_left(days_left: int) -> str:
    """残り日数を人間可読な形式に整形。"""
    if days_left < 0:
        return f"⚠️ 期限超過（{abs(days_left)}日経過）"
    if days_left == 0:
        return "🚨 今日中"
    if days_left <= 3:
        return f"🚨 あと{days_left}日"
    return f"あと{days_left}日"


def get_existing_order_ids_from_sheet() -> set:
    """スプレッドシートから既存の注文番号を取得（重複追加防止）。"""
    try:
        import gspread
        from google.oauth2 import service_account

        key_path = _PROJECT_ROOT / "secrets" / "gcp-sa.json"
        creds = service_account.Credentials.from_service_account_file(
            str(key_path),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        # 最初のシート（固定シート名が無ければ）
        try:
            ws = sh.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.sheet1
        # M列（注文番号）のデータ3行目以降を取得
        col_values = ws.col_values(13)  # M列 = 13
        # 3行目以降
        return set(v for v in col_values[2:] if v)
    except Exception as e:
        print(f"⚠️ 既存注文番号の取得に失敗: {e}")
        return set()


def append_rows_to_sheet(rows: list) -> bool:
    """スプレッドシートの3行目以降（既存データの次）に行追加。"""
    try:
        import gspread
        from google.oauth2 import service_account

        key_path = _PROJECT_ROOT / "secrets" / "gcp-sa.json"
        creds = service_account.Credentials.from_service_account_file(
            str(key_path),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.sheet1

        # 現在のデータ最終行を調べる
        col_values = ws.col_values(13)  # M列（注文番号）
        next_row = max(len(col_values) + 1, DATA_START_ROW)

        # A〜V列の範囲で一括append
        range_name = f"A{next_row}"
        ws.update(range_name=range_name, values=rows, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"❌ スプレッドシートへの書き込みに失敗: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="You and Me 日次チェック")
    parser.add_argument("--days", type=int, default=7, help="新規注文検出の検索範囲（過去N日）")
    parser.add_argument("--unshipped-days", type=int, default=30,
                        help="未発送注文の発送期限チェック範囲（過去N日、デフォルト30）")
    parser.add_argument("--commit", action="store_true", help="スプレッドシートに実際に書き込む")
    parser.add_argument("--dry-run", action="store_true", help="書き込まず結果だけ表示（デフォルト）")
    args = parser.parse_args()
    dry_run = not args.commit

    print("=" * 60)
    print(f"You and Me 日次チェック {'(DRY RUN)' if dry_run else '(COMMIT)'}")
    print("=" * 60)

    # 1. 注文取得（新規検出用の狭い範囲）
    orders = fetch_orders(args.days)
    print(f"[{get_shop_name(SHOP)}] 取得件数 (新規検出 {args.days}日): {len(orders)}")

    # 1b. 未発送注文の期限チェック用に広い範囲も取得（古い未発送を見逃さない）
    if args.unshipped_days > args.days:
        orders_for_shipping = fetch_orders(args.unshipped_days)
        print(
            f"[{get_shop_name(SHOP)}] 取得件数 (発送期限チェック {args.unshipped_days}日): "
            f"{len(orders_for_shipping)}"
        )
    else:
        orders_for_shipping = orders

    if not orders and not orders_for_shipping:
        print("新規注文なし")
        return 0

    # 2. 発送期限通知（全未発送注文のあと何日で発送か、広い範囲から検出）
    notifications = build_ship_deadline_notifications(orders_for_shipping)
    print("\n" + "-" * 60)
    print(f"📅 発送期限通知（未発送注文 {len(notifications)}件）")
    print("-" * 60)
    if not notifications:
        print("  未発送注文はありません")
    for n in notifications:
        print(
            f"  OrderId={n['order_id']} | "
            f"購入日={format_date_mmdd(n['purchase_date'])} | "
            f"期限={format_date_mmdd(n['latest_ship_date'])} "
            f"[{format_days_left(n['days_left'])}] | "
            f"状態={n['status']}"
        )

    # 3. スプレッドシート転記準備
    existing_ids = get_existing_order_ids_from_sheet()
    print(f"\n既存のスプレッドシート記載済み注文番号: {len(existing_ids)}件")

    # 転記対象のステータス（Canceled や Pending は除外）
    TRANSCRIBE_STATUSES = {"Unshipped", "PartiallyShipped", "Shipped"}

    new_rows = []
    new_order_summary = []
    skipped_by_status = 0
    for order in orders:
        order_id = order.get("AmazonOrderId", "")
        if order_id in existing_ids:
            continue  # 既に記載済み
        status = order.get("OrderStatus", "")
        if status not in TRANSCRIBE_STATUSES:
            skipped_by_status += 1
            continue
        items, address = fetch_order_detail(order_id)
        row = build_spreadsheet_row(order, items, address)
        new_rows.append(row)
        new_order_summary.append({
            "order_id": order_id,
            "purchase_date": order.get("PurchaseDate", ""),
            "items_count": len(items),
            "status": status,
        })

    if skipped_by_status:
        print(f"\n（Canceled/Pending等のためスキップ: {skipped_by_status}件）")

    print(f"\n" + "-" * 60)
    print(f"📝 スプレッドシート転記対象: {len(new_rows)}件")
    print("-" * 60)
    for s in new_order_summary:
        print(
            f"  OrderId={s['order_id']} | "
            f"購入日={format_date_mmdd(s['purchase_date'])} | "
            f"状態={s['status']} | "
            f"アイテム数={s['items_count']}"
        )

    if dry_run:
        print("\n[DRY RUN] スプレッドシートへの書き込みはスキップ")
        print("実際に書き込むには --commit を指定してください")
        return 0

    if not new_rows:
        print("\n新規に転記する行はありません")
        return 0

    # 4. 書き込み
    print(f"\nスプレッドシートに{len(new_rows)}行を追加中...")
    success = append_rows_to_sheet(new_rows)
    if success:
        print(f"✅ {len(new_rows)}行の転記完了")
        return 0
    else:
        print("❌ 転記失敗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
