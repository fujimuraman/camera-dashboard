"""DBの設定値を環境変数に流し込む。
配布版で「設定画面に入力した値」を SP-API クライアント等から透過的に使えるようにする。

使い方:
    from env_bootstrap import bootstrap_env_from_db
    bootstrap_env_from_db()  # 起動時に1回呼ぶ

優先順位:
    1. 既存の os.environ 値（=.env 由来等）
    2. DB の settings テーブル値
    → 既存環境（.env 設定済み）は壊さず、新規環境ではDB値が使われる
"""
import os
import re
from pathlib import Path


def _extract_sheet_id(url_or_id: str) -> str:
    """URL から SPREADSHEET_ID を抽出。素のIDが渡されたらそのまま返す。"""
    if not url_or_id:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    # URL じゃなければ ID と判断
    return url_or_id.strip()


def bootstrap_env_from_db():
    """DB の settings テーブルから値を読んで os.environ にセット（既存値は保護）。

    対応キー:
      - SP-API 認証: sp_api_refresh_token / sp_api_lwa_app_id / sp_api_lwa_client_secret / sp_api_seller_id
        → {SHOP_KEY}_REFRESH_TOKEN 等の形式に展開（amazon-seller-automation/sp_api_client が読む形式）
      - 仕入れ台帳: sheet_url（URL or ID） → SPREADSHEET_ID
      - GOOGLE_CREDS_PATH: 既に .env で設定済みならそれを優先、未設定ならDBから（後で実装）
    """
    try:
        from db import get_db
    except Exception:
        return  # DB アクセス失敗時は何もしない（起動自体は継続）

    shop = os.getenv("SHOP_KEY", "fuji").upper()

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('sp_api_refresh_token','sp_api_lwa_app_id','sp_api_lwa_client_secret','sp_api_seller_id','sheet_url','google_creds_path')"
            ).fetchall()
            db_settings = {r["key"]: (r["value"] or "") for r in rows}
    except Exception:
        return

    # SP-API 認証情報を環境変数に展開
    mapping = {
        "sp_api_refresh_token":     f"{shop}_REFRESH_TOKEN",
        "sp_api_lwa_app_id":        f"{shop}_LWA_CLIENT_ID",
        "sp_api_lwa_client_secret": f"{shop}_LWA_CLIENT_SECRET",
        "sp_api_seller_id":         f"{shop}_SELLER_ID",
    }
    for db_key, env_key in mapping.items():
        if db_settings.get(db_key) and not os.getenv(env_key):
            os.environ[env_key] = db_settings[db_key]

    # SPREADSHEET_ID（sheet_url または既存の SPREADSHEET_ID）
    if not os.getenv("SPREADSHEET_ID"):
        sheet_id = _extract_sheet_id(db_settings.get("sheet_url", ""))
        if sheet_id:
            os.environ["SPREADSHEET_ID"] = sheet_id

    # GOOGLE_CREDS_PATH（DB > env のフォールバック順、ただし env 既設は優先）
    if not os.getenv("GOOGLE_CREDS_PATH") and db_settings.get("google_creds_path"):
        os.environ["GOOGLE_CREDS_PATH"] = db_settings["google_creds_path"]
