# AI電動スマート台車 配送予約システム 全体設計

> **重要**: このリポジトリは「画面上のシミュレーション」です。実機の走行・電子ロック制御は
> `hardware.py` の `REAL_HARDWARE_ENABLED = False` により**コード上でも無効**です。
> 有効化は、非常停止・通信断時停止・障害物検知・速度制限・操作権限などの安全設計を
> 専門家が検証した後にのみ行ってください(UIや環境変数からは切り替えられません)。

## 1. 構成

```
ブラウザ(利用者 / 管理者)  ──HTTPS──  Flask(gunicorn)  ──  PostgreSQL(Render) / SQLite(ローカル)
   HTML + CSS + JS(Leaflet地図)         REST JSON API         users / reservations / events
```

- サーバー: Python 3 + Flask。DBは `DATABASE_URL` があればPostgreSQL、無ければSQLite(ローカル開発用)。
- 画面: HTML / CSS / JavaScript(フレームワーク無し)。地図はLeaflet + OpenStreetMap。
- リアルタイム通知: 管理者画面が1秒ごとに `/api/admin/tick` を呼び、新着イベントを取得(ポーリング方式)。
- シミュレーション駆動: 台車の「前進」は**管理者画面が開いていて、シミュレーション実行中のときだけ**進む。
  管理者画面が閉じていれば台車は動かない(フェイルセーフ)。

## 2. ファイル構成

```
smart-cart/
├─ app.py              Flask本体(認証・予約API・管理者API・セキュリティヘッダ)
├─ geo.py              場所検索・道路経路・信号機位置(外部の無料公開サービス)
├─ db.py               DB層(SQLite/PostgreSQL両対応・トランザクション)
├─ sim.py              台車の状態遷移と安全ルール(純粋関数・テスト対象の中核)
├─ hardware.py         実機アダプタの口(常に無効。有効化はコード変更+専門家検証が前提)
├─ requirements.txt
├─ render.yaml         Render用(Webサービス + PostgreSQL)
├─ templates/  base.html  login.html  user.html  admin.html
├─ static/css/app.css
├─ static/js/  common.js  user.js  admin.js
└─ tests/test_flow.py  安全ルールと権限のテスト
```

## 3. データモデル

| テーブル | 主な列 |
|---|---|
| users | id, name(一意), password_hash, role(user/admin), created_at |
| reservations | id, user_id, scheduled_at, address, lat, lon, item_desc, forgotten_items, notes, status, hold_reason, cart_lat/lon, progress_m, route, locked, unlock_expires_at, obstacle, comms_ok, sensor_ok, signal_state |
| events(履歴) | id, ts, reservation_id, actor, kind, severity, detail |

`events` は予約・ログイン・出発・停止・再開・開錠・施錠・センサー入力など**すべての操作を追記のみ**で保存します。

## 4. 配送ステータスと遷移

```
reserved ─(管理者:出発)→ en_route ─(到着)→ arrived ─(顔検知)→ unlock_pending
                              │                                   │
                       (停止 hold_reason)                 管理者が「開ける」→ unlocked ─(30秒/異常/管理者)→ 施錠
                                                          管理者が「開けない」→ denied
completed / cancelled(利用者は reserved のときだけキャンセル可)
```

## 5. 安全ルール(sim.py で強制・テスト済み)

1. **施錠が既定**。`locked=0` になるのは `unlocked` のときだけ。到着・顔検知だけでは開かない。
2. 開錠は管理者の明示操作のみ。通信断・センサー異常なら開錠を拒否し**施錠維持**。
3. 開錠は30秒で自動施錠。開錠中に通信断・センサー異常になれば即施錠。
4. 障害物(人・車・物)検知で停止。**消えても自動再開しない**。管理者が障害物なしを確認して再開許可。
5. 曲がり角・見通しの悪い場所で自動停止し通知。管理者が映像と位置を見て再開許可。
6. 信号: 赤・状態不明で停止。**青を確認しても自動で進まない**(管理者の再開許可が必要)。
7. 速度上限 6km/h をコードの定数で固定。シミュレーションの倍速は時間の進み方であり速度上限は変えない。
8. 非常停止はいつでも可能。通信断・センサー異常は自動で停止。
9. 管理者操作は role=admin のみ。利用者は自分の予約だけ閲覧可。

## 6. 認証

- 名前 + 8文字以上のパスワード。**未登録の名前なら自動登録、登録済みならパスワード照合**。
- パスワードはハッシュ保存(werkzeug)。ログイン失敗の回数制限あり。管理者は環境変数 `ADMIN_NAME` / `ADMIN_PASSWORD`。
- Cookieは HttpOnly / SameSite=Lax / (Renderでは Secure)。POSTは `X-Requested-With` ヘッダ必須(CSRF対策)。

## 7. 実装順

1. ログイン・DB  2. 配送予約  3. 管理者画面  4. 地図  5. カメラ映像(模擬)  6. 遠隔開錠シミュレーション

## 8. 追加機能(第2版)
- 出発地・配送先は日本国内で自由に指定(検索 or 地図タップ)。直線30km以内。
- 台車はOSRMの道路経路に沿って走行。曲がり角は道の形から自動検出、信号はOpenStreetMapの信号機位置から停止ポイント化(いずれも再開は管理者の許可が必要)。
- 場所検索: ひらがな入力は簡易辞書(都道府県・主要都市)で漢字候補に展開して検索。
- 地図は国土地理院の航空写真(切替で地理院地図・OpenStreetMap)。
- 音声はブラウザ(端末)の日本語音声から男性の声を優先して選択。管理画面で変更可。

## 9. 配送後の自動帰還(第3版)
- 配送完了(管理者の「完了」、または受け取り時間30秒が過ぎて自動施錠)のあと、台車は自動で基地(`BASE_ADDRESS`)へ戻る(status: returning → completed)。
- 帰還中も配送中と同じ安全ルール: 常に施錠 / 曲がり角・信号・障害物で停止し管理者の再開許可が必要 / 非常停止 / 通信断・センサー異常で停止 / 6km/h上限。
- 基地は国土地理院の住所検索で座標を取得(`BASE_LAT`/`BASE_LON`で上書き可)。基地が不明、または配送先から30km超なら帰還せずその場で完了とし、履歴に理由を残す。
