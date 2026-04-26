# Amazon Seller Central 自動化プロジェクト

## プロジェクト概要
2店舗のAmazon Seller Central業務をSP-API経由で自動化する。
- **フジカメラ**(FBA発送・カメラ店)
- **You and Me**(自己発送・輸入代理店)

常時起動のClaude Codeから日次で実行する運用を想定。

## 技術スタック
- 言語: Python 3.11+
- SP-APIクライアント: `python-amazon-sp-api`
- Googleスプレッドシート連携: `gspread`(サービスアカウント認証)
- 環境変数管理: `python-dotenv`

## ディレクトリ構成
```
amazon-seller-automation/
├── CLAUDE.md
├── .env                       # gitignore対象
├── .gitignore
├── requirements.txt
├── secrets/                   # gitignore対象
│   └── gcp-sa.json
├── scripts/
│   ├── fuji_daily_check.py
│   ├── yandme_daily_check.py
│   └── common/
│       ├── __init__.py
│       └── sp_api_client.py
├── templates/
│   └── reply_templates.md     # 後日追加
├── logs/
└── setup/
    └── test_connection.py     # 認証テスト用
```

## 現在のフェーズ: 初期セットアップ

以下の順序で、ユーザー(かおる)と対話しながら進めること。
**各ステップで必ずユーザーに進捗を確認してから次に進む。一気に進めない。**

ユーザーはSP-APIの登録状況を把握していないので、画面の見方から一緒に確認する。

---

### Phase 0: 環境準備

1. 以下のファイルを作成:
   - `.gitignore`(`.env`、`secrets/`、`logs/`、`__pycache__/`、`*.pyc` を含める)
   - `requirements.txt`(下記参照)
   - 空の `logs/` ディレクトリ
   - 空の `secrets/` ディレクトリ

2. `requirements.txt` の内容:
   ```
   python-amazon-sp-api
   gspread
   google-auth
   python-dotenv
   ```

3. 仮想環境の作成と依存関係のインストールをユーザーにガイド:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windowsは venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. Git初期化(任意だがユーザーに推奨)

---

### Phase 1: SP-API登録状況の確認

フジカメラとYou and Me、それぞれのSeller Centralで登録状況を確認する。
**1店舗ずつ進める。フジカメラから始める。**

ユーザーに以下を案内:

1. フジカメラのSeller Centralにログイン
2. 右上メニューから「**アプリとサービス**」→「**アプリストアを管理**」
3. 画面右側の「**開発者セントラル**」または「**開発者プロファイル**」にアクセスできるか確認
4. アクセスできない場合:
   - 「開発者プロファイルを登録」のリンクから登録申請が必要
   - 以下の情報を記入するよう案内:
     - アプリ名: `fuji-camera-internal`(店舗ごとに変える)
     - 用途: 「自社の出品・注文・返品・バイヤーメッセージの確認業務の効率化」
     - データ利用範囲: 社内のみ(Private)
     - PII(個人情報)へのアクセス: 必要(自己発送の発送業務で使用)
   - 審査は通常数日〜1週間

5. 既に登録済みの場合: Developer IDを確認し、メモしてもらう

**フジカメラで完了したら、同じ手順をYou and Meでも実施する。**

---

### Phase 2: LWAアプリの作成とRefresh Token取得

各店舗の開発者セントラルで以下を実施。**店舗ごとに別々のアプリを作る。**

1. 開発者セントラルで「**アプリを追加**」(または「新しいアプリを登録」)
2. アプリ名を入力:
   - フジカメラ: `fuji-camera-internal`
   - You and Me: `yandme-internal`
3. 必要なロール(権限スコープ)を選択:

   **フジカメラ(FBA)で必要なロール:**
   - Product Listing
   - Inventory and Order Tracking
   - Amazon Fulfillment(FBA返品レポート取得に必須)
   - Buyer Communication(問い合わせ取得に必須)

   **You and Me(自己発送)で必要なロール:**
   - Product Listing
   - Inventory and Order Tracking
   - Direct-to-Consumer Shipping(自己発送の注文詳細取得に必須)
   - Buyer Communication(問い合わせ取得に必須)
   - Pricing(必要に応じて)

   ※ PII(個人情報)を扱うロールにはチェックを入れる必要あり。自己発送では住所取得のため必須。

4. アプリ作成後、以下を取得:
   - **LWA Client ID**
   - **LWA Client Secret**(表示は1回限りのことが多いので必ずコピー)

5. 「**自己承認(Self-authorize)**」または「認可」ボタンから **Refresh Token** を発行
   - 画面に表示されるRefresh Tokenを即座にコピー(再表示できない場合あり)

