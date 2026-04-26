# seller-dashboard

フジカメラ自社運用ダッシュボード（プライスター代替）

## 起動方法

```bash
cd C:\claude\seller-dashboard
python app.py
```

初回起動時に `admin` ユーザーが自動作成される。パスワードは：
- 環境変数 `FUJI_DASH_PASSWORD` で指定可能
- 未指定なら起動ログに表示されるランダムパスワード

ブラウザで http://localhost:8080 にアクセス。

## 構成

- `app.py` - Flask アプリ本体
- `config.py` - 設定
- `db.py` - SQLite 接続・スキーマ
- `auth.py` - 認証（Flask-Login）
- `templates/` - HTML テンプレート
- `static/` - CSS / JS

## 現状（Phase 1 初期）

- ✅ 7画面の骨組み完成（ダッシュボード / 注文 / 在庫 / 価格調整 / 分析 / 経費 / 設定）
- ✅ 認証（パスワード方式）
- ✅ SQLite スキーマ
- ✅ レスポンシブ（Bootstrap 5）
- ⏳ **Polling 未実装**（データは空）
- ⏳ 価格調整エンジン未実装
- ⏳ Cloudflare Tunnel 未設定（外部アクセス）

## 次のステップ

1. Polling 基盤構築（SP-API → SQLite）
2. 仕入れ台帳連携
3. 価格調整エンジン
4. Cloudflare Tunnel セットアップ


