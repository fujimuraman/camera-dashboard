# フジカメラ ダッシュボード 設計書

**プロジェクト名**: fuji-dashboard  
**目的**: プライスター代替の自社運用ダッシュボード  
**対象**: フジカメラ（Amazon FBA）のみ。You and Me は対象外。  
**作成日**: 2026-04-23

---

## 1. プロジェクト概要

プライスターと同等の機能を、SP-API を自前で叩いて構築する Web ダッシュボード。
主な目的：
- 毎月のプライスター月額課金（約2000円〜）を削減
- 仕入れ台帳（Google Sheets）との直結による真の利益計算
- 自分好みの UI/機能カスタマイズ
- 発送代行手数料（プライスターに無い独自機能）の管理

## 2. 機能要件（画面構成）

### 2.1 ダッシュボード（トップ画面）
- 今日・昨日・今月の販売数 / 売上 / 利益
- 要対応タスク（発送期限、返品、受取評価）
- 売れた商品 最新10件（リアルタイム表示）

**省略する項目**（ユーザー指示）:
- おすすめサービス、注目機能、Twitter サポート、お知らせ

### 2.2 注文一覧
- 期間フィルタ: 今月 / 先月 / 過去7日・14日・1ヶ月・3ヶ月・6ヶ月・12ヶ月 / 期間指定
- ステータス絞り込み: 全て / 保留中 / 未発送 / 発送済
- 配送区分: 全て / FBA
- 列:
  - 注文日 / 配送経路 / 商品画像 / 注文番号・SKU / 商品名
  - 商品価格 / 送料入金 / Amazon 手数料（仮計算or確定）
  - 仕入れ価格（仕入れ台帳連携）
  - **発送代行手数料**（1商品単位で計上、後述）
  - 粗利益 / 利益率
  - 状態 / コンディション
  - 再仕入れ判断リンク（Keepa / ヤフオク / 価格.com）
  - 残在庫数

### 2.3 在庫一覧
**重要**: プライスター同様、**Amazon SP-API から出品情報を取得**（仕入れ台帳は参照のみ）
- フィルタ: ステータス、価格追従設定、検索（SKU/商品名/ASIN）
- 列:
  - ステータス / 更新日時 / ASIN / 商品名・SKU / 作成日 / 数量
  - コンディション / 出品価格 + 送料 / 最低価格 + 送料
  - **価格追従モード**（プリセット5種）
  - 仕入れ価格 / 高値ストッパー / 赤字ストッパー
  - Amazon 手数料 / 粗利益
  - 売れた個数（通算）
  - リサーチリンク（Keepa / ヤフオク / 価格.com）
- 一括操作: 出品停止 / 再開 / 価格追従モード設定

### 2.4 価格自動調整設定（MVP はプリセット5モードのみ）
**MVP でサポートする 5 つのプリセット**:
1. **FBA状態合わせ** — FBA 内で同コンディションの最安値に合わせる
2. **状態合わせ** — 全出品（FBA+自己発送）で同コンディションの最安値
3. **FBA最安値** — FBA 全体の最安値
4. **最安値** — 全出品の最安値
5. **カート価格** — カート獲得中の価格に合わせる

**共通設定（全モード）**:
- 高値ストッパー（上限ガード）
- 赤字ストッパー（下限ガード、仕入れ価格 + α 推奨）

**将来拡張（Phase 2+）**: カスタムモード（13カテゴリの詳細設定）

### 2.5 売上分析
- 期間選択（分析画面と同じ）
- サマリー:
  - 販売数 / 売上金額 / 送料・ギフト入金
  - 仕入金額（自動） / Amazon 手数料（自動） / **その他経費**（手入力+自動）
  - 利益額 / 利益率
- 平均値:
  - 平均売上数・単価 / 平均仕入単価 / ポイント / 返金 / プロモーション
- グラフ: 月別 / 日別 / 曜日別 / 時間帯別
- 日別推移テーブル

### 2.6 経費管理
**月ごとの経費入力 + 自動取得**。

