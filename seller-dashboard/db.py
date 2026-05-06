"""SQLite DB 接続 & スキーマ管理"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH


SCHEMA = """
-- 注文（SP-API Orders から取得）
CREATE TABLE IF NOT EXISTS orders (
  amazon_order_id TEXT PRIMARY KEY,
  purchase_date TEXT,
  order_status TEXT,
  fulfillment_channel TEXT,
  marketplace_id TEXT,
  item_price_total REAL,
  shipping_price REAL,
  updated_at TEXT
);

-- 注文商品
CREATE TABLE IF NOT EXISTS order_items (
  order_item_id TEXT PRIMARY KEY,
  amazon_order_id TEXT,
  asin TEXT,
  seller_sku TEXT,
  title TEXT,
  quantity_ordered INTEGER,
  item_price REAL,
  amazon_fee REAL,
  amazon_fee_confirmed INTEGER DEFAULT 0,
  condition TEXT,
  shipped_quantity INTEGER
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(amazon_order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(seller_sku);

-- 在庫（SP-API Listings から取得）
CREATE TABLE IF NOT EXISTS inventory (
  seller_sku TEXT PRIMARY KEY,
  asin TEXT,
  title TEXT,
  product_condition TEXT,
  fulfillment_channel TEXT,
  quantity INTEGER,
  listing_price REAL,
  shipping_price REAL,
  min_price_fba REAL,
  min_price_all REAL,
  cart_price REAL,
  featured_offer_won INTEGER DEFAULT 0,
  main_image_url TEXT,
  status TEXT,
  updated_at TEXT,
  asin_listed_at TEXT,
  price_updated_at TEXT
);

-- 返品
CREATE TABLE IF NOT EXISTS returns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  return_date TEXT,
  amazon_order_id TEXT,
  seller_sku TEXT,
  asin TEXT,
  fnsku TEXT,
  quantity INTEGER,
  reason TEXT,
  detailed_disposition TEXT,
  fulfillment_center_id TEXT,
  customer_comments TEXT,
  UNIQUE(amazon_order_id, seller_sku, return_date)
);

-- 仕入れ価格（仕入れ台帳から同期）
CREATE TABLE IF NOT EXISTS cost_prices (
  seller_sku TEXT PRIMARY KEY,
  asin TEXT,
  cost_price REAL,
  supplier TEXT,
  purchase_date TEXT,
  ledger_row INTEGER,
  updated_at TEXT
);

-- 価格自動調整設定（SKU 別）
CREATE TABLE IF NOT EXISTS price_rules (
  seller_sku TEXT PRIMARY KEY,
  mode TEXT,              -- fba_condition / all_condition / fba_min / all_min / cart / none
  high_stopper REAL,
  low_stopper REAL,
  active INTEGER DEFAULT 1,
  updated_at TEXT
);

-- 経費（月ごと）
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  year_month TEXT,
  category TEXT,
  amount REAL,
  auto_calculated INTEGER DEFAULT 0,
  repeat_monthly INTEGER DEFAULT 0,
  note TEXT,
  UNIQUE(year_month, category)
);

-- 発送代行手数料設定（履歴管理）
CREATE TABLE IF NOT EXISTS shipping_agent_fees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_from TEXT UNIQUE,
  base_fee REAL,
  per_item_fee REAL,
  note TEXT
);

-- Financial Events（手数料内訳）
CREATE TABLE IF NOT EXISTS financial_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  amazon_order_id TEXT,
  event_type TEXT,
  posted_date TEXT,
  fee_type TEXT,
  amount REAL,
  currency TEXT
);
-- 注: 旧 raw_json カラムは2026-05に廃止（92MB肥大化、未使用のため）。
-- 既存DBには ALTER TABLE DROP COLUMN しないとカラム自体は残るが、INSERT/SELECT 共に未参照のため実害なし。
CREATE INDEX IF NOT EXISTS idx_fin_order ON financial_events(amazon_order_id);

-- 価格変更ログ
CREATE TABLE IF NOT EXISTS price_change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seller_sku TEXT,
  old_price REAL,
  new_price REAL,
  reason TEXT,
  executed_at TEXT,
  success INTEGER DEFAULT 0,
  error_message TEXT
);

-- 設定
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- Polling 実行ログ
CREATE TABLE IF NOT EXISTS polling_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  target TEXT,        -- orders / listings / returns / all
  success INTEGER DEFAULT 0,
  message TEXT
);

-- 貸借対照表 (B/S) のスナップショット（期末ごとに保存）
CREATE TABLE IF NOT EXISTS balance_sheet (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  year_month TEXT,        -- スナップショット時点 YYYY-MM（期末）
  side TEXT,              -- 'asset' / 'liability' / 'equity'
  subgroup TEXT,          -- 流動資産/固定資産/流動負債/固定負債/純資産
  category TEXT,          -- 表示名（買掛金 等）
  amount REAL,
  note TEXT,
  UNIQUE(year_month, category)
);

-- ユーザー（認証）
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE,
  password_hash TEXT,
  created_at TEXT
);

