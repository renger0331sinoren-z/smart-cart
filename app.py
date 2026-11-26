"""AI電動スマート台車 配送予約システム(シミュレーション)。"""
import json
import math
import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db
import geo
import hardware
import sim

IS_RENDER = os.environ.get("RENDER") == "true"
ADMIN_NAME = (os.environ.get("ADMIN_NAME") or "admin").strip()
DEPOT = (float(os.environ.get("DEPOT_LAT", "35.6812")), float(os.environ.get("DEPOT_LON", "139.7671")))
BASE_ADDRESS = os.environ.get("BASE_ADDRESS", "宮崎県宮崎市波島一丁目11番6号")  # 配送後に自動で戻る基地
MAX_TRIP_M = float(os.environ.get("MAX_TRIP_M", "30000"))  # 出発地と配送先の直線距離の上限(シミュレーションが長くなりすぎないように)
TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_H", "9"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# templates/ が無く、全ファイルが同じ階層に置かれている場合(GitHubのアップロードでフォルダが崩れた時)も動かす
FLAT = not os.path.isdir(os.path.join(BASE_DIR, "templates"))
STATIC_FILES = {"app.css", "admin.js", "common.js", "login.js", "user.js"}
app = Flask(__name__, template_folder=BASE_DIR if FLAT else "templates", static_folder=None)


@app.get("/static/<path:name>")
def static_files(name):
    if FLAT:
        base = os.path.basename(name)
        if base not in STATIC_FILES:
            abort(404)
        return send_from_directory(BASE_DIR, base)
    return send_from_directory(os.path.join(BASE_DIR, "static"), name)


secret =os.environ.get("SECRET_KEY")
if not secret:
    if IS_RENDER:
        raise RuntimeError("SECRET_KEY が未設定です")
    secret = secrets.token_hex(32)
app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_RENDER,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)
if IS_RENDER:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_base_cache = {}


def get_base():
    """基地の座標。BASE_LAT/BASE_LON があればそれを使い、無ければ国土地理院の住所検索で調べる。見つからなければ None。"""
    if "v" in _base_cache and (_base_cache["v"] or time.time() - _base_cache["t"] < 300):
        return _base_cache["v"]
    val = None
    if os.environ.get("BASE_LAT") and os.environ.get("BASE_LON"):
        val = (float(os.environ["BASE_LAT"]), float(os.environ["BASE_LON"]))
    else:
        import re as _re
        # 「一丁目11番6号」→「1-11-6」の形にして検索すると見つかりやすい
        kan = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
        q = BASE_ADDRESS
        for k, v in kan.items():
            q = q.replace(k + "丁目", v + "-")
        q = _re.sub(r"(\d+)番(\d+)号", r"\1-\2", q).replace("番", "-").replace("号", "")
        for query in (q, BASE_ADDRESS):
            try:
                res = geo._gsi(query)
            except geo.ServiceError:
                continue
            if res:
                val = (res[0]["lat"], res[0]["lon"])
                break
    _base_cache.update(v=val, t=time.time())
    return val


def set_cart_location(tx, pos):
    tx.execute("INSERT INTO cart_state(id,lat,lon,updated_at) VALUES (1,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, updated_at=excluded.updated_at",
               (pos[0], pos[1], iso_now()))


def get_cart_location():
    """台車の現在地。まだ一度も走っていなければ基地(BASE_ADDRESS)。"""
    with db.reader() as tx:
        r = tx.one("SELECT lat, lon FROM cart_state WHERE id=1")
    return (r["lat"], r["lon"]) if r else get_base()


def cart_label(pos):
    base = get_base()
    if base and sim.haversine(pos, base) <= 30:
        return BASE_ADDRESS + "(基地)"
    return "台車の現在地"


SIM_COLS = ["status", "hold_reason", "cart_lat", "cart_lon", "progress_m", "route", "locked",
            "unlock_expires_at", "obstacle", "comms_ok", "sensor_ok", "signal_state", "stop_idx", "stop_reached", "outcome"]
DUMMY_HASH = generate_password_hash("dummy-password-for-timing")
FAILS = {}  # (ip, name) -> [timestamps]  ※ワーカーごとの簡易制限


# ---------- 共通 ----------
def iso_now():
    return sim.iso(sim.now_utc())


def err(msg, code=400):
    return jsonify(error=msg), code


