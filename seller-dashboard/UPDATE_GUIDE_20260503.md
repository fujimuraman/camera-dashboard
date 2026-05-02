# camera-dashboard アップデート手順書（2026-05-03 版）

> このファイルは、4日前くらいに camera-dashboard を導入した友人向けに、
> Claude Code（または同等のコーディングAI）に渡してアップデートを実施してもらうための指示書です。

---

## このアップデートで変わること（ユーザー視点）

### 在庫一覧
- **「分析」列**を新設。`Keepa` リンクに加えて **売れ行きランク S/A/B/C** バッジを表示
- ランクの判定基準は **凡例ホバー** で見られる
- **追従モード（リプライサー）が実機能化**。`offers_json` から最低価格を動的計算し、acceptable/poor コンディション除外、自分の出品除外、警告UI 等の改善を反映
- ストッパー（上限/下限）の名称・仕様を改善

### 売上分析
- **円グラフ2つ追加**: 販売スピード分布（登録日→販売日の経過日数） + 在庫ランク分布
- **前月・前期間との累計売上線**を月別/日別グラフに追加（モチベ比較用）
- **横軸が常に全期間表示**: 当月日別=月初〜月末、年間月別=1〜12月、データなし日も0表示
- グラフの凡例順を統一
- 注文一覧の絞り込みUIを売上分析と同じ preset（this/prev/year/custom）に統一

### カメラ市況分析（新機能・副業 No.12）
- **自社の仕入れ対象リストに連動した BSR 追跡機能**を新規追加
- 100ASIN単位でランダム順にBSR取得、ヒステリシス（残量50で停止、200で再開）
- 1周完了で round_maintenance（注文ASIN取込・ゾンビ除去）
- 過去5年の min-max で 10〜90 にスケーリングした「市場活況度スコア」を月別グラフに追加
- 設定UIをアコーディオン化、**デフォルトカメラリストの ON/OFF**、**CSVアップロード対応**
  - カメラ事業以外の人や独自リストを使いたい人向け

### 細かい改善
- HTML/静的ファイル no-cache 化（Cloudflare キャッシュで古い画面が見える問題を解消）
- 売れ行きランクの閾値を 70%→60% に緩和

---

## 友人へのお願い

このファイルを **そのまま Claude Code（または同等のAI）にコピペ** して以下のように指示してください：

```
このアップデート手順書（UPDATE_GUIDE_20260503.md）に沿って camera-dashboard をアップデートしてください。
```

---

## 🤖 AIへの実施指示（ここから下を実行してください）

