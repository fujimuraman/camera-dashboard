"""fuji-dashboard 設定"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# .env ファイル自動読み込み（プロジェクト直下の .env を優先）
try:
    from dotenv import load_dotenv
    _env_file = BASE_DIR / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass  # dotenv 未インストールでも動作（環境変数は OS から読む）
# DB ファイルパス（環境変数で上書き可。テスト用に別DBを使うのに便利）
DB_PATH = Path(os.getenv("FUJI_DASH_DB", str(BASE_DIR / "data.db")))
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# Flask
SECRET_KEY = os.getenv("FUJI_DASH_SECRET", "change-me-in-production-" + os.urandom(8).hex())
HOST = "0.0.0.0"  # 外部アクセス可（Cloudflare Tunnel 経由）
PORT = int(os.getenv("FUJI_DASH_PORT", "8080"))

# 認証（Flask-Login 用パスワード）
# 初期ユーザー: admin / 初回起動時に設定されるパスワード（FUJI_DASH_PASSWORD env か、DB 保存）
ADMIN_USERNAME = os.getenv("FUJI_DASH_USER", "admin")
# パスワードは .env か settings テーブルから取得

# Polling 設定（デフォルト、settings テーブルで上書き可）
DEFAULT_POLLING_INTERVAL_MIN = 15

# SP-API 認証情報（amazon-seller-automation の .env を使用）
# 配布時はユーザーが SP_API_ENV_PATH を環境変数で指定する想定
SP_API_ENV_PATH = Path(os.getenv("SP_API_ENV_PATH",
                                  str(BASE_DIR.parent / "amazon-seller-automation" / ".env")))

# 仕入れ台帳（Google Sheets）
# 配布時に必須: 環境変数 SPREADSHEET_ID で各セラーが自分のシートIDを指定
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDS_PATH = Path(os.getenv("GOOGLE_CREDS_PATH",
                                    str(BASE_DIR.parent / "credentials.json")))

# Amazon Marketplace（A1VC38T7YXB528 = Amazon.co.jp 共通値、上書き可）
MARKETPLACE_ID = os.getenv("MARKETPLACE_ID", "A1VC38T7YXB528")

# Shop 識別子（amazon-seller-automation の .env キー名と連動）
# 例: SHOP_KEY=fuji なら FUJI_REFRESH_TOKEN, FUJI_LWA_APP_ID 等を参照
SHOP_KEY = os.getenv("SHOP_KEY", "fuji")
