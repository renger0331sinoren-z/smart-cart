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
`DEPOT_LAT`, `DEPOT_LON`(地図を最初に開く位置) / `MAX_TRIP_M`(出発地〜配送先の直線距離の上限・既定30000) /
`GEO_CONTACT`(外部地図サービスへ名乗る連絡先。メールアドレス推奨)

## 地図・検索・経路(外部の無料公開サービス)
- 地図: 国土地理院の航空写真・地理院地図(出典表示つき)/ 場所検索: 国土地理院 + OpenStreetMap(Nominatim)
- 道路経路: OSRM(routing.openstreetmap.de)/ 信号機の位置: OpenStreetMap(Overpass)
- いずれも無料の公開サーバーで、大量利用・商用運用は不可です。本格運用では自前のサーバー(`ROUTING_URL` `NOMINATIM_URL` `OVERPASS_URL`)に切り替えてください。
- 外部サービスに接続できないときは、予約はでき、出発時に簡易経路(直線)になります。
