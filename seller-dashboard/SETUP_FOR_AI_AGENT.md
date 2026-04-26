# seller-dashboard セットアップ指示書（AIエージェント向け）

このドキュメントは、**Amazon セラーが自分のAIエージェント（Claude等）にこのダッシュボードを組み込んでもらう** ことを想定した手順書です。

AIエージェントはこの指示書に従ってセットアップを進めてください。
**ユーザー操作が必要な部分は明示的に「ユーザーに依頼」と書いてあります。** AIが勝手に進めず必ず確認してください。

---

## このダッシュボードの概要

Amazon Seller Central の業務（在庫・注文・返品・経費）を一元管理する Web ダッシュボード。
**プライスター代替** として作られた MVP。

### 主な機能
- 在庫一覧（仕入値・最低価格・利益率）
- 注文一覧（販売日数・粗利益）
- 売上分析（日別/月別/曜日別/時間帯別チャート）
- 決算（月別比較表・P/L・B/S。確定申告用）
- 返品・経費・設定
- Amazon SP-API 連携（自動取込）
- Google スプレッドシート連携（仕入れ台帳・オプション）

### 前提条件（ユーザー側で必要なもの）

ユーザーに以下を準備してもらってください：

1. **Amazon Seller Central アカウント**（プロフェッショナル契約）
2. **Amazon SP-API 開発者登録 + Refresh Token**
   - Seller Central → アプリストアを管理 → 開発者セントラル
   - 必要ロール: Product Listing / Inventory and Order Tracking / Pricing 等
3. **Python 3.10 以上**
4. **（オプション）Google Cloud Console のサービスアカウント JSON**
   - 仕入れ台帳をGoogle Sheetsで管理する場合のみ必要

---

## セットアップ手順

### Step 1: ソースコード配置

ユーザーから渡された seller-dashboard ディレクトリを適切な場所に配置：

```
<プロジェクトルート>/
├── seller-dashboard/        ← 本体
└── amazon-seller-automation/  ← SP-API クライアント
    ├── .env               ← 認証情報（後で作成）
    └── secrets/           ← サービスアカウントJSON配置先
```

**両ディレクトリが同じ親ディレクトリ配下にあること** が前提（相対パスで参照）。

### Step 2: Python 依存関係インストール

AIエージェントが自動で実行：

```bash
cd seller-dashboard
pip install -r requirements.txt
```

### Step 3: amazon-seller-automation の .env 作成

`<プロジェクトルート>/amazon-seller-automation/.env` を作成：

```env
# 必須: SP-API 認証情報（ユーザーから取得）
FUJI_LWA_CLIENT_ID=<ユーザー入力>
FUJI_LWA_CLIENT_SECRET=<ユーザー入力>
FUJI_REFRESH_TOKEN=<ユーザー入力>
FUJI_SELLER_ID=<ユーザー入力>

# 共通
MARKETPLACE_ID=A1VC38T7YXB528  # Amazon.co.jp
SP_API_REGION=FE                # 極東リージョン

# Googleスプレッドシート（オプション、台帳を使わなければ空でOK）
GOOGLE_SERVICE_ACCOUNT_JSON_PATH=./secrets/gcp-sa.json
```

> ⚠ **ユーザーに依頼**: `FUJI_LWA_*` と `FUJI_REFRESH_TOKEN` と `FUJI_SELLER_ID` の値はユーザー本人が SP-API 開発者ポータルから取得する必要があります。AIが代行不可。
>
> ※「FUJI_」プレフィックスはこのテンプレートのデフォルト名。`SHOP_KEY` 環境変数（後述）で変更可能。

### Step 4: seller-dashboard の .env 作成

`<プロジェクトルート>/seller-dashboard/.env` を作成：