**手入力項目**:
- 人件費 / 交通費 / 送料 / 梱包材 / 消耗品 / その他経費
- 通信費 / 税金 / システム使用料
- プラス計上（加算項目）
- **発送代行手数料（基本料）** ← フジカメラ独自
- **発送代行手数料（1商品単位）** ← フジカメラ独自、後述

**自動取得項目**（SP-API Finances API）:
- FBA 保管手数料 / FBA 長期保管手数料
- ※ プライスターの「月額登録料」は**不要**（プライスター利用料だったため）

**繰り返し設定**:
- 月初に前月の値を自動コピー（プライスター同機能）

### 2.7 ランキング（優先度低・Phase 2）
- 売上個数ランキング / 利益額ランキング（商品別）

---

## 3. 発送代行手数料の扱い（独自仕様）

フジカメラは**発送代行業者**を利用しているため、プライスターには無い独自の経費管理が必要。

### 3.1 データ構造
```
発送代行設定（経費管理画面の「設定」タブから Web 入力）:
  - 基本料（月額固定）: 例 ¥10,000
  - 1商品あたり手数料: 例 ¥300
  - 適用開始月: 例 2026-04（この月から有効。料金改定時に新レコード追加）

計算:
  月間発送代行手数料 = 基本料 + (当月の発送済み注文数 × 1商品あたり手数料)
```

### 3.2 Web 画面からの入力方法
経費管理画面（プライスターの「その他経費」画面と同じスタイル）に以下を追加：
- **「発送代行基本料」入力欄** - 月額固定
- **「発送代行手数料（1商品単位）」入力欄** - 単価
- 繰り返しチェックボックス（前月値自動コピー）
- **履歴表示** - 過去の料金改定履歴を見える化

### 3.3 注文一覧 / 売上分析 での反映
- 各注文行に発送代行手数料（1商品あたり手数料）を自動計上
- 利益 = 売上 − 仕入 − Amazon 手数料 − 発送代行手数料 − その他経費

### 3.4 経費管理画面
- 発送代行基本料 / 1商品手数料の設定 UI
- 繰り返し設定可能（前月値コピー）
- 料金改定時は適用開始月を新規指定して新規レコード保存

---

## 4. 非機能要件

### 4.1 性能
- Polling 間隔: デフォルト **15分**（設定画面で変更可能: 5分〜60分）
- SP-API レート制限を考慮して間隔を決定
- ダッシュボード表示は 1秒以内

### 4.2 可用性
- Server PC で常時稼働（24/7）
- Server PC 再起動時は自動再開

### 4.3 セキュリティ
- **外部からアクセス可能**（スマホ含む、自宅外からも）
- **パスワード認証**必須（Flask-Login or HTTP Basic 認証）
- SP-API 認証情報は `.env`（既存）
- 仕入れ台帳への書き込み権限は限定

### 4.4 外部アクセス方式（候補）
以下のいずれかを採用：

| 方式 | 特徴 | コスト |
|------|------|--------|
| **Cloudflare Tunnel**（推奨） | ポート開放不要、HTTPS 自動、無料、静的IP不要 | ¥0 |
| 自宅ルータのポート転送 + DDNS | 設定複雑、固定IP推奨、セキュリティ注意 | ¥0（無料 DDNS）|
| VPN（Tailscale 等） | 個人用途は無料、スマホアプリあり | ¥0 |
| VPS に移行 | 固定 IP、独立稼働 | 月 ¥500〜 |

**推奨: Cloudflare Tunnel**
- Server PC に `cloudflared` インストール → tunnel 作成 → 自分のドメイン（あるいは trycloudflare.com サブドメイン）でアクセス可能
- 例: `https://fuji.yourdomain.com` → Server PC localhost:8080 に到達
- HTTPS 強制、DDoS 保護もおまけ
- パスワード認証はアプリ側（Flask-Login）で実装

