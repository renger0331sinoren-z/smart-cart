# スマート台車 配送予約システム(シミュレーション版)

設計は `DESIGN.md` を参照。実機制御は `hardware.py` で無効化されています。

## ローカル起動
```
pip install -r requirements.txt
ADMIN_PASSWORD=8文字以上のパスワード python app.py     # http://localhost:5000
python tests/test_flow.py                              # 安全ルール・権限のテスト
```
ローカルはSQLite(`instance/app.db`)。`DATABASE_URL` を設定するとPostgreSQLを使います。

## Renderで公開
1. このフォルダをGitHubへ push し、Renderで「Blueprint」として `render.yaml` を読み込む。
2. 環境変数 `ADMIN_PASSWORD`(8文字以上)を設定。`SECRET_KEY` と `DATABASE_URL` は自動設定。
3. データはPostgreSQLに保存されるため、再起動しても消えません。

## 環境変数
`ADMIN_NAME`(既定 admin) / `ADMIN_PASSWORD` / `SECRET_KEY` / `DATABASE_URL` /
`DEPOT_LAT`, `DEPOT_LON`(出発地の座標) / `SERVICE_RADIUS_M`(配送エリア半径・既定3000)