```env
# 仕入れ台帳（オプション、使わなければ空でOK）
SPREADSHEET_ID=<ユーザーの Google Sheets ID、不要なら空>

# サービスアカウントJSONのパス（仕入れ台帳を使う場合のみ）
GOOGLE_CREDS_PATH=<絶対パス、例: C:/myproject/credentials.json>

# SP-API .env のパス（amazon-seller-automation の .env を流用）
SP_API_ENV_PATH=<絶対パス、例: C:/myproject/amazon-seller-automation/.env>

# Shop識別子（amazon-seller-automation/.env のキー名と連動）
# SHOP_KEY=fuji だと FUJI_REFRESH_TOKEN 等を参照
SHOP_KEY=fuji

# Marketplace（Amazon.co.jp 以外なら変更）
MARKETPLACE_ID=A1VC38T7YXB528

# Flask 起動ポート（デフォルト 8080）
# FUJI_DASH_PORT=8080
```

### Step 5: 仕入れ台帳サービスアカウント JSON 配置（オプション）

仕入れ台帳を Google Sheets で管理する場合のみ：

1. ユーザーから `credentials.json`（Google サービスアカウントの鍵）を入手
2. `GOOGLE_CREDS_PATH` で指定したパスに配置
3. ユーザーに依頼: **対象スプレッドシートをサービスアカウントのメールアドレスに「編集者」権限で共有**

> ⚠ **ユーザーに依頼**: Google Cloud Console でサービスアカウント作成と JSON ダウンロードはユーザー本人の操作が必要です。

### Step 6: Flask 起動

```bash
cd seller-dashboard
python app.py
```

ブラウザで http://localhost:8080/ を開く。

### Step 7: 初期セットアップ画面（自動表示）

初回起動時、`/setup` 画面に自動リダイレクトされます。

ユーザーに以下を入力してもらう：
- **ショップ名**: 表示用（例: "My Camera Shop"）
- **管理者ユーザー名**: ログインID（デフォルト: admin）
- **パスワード**: 6文字以上

> ⚠ **ユーザーに依頼**: パスワードはユーザー本人が決めてください。AIが代行しない。

完了後、`/login` 画面にリダイレクトされます。

### Step 8: ログイン → 設定確認

設定画面（`/settings`）で以下を確認：
- SP-API 接続テスト
- Polling 間隔（デフォルト10分）
- 価格自動調整の有効/無効

### Step 9: 初回データ同期

ナビバー右上の「⟳ 全同期」ボタンをクリック。
Amazon SP-API から在庫・注文・返品データが取り込まれます（数分かかる）。

---

## トラブルシューティング

### Q. /setup 画面で「ユーザーが既に存在します」と出る
A. DB に既存ユーザーがいる。`seller-dashboard/data.db` を削除すれば再セットアップ可能。

### Q. ログイン後に Internal Server Error
A. `.env` の `SP_API_ENV_PATH` のパスが間違っている可能性。絶対パスを確認。

### Q. 「⟳ 全同期」が失敗する
A. Refresh Token が失効している or Seller ID/Marketplace ID が違う。`amazon-seller-automation/.env` を再確認。

### Q. 仕入れ台帳が同期されない
A.
1. `SPREADSHEET_ID` が正しいか
2. `GOOGLE_CREDS_PATH` が正しいか
3. サービスアカウントメールが対象スプレッドシートに「編集者」として共有されているか

---

## カスタマイズ

このダッシュボードは「カメラ転売向け」に作られたため、他カテゴリで使う場合は以下を調整可能：

- **Amazon手数料率**: `app.py` の `estimate_amazon_fee_rate()` 関数（デフォルト: カメラ8% / レンズ10%）
- **発送代行手数料**: `経費` 画面で月額固定費・1商品手数料を入力
- **B/S 期首値**: `決算` 画面で手入力

---

## AIエージェントとして守ってほしいこと

1. **ユーザーの認証情報（パスワード・APIキー）を勝手に決めない**。必ずユーザーに入力してもらう
2. **`.env` ファイル等の秘密情報を Git にコミットしない**（`.gitignore` で保護されている）
3. **作業中に詰まったらユーザーに確認**。推測で進めない
4. **SP-API の利用料金が発生する可能性** をユーザーに伝える
5. **初回起動時のパスワード**: `/setup` 画面で必ずユーザー本人が設定する

セットアップ完了後は通常運用に入ります。設定画面・経費画面・決算画面はAIエージェントが補助できますが、**お金が動く操作（価格自動調整の有効化、Amazon API への PATCH 等）はユーザーの明示的な許可** を取ってから実行してください。