### 4.5 モバイル対応
- **レスポンシブ デザイン必須**（スマホ・タブレットで概ね同じ UI）
- Bootstrap 5 のグリッド / collapse ナビで対応
- テーブルは横スクロール可（モバイルでも全列見える）
- グラフは Chart.js のレスポンシブモード
- タッチ操作最適化（ボタンサイズ、スワイプ対応）

---

## 5. 技術スタック

| 層 | 技術 | 理由 |
|----|------|------|
| バックエンド | **Python 3.11+** / **Flask** | 既存 SP-API コード活用、軽量 |
| DB | **SQLite** | 1ファイル、管理楽、localhost用途には十分 |
| フロントエンド | **HTML + Bootstrap 5 + Chart.js + Vanilla JS** | 依存少、静的配信可能 |
| API 連携 | **python-amazon-sp-api**（既存） + `gspread`（仕入れ台帳） | 既存資産流用 |
| タスクスケジューラ | **APScheduler** | Flask 内で統合、Polling 実行 |
| デプロイ | **Server PC localhost:8080** | 既存の scheduled-tasks 基盤と統合可能 |

### 依存ライブラリ（追加分）
- flask, flask-cors
- apscheduler
- sqlalchemy (or 生 sqlite3)

---

## 6. データモデル（SQLite スキーマ案）

### 6.1 テーブル一覧

```sql
-- 注文（SP-API Orders から取得）
CREATE TABLE orders (
  amazon_order_id TEXT PRIMARY KEY,
  purchase_date TEXT,
  order_status TEXT,
  fulfillment_channel TEXT,  -- AFN / MFN
  marketplace_id TEXT,
  item_price_total REAL,
  shipping_price REAL,
  updated_at TEXT
);

-- 注文商品（SP-API OrderItems）
CREATE TABLE order_items (
  order_item_id TEXT PRIMARY KEY,
  amazon_order_id TEXT,
  asin TEXT,
  seller_sku TEXT,
  title TEXT,
  quantity_ordered INTEGER,
  item_price REAL,
  amazon_fee REAL,           -- 手数料（仮計算/確定）
  amazon_fee_confirmed INTEGER,  -- 0=仮, 1=確定
  condition TEXT,
  shipped_quantity INTEGER
);

-- 在庫（SP-API Listings から取得）
CREATE TABLE inventory (
  seller_sku TEXT PRIMARY KEY,
  asin TEXT,
  title TEXT,
  product_condition TEXT,
  fulfillment_channel TEXT,
  quantity INTEGER,
  listing_price REAL,
  shipping_price REAL,
  min_price_fba REAL,        -- 最低価格（FBA）
  min_price_all REAL,        -- 最低価格（全体）
  cart_price REAL,           -- カート価格
  featured_offer_won INTEGER, -- カート獲得中? 0/1
  updated_at TEXT
);

-- 返品（SP-API Reports → GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA）
CREATE TABLE returns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  return_date TEXT,
  amazon_order_id TEXT,
  seller_sku TEXT,
  asin TEXT,
  fnsku TEXT,
  quantity INTEGER,
  reason TEXT,           -- DEFECTIVE, CUSTOMER_DAMAGED, ...
  detailed_disposition TEXT,
  fulfillment_center_id TEXT,
  customer_comments TEXT
);

-- 仕入れ価格（仕入れ台帳から同期）
CREATE TABLE cost_prices (
  seller_sku TEXT PRIMARY KEY,
  asin TEXT,
  cost_price REAL,
  supplier TEXT,              -- メルカリ / セカスト / トレファク / オフモール
  purchase_date TEXT,
  ledger_row INTEGER,         -- Google Sheets の行番号
  updated_at TEXT
);

-- 価格自動調整設定（SKU 別）
CREATE TABLE price_rules (
  seller_sku TEXT PRIMARY KEY,
  mode TEXT,                   -- fba_condition / all_condition / fba_min / all_min / cart / none
  high_stopper REAL,           -- 高値ストッパー（なければ NULL）
  low_stopper REAL,            -- 赤字ストッパー（なければ NULL、仕入れ価格 + α 推奨）
  active INTEGER,              -- 有効? 0/1
  updated_at TEXT
);

-- 経費（月ごと）
CREATE TABLE expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  year_month TEXT,             -- "2026-04"
  category TEXT,               -- 人件費 / 交通費 / 送料 / 梱包材 ... / 発送代行基本料 / 発送代行手数料
  amount REAL,
  auto_calculated INTEGER,     -- 0=手入力, 1=自動取得（FBA保管等）
  repeat_monthly INTEGER,      -- 繰り返し? 0/1
  note TEXT,
  UNIQUE(year_month, category)
);

-- 発送代行手数料設定
CREATE TABLE shipping_agent_fees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_from TEXT,         -- "2026-04-01" （この月から適用）
  base_fee REAL,               -- 基本料（月額）
  per_item_fee REAL,           -- 1商品あたり手数料
  note TEXT
);

-- Polling 設定
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- 例: ('polling_interval_min', '15')

-- Polling 実行ログ
CREATE TABLE polling_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  success INTEGER,
  message TEXT
);
```