6. **Seller IDの確認:**
   - Seller Central右上のアカウント情報、または「設定」→「出品者アカウント情報」→「あなたの出品者トークン」から取得
   - 形式例: `A1B2C3D4E5F6G7`

**両店舗ともPhase 2完了時点で、以下6項目×2店舗=12項目が揃う:**
- LWA Client ID
- LWA Client Secret
- Refresh Token
- Seller ID

---

### Phase 3: 認証情報の設定

1. プロジェクトルートに `.env` を作成(コミットしない):

   ```env
   # フジカメラ
   FUJI_LWA_CLIENT_ID=
   FUJI_LWA_CLIENT_SECRET=
   FUJI_REFRESH_TOKEN=
   FUJI_SELLER_ID=

   # You and Me
   YANDME_LWA_CLIENT_ID=
   YANDME_LWA_CLIENT_SECRET=
   YANDME_REFRESH_TOKEN=
   YANDME_SELLER_ID=

   # 共通
   MARKETPLACE_ID=A1VC38T7YXB528
   SP_API_REGION=FE

   # Googleスプレッドシート
   GOOGLE_SERVICE_ACCOUNT_JSON_PATH=./secrets/gcp-sa.json
   YANDME_SPREADSHEET_ID=REDACTED_SHEET_ID
   ```

2. ユーザーにサービスアカウントJSONを `secrets/gcp-sa.json` に配置してもらう
   - ※ You and Meのスプレッドシートへのエディター権限は追加済み

3. `.env` に値を入力してもらう(Claude Codeが直接書き込まず、ユーザーに依頼する)

---

### Phase 4: 共通SP-APIクライアントの作成

`scripts/common/sp_api_client.py` を作成する。
店舗識別子('fuji' / 'yandme')を渡すと、対応する認証情報でSP-APIクライアントを返す設計。

要件:
- `python-amazon-sp-api` の `Credentials` を使って店舗ごとに切り替え
- Marketplace: 日本(`Marketplaces.JP`)
- エラーハンドリング(認証エラー時はわかりやすいメッセージ)
- シンプルな関数インターフェース(例: `get_client(shop: str, api_class)`)

---

### Phase 5: 接続テスト

`setup/test_connection.py` を作成し、以下を実行:

1. 両店舗でLWAトークン取得が成功すること
2. `Sellers API` の `get_marketplace_participation()` が正常レスポンスを返すこと
3. 取得結果から店舗名・Marketplace IDを表示
4. 失敗時は、どのステップで失敗したか明確にログ出力

実行コマンド:
```bash
python setup/test_connection.py
```

期待出力の例:
```
[フジカメラ] 接続OK - Marketplace: Amazon.co.jp
[You and Me] 接続OK - Marketplace: Amazon.co.jp
すべての接続テストに成功しました。
```

両店舗ともテストが通ったら、**初期セットアップ完了**。

---

### Phase 6: 次フェーズへの引き継ぎ

セットアップ完了後、ユーザーに以下を報告:
- セットアップ完了のサマリー
- 次フェーズ(日次運用機能の実装)に必要な情報を求める:
  1. 問い合わせ返信のテンプレート
  2. You and Meスプレッドシートへの記入ルール詳細(どのSP-APIフィールドをどの列に入れるか)
  3. 日次実行のトリガー方法(手動コマンド / cron / タスクスケジューラ)

この時点で本 `CLAUDE.md` は一旦役目を終える。
日次運用用の指示書は別途 `CLAUDE_DAILY.md` として作成する。

---

## 運用ルール(全フェーズ共通)

- `.env`、`secrets/` は**絶対にコミットしない**
- SP-APIのレート制限を尊重(`python-amazon-sp-api` は自動リトライあり)
- エラーと実行ログは `logs/YYYY-MM-DD.log` に記録
- **現フェーズでは読み取り専用**。Amazon側への書き込み(返信送信・ステータス変更等)は一切行わない
- ユーザーの明示的な承認なく、新しいAPIスコープの追加や認証情報の変更は行わない
- 不明点・判断に迷う点があれば、推測で進めず必ずユーザーに確認する

## 将来の拡張予定(今は実装しない)

- 返品商品の確認と報告(フジカメラ)
- 問い合わせの確認と返信文案の生成(両店舗)
- 店舗評価の確認と返信文案の生成(フジカメラ・手段未定)
- 自己発送の注文情報をスプレッドシートへ転記(You and Me)
- 発送期限3日前アラート(You and Me)

## メモ
- 日本のMarketplace ID: `A1VC38T7YXB528`
- SP-APIリージョン: 極東(`FE` / Far East)
- エンドポイント: `https://sellingpartnerapi-fe.amazon.com`
