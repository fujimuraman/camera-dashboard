"""
SP-API 共通クライアント

店舗識別子('fuji' / 'yandme')を渡すと、対応する認証情報で
SP-APIクライアントを生成して返す。

使い方:
    from scripts.common.sp_api_client import get_client
    from sp_api.api import Orders, Sellers

    client = get_client('fuji', Sellers)
    resp = client.get_marketplace_participation()
    print(resp.payload)
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from sp_api.base import Marketplaces
from sp_api.base.exceptions import SellingApiException


# プロジェクトルートの .env を明示的に読み込む
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


# 店舗識別子 → 環境変数プレフィックスのマッピング
_SHOP_PREFIX = {
    "fuji": "FUJI",
    "yandme": "YANDME",
}


def _load_credentials(shop: str) -> dict:
    """
    指定された店舗の認証情報を .env から読み込む。

    Args:
        shop: 店舗識別子 ('fuji' or 'yandme')

    Returns:
        python-amazon-sp-api の Credentials 形式の dict

    Raises:
        ValueError: 不明な shop、または必要な環境変数が不足している場合
    """
    if shop not in _SHOP_PREFIX:
        raise ValueError(
            f"Unknown shop: {shop!r}. Expected one of {list(_SHOP_PREFIX)}"
        )

    prefix = _SHOP_PREFIX[shop]
    required_keys = [
        f"{prefix}_LWA_CLIENT_ID",
        f"{prefix}_LWA_CLIENT_SECRET",
        f"{prefix}_REFRESH_TOKEN",
    ]

    missing = [k for k in required_keys if not os.getenv(k)]
    if missing:
        raise ValueError(
            f"Missing environment variables for {shop!r}: {missing}. "
            f"Check your .env file at {_PROJECT_ROOT / '.env'}"
        )

    return {
        "refresh_token": os.getenv(f"{prefix}_REFRESH_TOKEN"),
        "lwa_app_id": os.getenv(f"{prefix}_LWA_CLIENT_ID"),
        "lwa_client_secret": os.getenv(f"{prefix}_LWA_CLIENT_SECRET"),
    }


def get_client(shop: str, api_class):
    """
    指定された店舗・APIクラスに対応する SP-API クライアントを返す。

    Args:
        shop: 'fuji' または 'yandme'
        api_class: sp_api.api の API クラス (例: Sellers, Orders, Inventories)

    Returns:
        初期化された API クライアントインスタンス

    Raises:
        ValueError: 認証情報の読み込みに失敗した場合
        SellingApiException: 認証エラーが発生した場合
    """
    credentials = _load_credentials(shop)
    try:
        return api_class(credentials=credentials, marketplace=Marketplaces.JP)
    except SellingApiException as e:
        raise SellingApiException(
            f"Failed to initialize {api_class.__name__} client for shop={shop!r}: {e}"
        ) from e


def get_shop_name(shop: str) -> str:
    """店舗識別子から表示用の日本語名を返す。"""
    return {
        "fuji": "フジカメラ",
        "yandme": "You and Me",
    }.get(shop, shop)


def get_seller_id(shop: str) -> str:
    """指定店舗の Seller ID を .env から取得。"""
    prefix = _SHOP_PREFIX.get(shop)
    if not prefix:
        raise ValueError(f"Unknown shop: {shop!r}")
    seller_id = os.getenv(f"{prefix}_SELLER_ID")
    if not seller_id:
        raise ValueError(f"{prefix}_SELLER_ID is not set in .env")
    return seller_id
