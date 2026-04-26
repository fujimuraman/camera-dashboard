"""
SP-API 接続テスト

両店舗（フジカメラ / You and Me）の認証情報で SP-API に接続できるかを確認する。
Sellers API の get_marketplace_participation() を呼び、各Marketplaceに
正常にアクセスできることを検証する。

実行:
    python setup/test_connection.py
"""

import io
import sys
from pathlib import Path

# Windows cp932 環境でも絵文字が出力できるよう UTF-8 化
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# プロジェクトルートを sys.path に追加（setup/ から scripts/ を import するため）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from sp_api.api import Sellers  # noqa: E402
from sp_api.base.exceptions import SellingApiException  # noqa: E402

from scripts.common.sp_api_client import get_client, get_shop_name, get_seller_id  # noqa: E402


SHOPS = ["fuji", "yandme"]


def test_shop(shop: str) -> bool:
    """
    指定店舗の接続テストを実施。

    Returns:
        True: 成功 / False: 失敗
    """
    shop_name = get_shop_name(shop)
    print(f"\n[{shop_name}] 接続テスト開始...")

    # Step 1: 認証情報の読み込み & Seller ID 確認
    try:
        seller_id = get_seller_id(shop)
        print(f"[{shop_name}] Seller ID: {seller_id}")
    except ValueError as e:
        print(f"[{shop_name}] ❌ 認証情報の読み込みに失敗: {e}")
        return False

    # Step 2: SP-API クライアント初期化
    try:
        client = get_client(shop, Sellers)
        print(f"[{shop_name}] クライアント初期化OK")
    except Exception as e:
        print(f"[{shop_name}] ❌ クライアント初期化に失敗: {e}")
        return False

    # Step 3: Marketplace 参加情報の取得
    try:
        response = client.get_marketplace_participation()
    except SellingApiException as e:
        print(f"[{shop_name}] ❌ API呼び出しに失敗: {e}")
        return False
    except Exception as e:
        print(f"[{shop_name}] ❌ 予期しないエラー: {type(e).__name__}: {e}")
        return False

    # Step 4: レスポンス解析
    try:
        payload = response.payload
        if not payload:
            print(f"[{shop_name}] ⚠️ レスポンスのpayloadが空です")
            return False

        marketplaces = []
        for item in payload:
            mp = item.get("marketplace", {})
            mp_name = mp.get("name", "不明")
            mp_id = mp.get("id", "不明")
            marketplaces.append(f"{mp_name}(ID={mp_id})")

        mp_list = ", ".join(marketplaces) if marketplaces else "なし"
        print(f"[{shop_name}] 接続OK - Marketplace: {mp_list}")
        return True
    except Exception as e:
        print(f"[{shop_name}] ❌ レスポンス解析に失敗: {e}")
        return False


def main() -> int:
    print("=" * 60)
    print("SP-API 接続テスト")
    print("=" * 60)

    results = {shop: test_shop(shop) for shop in SHOPS}

    print("\n" + "=" * 60)
    print("結果サマリー")
    print("=" * 60)
    for shop, ok in results.items():
        shop_name = get_shop_name(shop)
        status = "✅ 成功" if ok else "❌ 失敗"
        print(f"  {shop_name}: {status}")

    if all(results.values()):
        print("\n🎉 すべての接続テストに成功しました。")
        return 0
    else:
        print("\n⚠️ 一部の接続テストに失敗しました。ログを確認してください。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