---

## 7. SP-API 連携（既存 + 追加分）

### 7.1 既存で動作中
- Orders API（`fuji_daily_check.py`）
- Reports API（返品レポート取得）
- Catalog Items API（商品情報取得）

### 7.2 新規で必要なロール / API
| 機能 | API | ロール | 状態 |
|------|-----|--------|------|
| 出品在庫取得 | Listings Items API | Product Listing | ✅ 既存権限で可能 |
| **価格更新** | Listings Items API（PATCH） | Product Listing + **Pricing** | ❌ **Pricing ロール追加必要** |
| 競合価格取得 | Product Pricing API | **Pricing** | ❌ **Pricing ロール追加必要** |
| 手数料情報 | Finances API（listFinancialEvents） | Finance and Accounting | ❌ 追加必要 |
| FBA 保管料 | Reports API（LONG_TERM_STORAGE_FEE_CHARGES） | Amazon Fulfillment | ✅ 既存 |

### 7.3 必要なロール追加作業
1. ソリューションプロバイダーポータル → `fuji-camera-internal` アプリ編集
2. 以下ロールを追加:
   - **Pricing**（価格取得・更新、競合価格）
   - **Finance and Accounting**（手数料の確定値取得）
3. 保存後、**Refresh Token 再発行**（既存 Token は新ロール未対応）
4. `.env` の `FUJI_REFRESH_TOKEN` を差し替え

---

## 8. 価格自動調整エンジン（MVP）

### 8.1 アルゴリズム
```python
def decide_new_price(sku_info, rule, competitor_data):
    """SKU の新価格を決定（プリセット5モード）"""
    
    # モード別 target_price 算出
    if rule.mode == 'fba_condition':
        target = competitor_data.min_price_fba_same_condition
    elif rule.mode == 'all_condition':
        target = competitor_data.min_price_same_condition
    elif rule.mode == 'fba_min':
        target = competitor_data.min_price_fba
    elif rule.mode == 'all_min':
        target = competitor_data.min_price_all
    elif rule.mode == 'cart':
        target = competitor_data.cart_price
    else:
        return None  # 自動調整なし
    
    if target is None:
        return None  # 競合データ不足、変更なし
    
    # ストッパーガード
    if rule.high_stopper and target > rule.high_stopper:
        target = rule.high_stopper
    if rule.low_stopper and target < rule.low_stopper:
        return None  # 赤字になる、変更しない
    
    # 現価格と比較、変更なら更新
    current = sku_info.listing_price
    if abs(target - current) < 1:  # 1円未満の差は更新しない
        return None
    
    return target
```

### 8.2 実行タイミング
- Polling（15分毎）で在庫取得後
- 各 SKU で `decide_new_price` 実行
- 返却値があれば SP-API で `listings.put_listings_item` 更新