-- カメラ市況分析: 市場全体（カメラ＋レンズ）の BSR 履歴メタ情報
-- ASIN ごと最新 BSR 履歴を保持。bsr_history_json は最大2年分の日次 [{date, rank}, ...]
CREATE TABLE IF NOT EXISTS market_bsr_meta (
  asin TEXT PRIMARY KEY,
  category TEXT,
  rank_in_category INTEGER,
  title TEXT,
  current_price INTEGER,
  bsr_current INTEGER,
  bsr_history_json TEXT,
  bsr_updated_at TEXT,
  fetch_attempts INTEGER DEFAULT 0,
  demand_rank TEXT,
  source TEXT
);

-- 市場活況スコアのキャッシュ（ym='YYYY-MM'、score=10〜90、median_bsr=その月の中央値）
CREATE TABLE IF NOT EXISTS market_score_cache (
  ym TEXT PRIMARY KEY,
  score REAL,
  median_bsr INTEGER,
  raw_score REAL,
  asin_count INTEGER,
  updated_at TEXT
);

-- BSRスコアの日次キャッシュ（売上分析の日別グラフ高速化用）
-- source = 'inventory' または 'market'。同じ date でも source 違いで2行入る
-- 在庫＋市況の raw を共通スケーリングするため、scaled_score はバッチ計算時に統一min/max適用済
-- has_inventory_count = その date にデータあるASINのうち自社在庫保有数（market集計時の付加情報）
CREATE TABLE IF NOT EXISTS bsr_score_daily_cache (
  date TEXT NOT NULL,
  source TEXT NOT NULL,
  asin_count INTEGER,
  has_inventory_count INTEGER,
  median_bsr INTEGER,
  raw_score REAL,
  scaled_score REAL,
  updated_at TEXT,
  PRIMARY KEY (date, source)
);
CREATE INDEX IF NOT EXISTS idx_bsr_score_daily_date ON bsr_score_daily_cache(date);

-- 在庫数の日次スナップショット（売上分析の在庫数推移を「実測」で表示するため）
-- snapshot_date = "YYYY-MM-DD"、count_active = Active && qty>0 の SKU 数、value = 仕入価格 × 数量の合計
CREATE TABLE IF NOT EXISTS inventory_snapshots (
  snapshot_date TEXT PRIMARY KEY,
  count_active INTEGER NOT NULL,
  inventory_value INTEGER,
  recorded_at TEXT
);
"""


def init_db():
    """DB 初期化（テーブル作成 + 既存テーブルへの列追加マイグレーション）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    # ALTER TABLE での列追加（IF NOT EXISTS 代替）
    for alter in [
        "ALTER TABLE inventory ADD COLUMN asin_listed_at TEXT",
        "ALTER TABLE inventory ADD COLUMN price_updated_at TEXT",
        "ALTER TABLE inventory ADD COLUMN offers_json TEXT",
        "ALTER TABLE inventory ADD COLUMN offers_updated_at TEXT",
        "ALTER TABLE inventory ADD COLUMN product_type TEXT",
        "ALTER TABLE inventory ADD COLUMN keepa_sales_30d INTEGER",
        "ALTER TABLE inventory ADD COLUMN keepa_sales_90d INTEGER",
        "ALTER TABLE inventory ADD COLUMN keepa_sales_180d INTEGER",
        "ALTER TABLE inventory ADD COLUMN keepa_updated_at TEXT",
        "ALTER TABLE order_items ADD COLUMN shipping_price REAL DEFAULT 0",
        "ALTER TABLE order_items ADD COLUMN promotion_discount REAL DEFAULT 0",
        "ALTER TABLE shipping_agent_fees ADD COLUMN repeat_monthly INTEGER DEFAULT 0",
        "ALTER TABLE shipping_agent_fees ADD COLUMN repeat_base INTEGER DEFAULT 0",
        "ALTER TABLE shipping_agent_fees ADD COLUMN repeat_per_item INTEGER DEFAULT 0",
        "ALTER TABLE expenses ADD COLUMN tax_category TEXT",
        "ALTER TABLE cost_prices ADD COLUMN sale_date TEXT",
        "ALTER TABLE cost_prices ADD COLUMN sale_flag TEXT",
        # BSR（市場活況度の元データ）
        "ALTER TABLE inventory ADD COLUMN bsr_current INTEGER",
        "ALTER TABLE inventory ADD COLUMN bsr_history_json TEXT",  # 90日履歴（[(date, bsr), ...]）
        "ALTER TABLE inventory ADD COLUMN bsr_updated_at TEXT",
        # market_bsr_meta 拡張（自社仕入れ対象連動版）
        "ALTER TABLE market_bsr_meta ADD COLUMN fetch_attempts INTEGER DEFAULT 0",
        "ALTER TABLE market_bsr_meta ADD COLUMN demand_rank TEXT",
        "ALTER TABLE market_bsr_meta ADD COLUMN source TEXT",
    ]:
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass  # 既に存在
    conn.commit()
    conn.close()


@contextmanager
def get_db():
    """接続コンテキスト（自動コミット/クローズ）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


if __name__ == "__main__":
    init_db()
    print(f"✔ DB initialized: {DB_PATH}")