def log_event(tx, rid, actor, kind, severity="info", detail=""):
    tx.execute("INSERT INTO events(ts,reservation_id,actor,kind,severity,detail) VALUES (?,?,?,?,?,?)",
               (iso_now(), rid, actor, kind, severity, detail))


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db.reader() as tx:
        return tx.one("SELECT id,name,role FROM users WHERE id=?", (uid,))


def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if not u:
            session.clear()
            return err("ログインが必要です", 401)
        return fn(u, *a, **k)
    return w


def admin_required(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if not u:
            session.clear()
            return err("ログインが必要です", 401)
        if u["role"] != "admin":
            return err("管理者のみ利用できます", 403)
        return fn(u, *a, **k)
    return w


@app.before_request
def csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-Requested-With") != "fetch":
        return err("不正なリクエストです", 403)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://tile.openstreetmap.org https://cyberjapandata.gsi.go.jp https://unpkg.com; "
        "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
    if IS_RENDER:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/") or request.path in ("/app", "/admin", "/login"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- DB初期化・管理者 ----------
def ensure_admin():
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if len(pw) < 8:
        app.logger.warning("ADMIN_PASSWORD が未設定(8文字以上が必要)のため管理者アカウントは作成されません")
        return
    with db.transaction() as tx:
        u = tx.one("SELECT * FROM users WHERE name=?", (ADMIN_NAME,))
        if not u:
            tx.insert("INSERT INTO users(name,password_hash,role,created_at) VALUES (?,?,?,?)",
                      (ADMIN_NAME, generate_password_hash(pw), "admin", iso_now()))
        elif u["role"] != "admin" or not check_password_hash(u["password_hash"], pw):
            tx.execute("UPDATE users SET role='admin', password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))


db.init_db()
ensure_admin()
with db.transaction() as _tx:
    _tx.execute("UPDATE reservations SET origin_lat=?, origin_lon=?, origin_address='出発地(旧データ)' WHERE origin_lat IS NULL",
                (DEPOT[0], DEPOT[1]))


# ---------- ページ ----------
@app.get("/")
def index():
    u = current_user()
    if not u:
        return redirect("/login")
    return redirect("/admin" if u["role"] == "admin" else "/app")


@app.get("/login")
def login_page():
    return render_template("login.html")


@app.get("/app")
def user_page():
    u = current_user()
    if not u:
        return redirect("/login")
    if u["role"] == "admin":
        return redirect("/admin")
    return render_template("user.html", user=u, depot=DEPOT)


@app.get("/admin")
def admin_page():
    u = current_user()
    if not u:
        return redirect("/login")
    if u["role"] != "admin":
        return redirect("/app")
    return render_template("admin.html", user=u, depot=DEPOT,
                           max_speed=sim.MAX_SPEED_KMH, hw=hardware.REAL_HARDWARE_ENABLED)


# ---------- 認証API ----------
def norm_name(v):
    if not isinstance(v, str):
        return ""
    v = " ".join(v.split())
    if not v or len(v) > 30 or any(ord(c) < 32 for c in v):
        return ""
    return v


def throttled(key):
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]
    return len(FAILS[key]) >= 10


@app.post("/api/login")
def api_login():
    d = request.get_json(silent=True) or {}
    name, pw = norm_name(d.get("name")), d.get("password")
    if not name:
        return err("名前は1〜30文字で入力してください")
    if not isinstance(pw, str) or not (8 <= len(pw) <= 128):
        return err("パスワードは8文字以上128文字以内で入力してください")
    key = (request.remote_addr, name.lower())
    if throttled(key):
        return err("試行回数が多すぎます。しばらくしてからお試しください", 429)
    bad = lambda: (FAILS.setdefault(key, []).append(time.time()), err("名前またはパスワードが違います", 401))[1]

    with db.transaction() as tx:
        u = tx.one("SELECT * FROM users WHERE name=?", (name,))
        if u:
            ok = check_password_hash(u["password_hash"], pw)
            if not ok:
                log_event(tx, None, "anonymous", "login_failed", "warning", f"名前: {name}")
                resp = bad()
                return resp
        else:
            check_password_hash(DUMMY_HASH, pw)
            if name.lower() == ADMIN_NAME.lower() or tx.one("SELECT id FROM users WHERE lower(name)=lower(?)", (name,)):
                log_event(tx, None, "anonymous", "login_failed", "warning", f"使用不可の名前: {name}")
                return err("この名前は使用できません。別の名前を入力してください", 409)
            uid = tx.insert("INSERT INTO users(name,password_hash,role,created_at) VALUES (?,?,?,?)",
                            (name, generate_password_hash(pw), "user", iso_now()))
            u = {"id": uid, "name": name, "role": "user"}
            log_event(tx, None, f"user:{name}", "user_registered", "info", "新規登録")
        log_event(tx, None, f"{u['role']}:{name}", "login", "info", "")
    session.clear()
    session["uid"] = u["id"]
    session.permanent = True
    return jsonify(ok=True, role=u["role"], redirect="/admin" if u["role"] == "admin" else "/app")


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


# ---------- 状態更新の共通処理 ----------
def mutate(rid, fn, actor):
    """予約を排他ロックして fn(res) -> (新状態, イベント) を適用し、履歴も同一トランザクションで保存。"""
    err_obj = None
    new = None
    with db.transaction() as tx:
        res = tx.one("SELECT * FROM reservations WHERE id=?", (rid,), lock=True)
        if not res:
            return None, LookupError("予約が見つかりません")
        try:
            new, events = fn(res)
            sets = ",".join(f"{c}=?" for c in SIM_COLS)
            tx.execute(f"UPDATE reservations SET {sets}, updated_at=? WHERE id=?",
                       [new[c] for c in SIM_COLS] + [iso_now(), rid])
            if new["cart_lat"] is not None and new["status"] not in ("reserved", "cancelled") and \
                    (new["cart_lat"], new["cart_lon"]) != (res["cart_lat"], res["cart_lon"]):
                set_cart_location(tx, (new["cart_lat"], new["cart_lon"]))  # 台車が動いたら現在地を記憶
        except sim.ActionError as e:
            events, err_obj = e.events, e
        for kind, sev, detail in events:
            log_event(tx, rid, actor, kind, sev, detail)
    return new, err_obj


def settle_unlocked():
    """開錠中の予約の有効期限・異常を反映(期限切れは施錠)。"""
    with db.reader() as tx:
        ids = [r["id"] for r in tx.query("SELECT id FROM reservations WHERE locked=0")]
    for rid in ids:
        mutate(rid, lambda r: sim.apply_tick(r, 0, sim.now_utc()), "system")


# ---------- 表示用 ----------
def user_view(r):
    live = r["status"] in sim.ACTIVE
    remaining = None
    if r["locked"] == 0 and r["unlock_expires_at"]:
        remaining = max(0, int((sim.parse_iso(r["unlock_expires_at"]) - sim.now_utc()).total_seconds()))
    return {
        "id": r["id"], "scheduled_at": r["scheduled_at"], "address": r["address"], "lat": r["lat"], "lon": r["lon"],
        "origin_address": r.get("origin_address"), "origin_lat": r.get("origin_lat"), "origin_lon": r.get("origin_lon"),
        "item_desc": r["item_desc"], "forgotten_items": r["forgotten_items"], "notes": r["notes"],
        "status": r["status"], "status_label": sim.label(r["status"], r.get("outcome")), "outcome": r.get("outcome"),
        "paused": bool(r["hold_reason"]) and r["status"] == "en_route",
        "locked": bool(r["locked"]), "unlock_remaining_s": remaining,
        "cart_lat": r["cart_lat"] if live else None, "cart_lon": r["cart_lon"] if live else None,
        "created_at": r["created_at"],
    }


def admin_view(r, with_route=False):
    plan = r["route"] or r.get("route_plan")
    route = json.loads(plan) if plan else None
    v = user_view(r)
    v.update({
        "user_name": r.get("user_name"), "user_id": r["user_id"],
        "cart_lat": r["cart_lat"], "cart_lon": r["cart_lon"],
        "hold_reason": r["hold_reason"], "hold_label": sim.HOLD_LABEL.get(r["hold_reason"]),
        "obstacle": bool(r["obstacle"]), "comms_ok": bool(r["comms_ok"]), "sensor_ok": bool(r["sensor_ok"]),
        "signal_state": r["signal_state"], "progress_m": round(r["progress_m"], 1),
        "total_m": round(route["total"], 1) if route else None,
        "route_source": route.get("source") if route else None,
        "stop_idx": r["stop_idx"], "stop_count": len(route["stops"]) if route else 0,
        "route_points": route["points"] if (route and with_route) else None,  # 点が多いので選択中の予約だけ送る
        "stops": route["stops"] if (route and with_route) else [],
    })
    return v


# ---------- 利用者API ----------
def _point(d, klat, klon, label):
    try:
        lat, lon = float(d.get(klat)), float(d.get(klon))
    except (TypeError, ValueError):
        raise ValueError(f"{label}を地図または検索で選択してください")
    if not (math.isfinite(lat) and math.isfinite(lon) and 20 <= lat <= 46 and 122 <= lon <= 154):
        raise ValueError(f"{label}は日本国内で選択してください")
    return lat, lon


def parse_reservation(d):
    def text(k, label, maxlen, required=False):
        v = d.get(k, "")
        v = v.strip() if isinstance(v, str) else ""
        if required and not v:
            raise ValueError(f"{label}を入力してください")
        if len(v) > maxlen:
            raise ValueError(f"{label}が長すぎます")
        return v
    try:
        sched = d.get("scheduled_at", "")
        t = datetime.strptime(sched, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        raise ValueError("希望日時を正しく入力してください")
    now_local = sim.now_utc().replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_H)
    if t < now_local - timedelta(minutes=5) or t > now_local + timedelta(days=60):
        raise ValueError("希望日時は現在から60日以内で指定してください")
    lat, lon = _point(d, "lat", "lon", "配送先")
    out = {"scheduled_at": sched, "lat": lat, "lon": lon}
    out["address"] = text("address", "配送先の場所", 200, True)
    out["item_desc"] = text("item_desc", "荷物の内容", 500, True)
    out["forgotten_items"] = text("forgotten_items", "忘れ物の詳細", 500)
    out["notes"] = text("notes", "注意事項", 500)
    return out


def make_plan(origin, dest):
    """道路に沿った走行プランを作る。 戻り値: (plan_json, メモ)。道路が見つからなければ ValueError。
    外部サービスに接続できないときは (None, メモ) を返し、出発時にもう一度試す。"""
    try:
        pts, _dist, snap = geo.road_route(origin, dest)
    except geo.NoRoute:
        raise ValueError("出発地と配送先を結ぶ道が見つかりません。道路の近くの場所を選んでください")
    except geo.ServiceError:
        return None, "経路サービスに接続できなかったため、出発時に再度取得します"
    if snap > 300:
        raise ValueError("選んだ場所が道路から遠すぎます(300m以上)。道路の近くを選んでください")
    try:
        signals = geo.traffic_signals(pts)
        note = f"信号機{len(signals)}か所を地図データから取得"
    except geo.ServiceError:
        signals, note = [], "信号機の位置を取得できなかったため、曲がり角のみで停止します"
    plan = sim.plan_from_geometry(pts, signals)
    plan["signals_known"] = bool(signals) or note.startswith("信号機")
    return json.dumps(plan), note


@app.post("/api/reservations")
@login_required
def create_reservation(u):
    if u["role"] == "admin":
        return err("管理者アカウントでは予約できません", 403)
    try:
        d = parse_reservation(request.get_json(silent=True) or {})
        origin = get_cart_location()  # 出発地は選ばせず、台車がいま(最後に)いる場所から出発する
        if not origin:
            raise ValueError("台車の現在地(基地)を調べられませんでした。管理者にお問い合わせください")
        dist = sim.haversine(origin, (d["lat"], d["lon"]))
        if dist < 30:
            raise ValueError("台車の現在地に近すぎます(30m以上離れた場所を選んでください)")
        if dist > MAX_TRIP_M:
            raise ValueError(f"台車の現在地から遠すぎます(直線で{int(MAX_TRIP_M / 1000)}km以内)")
        plan, note = make_plan(origin, (d["lat"], d["lon"]))
    except ValueError as e:
        return err(str(e))
    with db.transaction() as tx:
        n = tx.one("SELECT COUNT(*) AS n FROM reservations WHERE user_id=? AND status IN "
                   "('reserved','en_route','arrived','unlock_pending','unlocked','denied','returning')", (u["id"],))["n"]
        if n >= 10:
            return err("進行中の予約が多すぎます(最大10件)")
        now = iso_now()
        label = cart_label(origin)
        rid = tx.insert(
            "INSERT INTO reservations(user_id,scheduled_at,address,lat,lon,item_desc,forgotten_items,notes,"
            "origin_address,origin_lat,origin_lon,route_plan,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (u["id"], d["scheduled_at"], d["address"], d["lat"], d["lon"], d["item_desc"],
             d["forgotten_items"], d["notes"], label, origin[0], origin[1], plan, now, now))
        log_event(tx, rid, f"user:{u['name']}", "reservation_created", "notice",
                  f"{d['scheduled_at']} / {label} → {d['address']} / {note}")
    return jsonify(ok=True, id=rid), 201