### 8.3 安全機構
- **1日の値動き上限**: 例「±10% 以上は変更しない」（暴落防止）
- **変更前に DB にログ記録**（old_price / new_price / reason）
- **DRY RUN モード**: 実際の書き込みなしでシミュレート

---

## 9. デプロイ構成

### 9.1 ディレクトリ構成
```
C:\claude\fuji-dashboard\
├── architecture.md      # 本書
├── app.py               # Flask エントリーポイント
├── polling.py           # 15分毎の SP-API 同期
├── price_engine.py      # 価格調整エンジン
├── db.py                # SQLite 接続・スキーマ
├── config.py            # 設定（Polling 間隔等）
├── data.db              # SQLite DB（gitignore）
├── static/              # CSS / JS / images
│   ├── css/
│   ├── js/
│   └── images/
├── templates/           # Flask テンプレート
│   ├── base.html
│   ├── dashboard.html
│   ├── orders.html
│   ├── inventory.html
│   ├── price_rules.html
│   ├── analytics.html
│   └── expenses.html
└── logs/                # 実行ログ
```

### 9.2 起動方法
```bash
cd C:\claude\fuji-dashboard
python app.py            # localhost:8080
```

ブラウザで http://localhost:8080 にアクセス

### 9.3 常時起動
- Windows スタートアップに登録 or
- scheduled-tasks で再起動管理（プロセス死活監視）

---

## 10. 開発ロードマップ

### Phase 0: 準備（1〜2日）
- [ ] SP-API 追加ロール（Pricing / Finance）申請・承認
- [ ] Refresh Token 再発行
- [ ] Flask プロジェクト初期化
- [ ] DB スキーマ実装

### Phase 1: MVP（2〜3週間）
- [ ] Polling 基盤（15分毎、Orders / Listings / Returns）
- [ ] 仕入れ台帳 連携（読み取り）
- [ ] ダッシュボード画面
- [ ] 注文一覧画面
- [ ] 在庫一覧画面
- [ ] 経費管理画面（発送代行含む）
- [ ] 売上分析画面（基本グラフ）

### Phase 2: 価格調整（1〜2週間）
- [ ] プリセット5モードの価格エンジン
- [ ] 高値/赤字ストッパー
- [ ] SKU 別ルール設定 UI
- [ ] DRY RUN テスト → 本番適用

### Phase 3: 改善・拡張（継続）
- [ ] カスタムモード（13カテゴリ詳細）
- [ ] ランキング
- [ ] 返品詳細分析
- [ ] プッシュ通知（Amazon SQS、必要に応じて）

---

## 11. 未解決事項（更新: 2026-04-23 ユーザー確認）

| # | 項目 | ステータス |
|---|------|----------|
| 1 | **SP-API ロール追加**（Pricing / Finance）| ✅ ユーザー側で追加済み想定（Phase 0 で動作確認） |
| 2 | 仕入れ台帳との連携頻度 | Polling と同タイミング（15分毎）で同期予定 |
| 3 | 発送代行手数料の具体的金額 | ✅ Web 画面から入力方式で確定（数値は稼働後ユーザーが入力）|
| 4 | 外部アクセス & 認証 | ✅ **Cloudflare Tunnel + Flask-Login パスワード認証**で確定 |
| 5 | スマホ表示 | ✅ **レスポンシブで PC とほぼ同じ UI**、Bootstrap 5 で実装 |

### Phase 0 で残る確認作業
- SP-API Pricing / Finance ロールが実際に有効か `scripts/common/sp_api_client.py` を使ってテスト呼び出し
- 有効でない場合、ロール追加 + Refresh Token 再発行

---

## 12. 既存資産との連携

- **daily_update.py**: 仕入れ台帳への自動入力（継続運用）
- **fuji_daily_check.py**: Amazon 注文の日次確認（簡易版、ダッシュボード完成後は不要になる可能性）
- **scheduled-tasks**: 物販管理の夜間実行。ダッシュボードの Polling と競合しないよう注意
- **amazon-seller-automation**: SP-API クライアント共通基盤（`sp_api_client.py`）を流用