### 前提
- ユーザーは `camera-dashboard` リポジトリを `git clone` でローカルに置いている
- リポジトリのパス例: `C:\camera-dashboard\` または `~/camera-dashboard/`
- Flask アプリ（`app.py`）が `python app.py` で起動する状態
- SQLite DB（`data/seller.db` or 同等）が運用中

### Step 1: 現状確認
```bash
# リポジトリのルートで
git status
git log --oneline -5
```

ローカル変更がある場合は **必ずユーザーに確認** してから先に進む（勝手に commit/stash しない）。

### Step 2: Flask プロセス停止

Windows PowerShell の場合:
```powershell
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
  (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*camera-dashboard*app.py*"
} | Stop-Process -Force
```

Mac/Linux の場合:
```bash
pkill -f "camera-dashboard.*app.py"
```

> **重要**: Flask が動いたままだと SQLite が書き込みロックでアップデートがコケる事があります。

### Step 3: 最新版を取得
```bash
git pull origin main
```

衝突したらユーザーに報告して停止。

### Step 4: 依存関係更新
```bash
pip install -r requirements.txt --upgrade
```

> 新規追加された依存はないはずだが念のため。

### Step 5: DB マイグレーション
本リリースで新規追加されたテーブル:
- `market_bsr_meta`
- `market_bsr_history`
- `market_score_cache`

これらは `db.py` の `CREATE TABLE IF NOT EXISTS` で自動作成されるので、**Flask 起動時に勝手に追加されます**。手動マイグレ不要。

確認だけしたい場合:
```bash
python -c "from db import get_db; conn=get_db(); print([r['name'] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])"
```

`market_bsr_meta`, `market_bsr_history`, `market_score_cache` が出てくれば OK。

### Step 6: カメラ市況分析の初期設定（任意）

新機能なので、ユーザーに使うかどうか確認:

#### Aパターン: 使わない（カメラ事業者ではない／興味なし）
- 設定画面で **「カメラ市況分析を有効化」を OFF** のまま放置
- 完了

#### Bパターン: デフォルトのカメラリストで使う
- 設定画面 → 市況分析 → **「デフォルトカメラリストを使用」を ON**
- 配布リポジトリ同梱の `data/target_asins.json`（1,573 ASIN）が読み込まれる
- 約 16 日で 1 周（14.4 分間隔で 1 ASIN 取得）

#### Cパターン: 自分の仕入れ対象リストで使う
- 設定画面 → 市況分析 → **「デフォルトカメラリストを使用」を OFF** + **CSV アップロード**
- CSV フォーマット: 1列目に ASIN（B0XXXXXXXX）、ヘッダ行は任意
- アップロード後、Keepa API キーが設定されていれば自動でポーリング開始

#### 共通: Keepa API キー
カメラ市況分析を使うには Keepa API キー（Basic plan 以上）が必要:
- 設定画面 → API → Keepa API Key を入力
- API トークン: 5T/分 消費、最大保有 300 トークン

### Step 7: Flask 再起動

Windows:
```powershell
cd C:\camera-dashboard  # 各自のパスに合わせて
Start-Process -FilePath "python" -ArgumentList "app.py" -WorkingDirectory "$PWD" -WindowStyle Hidden
```

Mac/Linux:
```bash
cd ~/camera-dashboard  # 各自のパスに合わせて
nohup python app.py > flask.log 2>&1 &
```

### Step 8: 動作確認
ブラウザで以下を確認:

1. `http://localhost:8080/` → ダッシュボードが表示される
2. `http://localhost:8080/orders` → 注文一覧
3. `http://localhost:8080/inventory` → 在庫一覧、**「分析」列に S/A/B/C ランクバッジが出ている**
4. `http://localhost:8080/analytics` → 売上分析、**横軸が全期間表示**、**円グラフ2つ**、**前期間累計線**
5. `http://localhost:8080/settings` → 設定、**カメラ市況分析セクションがアコーディオン化**

うまく表示されない場合:
- ブラウザのスーパーリロード（Ctrl+Shift+R / Cmd+Shift+R）
- Flask のログ（`flask.log`）にエラーが出ていないか確認

### Step 9: 完了報告
ユーザーに以下を伝えて完了:
- アップデートが完了したこと
- カメラ市況分析の初期設定方針（A/B/C のどれにしたか）
- 動作確認で気になった点があれば

---

## トラブルシューティング

### Flask が起動しない
- ポート 8080 が他プロセスで使われていないか確認
- `flask.log` のエラー内容をユーザーに見せて判断を仰ぐ

### git pull で衝突
- ユーザーが手動で改造していた可能性
- `git stash` してから `git pull` → 後で `git stash pop` で復活、必要ならマージ
- **勝手に解決しない**。ユーザーに確認

### 「分析」列にランクが出ない
- 在庫データに `offers_json` が無いと判定不能
- `python polling.py once` を一度回すと `offers_json` が埋まる

### カメラ市況分析が動かない
- Keepa API キー未設定 → 設定画面で入力
- ヒステリシス（残量50以下）で停止中の可能性 → トークン回復後に自動再開
- `logs/market_debug.log` を確認

### Cloudflare キャッシュで古い画面
- 既に no-cache 化されているので、ブラウザ側のキャッシュが原因の可能性
- スーパーリロードで解決するはず

---

## 連絡先

不明点・困りごとは オーナー（藤原）まで。
本ファイルは `2026-05-03` 時点のリポジトリ状態に対応。