@app.get("/api/cart")
@login_required
def api_cart(u):
    pos = get_cart_location()
    if not pos:
        return err("台車の現在地を調べられません", 503)
    base = get_base()
    return jsonify(lat=pos[0], lon=pos[1], label=cart_label(pos), at_base=bool(base and sim.haversine(pos, base) <= 30))


@app.get("/api/reservations")
@login_required
def list_my_reservations(u):
    settle_unlocked()
    with db.reader() as tx:
        rows = tx.query("SELECT * FROM reservations WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],))
    return jsonify(reservations=[user_view(r) for r in rows])


@app.post("/api/reservations/<int:rid>/cancel")
@login_required
def cancel_reservation(u, rid):
    with db.reader() as tx:
        r = tx.one("SELECT user_id FROM reservations WHERE id=?", (rid,))
    if not r or r["user_id"] != u["id"]:  # 他人の予約は存在しないものとして扱う
        return err("予約が見つかりません", 404)
    _, e = mutate(rid, lambda res: sim.apply_cancel(res, sim.now_utc()), f"user:{u['name']}")
    if e:
        return err(str(e), 409)
    return jsonify(ok=True)


# ---------- 場所検索 ----------
SEARCH_LOG = {}  # uid -> [timestamps]


@app.get("/api/search")
@login_required
def api_search(u):
    q = " ".join((request.args.get("q") or "").split())
    if len(q) < 2 or len(q) > 60:
        return jsonify(results=[])
    now = time.time()
    hist = [t for t in SEARCH_LOG.get(u["id"], []) if now - t < 60]
    if len(hist) >= 40:
        return err("検索の回数が多すぎます。少し待ってください", 429)
    SEARCH_LOG[u["id"]] = hist + [now]
    try:
        return jsonify(results=geo.search_places(q))
    except geo.ServiceError:
        return err("検索サービスに接続できませんでした。地図をタップして選ぶこともできます", 503)


# ---------- 管理者API ----------
def overview(after, sel=0):
    with db.reader() as tx:
        rows = tx.query("SELECT r.*, u.name AS user_name FROM reservations r JOIN users u ON u.id=r.user_id "
                        "ORDER BY r.id DESC LIMIT 200")
        if after > 0:
            evs = tx.query("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 300", (after,))
        else:
            evs = list(reversed(tx.query("SELECT * FROM events ORDER BY id DESC LIMIT 200")))
    return {"reservations": [admin_view(r, r["id"] == sel) for r in rows], "events": evs}


@app.post("/api/admin/tick")
@admin_required
def admin_tick(u):
    """1秒ごとに管理者画面が呼ぶ。run=true の間だけ台車を進める(閉じれば台車は動かない)。"""
    d = request.get_json(silent=True) or {}
    try:
        dt = float(d.get("dt", 0))
        after = int(d.get("after", 0))
        sel = int(d.get("sel") or 0)
    except (TypeError, ValueError):
        return err("不正な値です")
    dt = max(0.0, min(dt, sim.MAX_DT_S)) if d.get("run") else 0.0
    with db.reader() as tx:
        ids = [r["id"] for r in tx.query("SELECT id FROM reservations WHERE status IN ('en_route','arrived','unlock_pending','unlocked','returning')")]
    for rid in ids:
        mutate(rid, lambda r: sim.apply_tick(r, dt, sim.now_utc()), "system")
    out = overview(after, sel)
    out["max_speed_kmh"] = sim.MAX_SPEED_KMH
    return jsonify(out)


def _a_dispatch(r, b, u): return sim.apply_dispatch(r, sim.now_utc())
def _a_resume(r, b, u): return sim.apply_resume(r, b.get("confirmed") is True, sim.now_utc())
def _a_estop(r, b, u): return sim.apply_estop(r, sim.now_utc())
def _a_sim(r, b, u): return sim.apply_sim(r, {k: v for k, v in b.items()}, sim.now_utc())
def _a_face(r, b, u): return sim.apply_face_detected(r, sim.now_utc())
def _a_unlock(r, b, u): return sim.apply_unlock(r, b.get("decision"), sim.now_utc())
def _a_complete(r, b, u): return sim.apply_complete(r, sim.now_utc())


ADMIN_ACTIONS = {"dispatch": _a_dispatch, "resume": _a_resume, "estop": _a_estop, "sim": _a_sim,
                 "face-detected": _a_face, "unlock": _a_unlock, "complete": _a_complete}


def ensure_return_plan(rid):
    """配送後に基地へ戻る経路を用意する(配送先→基地)。基地が不明・遠すぎる場合は作らない(その場で完了)。"""
    with db.reader() as tx:
        r = tx.one("SELECT status, return_plan, lat, lon FROM reservations WHERE id=?", (rid,))
    if not r or r["return_plan"] or r["status"] not in ("arrived", "unlock_pending", "unlocked", "denied"):
        return None
    base = get_base()
    if not base:
        note = "基地の位置を調べられなかったため、帰還は行いません(BASE_LAT / BASE_LON を設定してください)"
    elif sim.haversine((r["lat"], r["lon"]), base) > MAX_TRIP_M:
        note = f"基地まで{int(MAX_TRIP_M / 1000)}km以上離れているため、帰還は行いません(シミュレーション)"
    elif sim.haversine((r["lat"], r["lon"]), base) < 20:
        note = "すでに基地の位置にいるため、帰還は行いません"
    else:
        try:
            plan, _ = make_plan((r["lat"], r["lon"]), base)
        except ValueError:
            plan = None
        if not plan:  # 経路サービスに繋がらない・道が見つからない → 簡易経路
            plan = json.dumps(sim.build_route((r["lat"], r["lon"]), base))
        with db.transaction() as tx:
            tx.execute("UPDATE reservations SET return_plan=? WHERE id=? AND return_plan IS NULL", (plan, rid))
        return None
    with db.transaction() as tx:
        if not tx.one("SELECT id FROM events WHERE reservation_id=? AND kind='return_skipped'", (rid,)):
            log_event(tx, rid, "system", "return_skipped", "notice", note)
    return note


def ensure_plan(rid):
    """出発の直前に、台車の現在地(さっきいた場所)から配送先までの道路経路を作り直す。"""
    with db.reader() as tx:
        r = tx.one("SELECT status, route_plan, origin_lat, origin_lon, lat, lon FROM reservations WHERE id=?", (rid,))
    if not r or r["status"] != "reserved":
        return
    cur = get_cart_location()
    if not cur:
        return
    moved = r["origin_lat"] is None or sim.haversine((r["origin_lat"], r["origin_lon"]), cur) > 30
    if r["route_plan"] and not moved:
        return
    try:
        plan, _ = make_plan(cur, (r["lat"], r["lon"]))
    except ValueError:
        plan = None  # 道が見つからない等 → 出発時に簡易経路になる
    with db.transaction() as tx:
        tx.execute("UPDATE reservations SET origin_lat=?, origin_lon=?, origin_address=?, route_plan=? WHERE id=? AND status='reserved'",
                   (cur[0], cur[1], cart_label(cur), plan, rid))


@app.post("/api/admin/cart-reset")
@admin_required
def cart_reset(u):
    base = get_base()
    if not base:
        return err("基地の位置を調べられません(BASE_LAT / BASE_LON を設定してください)", 409)
    with db.transaction() as tx:
        if tx.one("SELECT id FROM reservations WHERE status IN ('en_route','returning')"):
            return err("走行中の台車があるため、位置をリセットできません", 409)
        set_cart_location(tx, base)
        log_event(tx, None, f"admin:{u['name']}", "cart_reset", "notice", "台車の現在地を基地にリセットしました")
    return jsonify(ok=True)


@app.post("/api/admin/reservations/<int:rid>/<action>")
@admin_required
def admin_action(u, rid, action):
    fn = ADMIN_ACTIONS.get(action)
    if not fn:
        return err("不明な操作です", 404)
    body = request.get_json(silent=True) or {}
    if action == "dispatch":
        ensure_plan(rid)
    elif action in ("complete", "unlock", "face-detected"):
        ensure_return_plan(rid)  # 完了(または受け取り終了)のあと、自動で基地へ戻れるよう先に経路を用意する
    new, e = mutate(rid, lambda r: fn(r, body, u), f"admin:{u['name']}")
    if isinstance(e, LookupError):
        return err(str(e), 404)
    if e:
        return err(str(e), 409)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", "5000")))
