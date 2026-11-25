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
import hardware
import sim

IS_RENDER = os.environ.get("RENDER") == "true"
ADMIN_NAME = (os.environ.get("ADMIN_NAME") or "admin").strip()
DEPOT = (float(os.environ.get("DEPOT_LAT", "35.6812")), float(os.environ.get("DEPOT_LON", "139.7671")))
SERVICE_RADIUS_M = float(os.environ.get("SERVICE_RADIUS_M", "3000"))
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

SIM_COLS = ["status", "hold_reason", "cart_lat", "cart_lon", "progress_m", "route", "locked",
            "unlock_expires_at", "obstacle", "comms_ok", "sensor_ok", "signal_state"]
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
        "img-src 'self' data: https://tile.openstreetmap.org https://unpkg.com; "
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
    return render_template("user.html", user=u, depot=DEPOT, radius=SERVICE_RADIUS_M)


@app.get("/admin")
def admin_page():
    u = current_user()
    if not u:
        return redirect("/login")
    if u["role"] != "admin":
        return redirect("/app")
    return render_template("admin.html", user=u, depot=DEPOT, radius=SERVICE_RADIUS_M,
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
        "item_desc": r["item_desc"], "forgotten_items": r["forgotten_items"], "notes": r["notes"],
        "status": r["status"], "status_label": sim.STATUS_LABEL.get(r["status"], r["status"]),
        "paused": bool(r["hold_reason"]) and r["status"] == "en_route",
        "locked": bool(r["locked"]), "unlock_remaining_s": remaining,
        "cart_lat": r["cart_lat"] if live else None, "cart_lon": r["cart_lon"] if live else None,
        "created_at": r["created_at"],
    }


def admin_view(r):
    route = json.loads(r["route"]) if r["route"] else None
    v = user_view(r)
    v.update({
        "user_name": r.get("user_name"), "user_id": r["user_id"],
        "cart_lat": r["cart_lat"], "cart_lon": r["cart_lon"],
        "hold_reason": r["hold_reason"], "hold_label": sim.HOLD_LABEL.get(r["hold_reason"]),
        "obstacle": bool(r["obstacle"]), "comms_ok": bool(r["comms_ok"]), "sensor_ok": bool(r["sensor_ok"]),
        "signal_state": r["signal_state"], "progress_m": round(r["progress_m"], 1),
        "total_m": round(route["total"], 1) if route else None,
        "route_points": route["points"] if route else None,
        "stops": route["stops"] if route else [],
    })
    return v


# ---------- 利用者API ----------
def parse_reservation(d):
    def text(k, maxlen, required=False):
        v = d.get(k, "")
        v = v.strip() if isinstance(v, str) else ""
        if required and not v:
            raise ValueError(f"{k}")
        if len(v) > maxlen:
            raise ValueError(f"{k}が長すぎます")
        return v
    try:
        sched = d.get("scheduled_at", "")
        t = datetime.strptime(sched, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        raise ValueError("希望日時を正しく入力してください")
    now_local = sim.now_utc().replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_H)
    if t < now_local - timedelta(minutes=5) or t > now_local + timedelta(days=60):
        raise ValueError("希望日時は現在から60日以内で指定してください")
    try:
        lat, lon = float(d.get("lat")), float(d.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("地図をクリックして配送先を選択してください")
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("配送先の位置が不正です")
    dist = sim.haversine(DEPOT, (lat, lon))
    if dist > SERVICE_RADIUS_M:
        raise ValueError(f"配送エリア外です(出発地から{int(SERVICE_RADIUS_M)}m以内)")
    if dist < 30:
        raise ValueError("出発地に近すぎます(30m以上離れた場所を選んでください)")
    out = {"scheduled_at": sched, "lat": lat, "lon": lon}
    for k, mx, req in (("address", 200, True), ("item_desc", 500, True), ("forgotten_items", 500, False), ("notes", 500, False)):
        try:
            out[k] = text(k, mx, req)
        except ValueError as e:
            labels = {"address": "配送先の場所", "item_desc": "荷物の内容"}
            msg = str(e)
            raise ValueError(f"{labels.get(msg, msg)}を入力してください" if msg in labels else msg)
    return out


@app.post("/api/reservations")
@login_required
def create_reservation(u):
    if u["role"] == "admin":
        return err("管理者アカウントでは予約できません", 403)
    try:
        d = parse_reservation(request.get_json(silent=True) or {})
    except ValueError as e:
        return err(str(e))
    with db.transaction() as tx:
        n = tx.one("SELECT COUNT(*) AS n FROM reservations WHERE user_id=? AND status IN "
                   "('reserved','en_route','arrived','unlock_pending','unlocked','denied')", (u["id"],))["n"]
        if n >= 10:
            return err("進行中の予約が多すぎます(最大10件)")
        now = iso_now()
        rid = tx.insert(
            "INSERT INTO reservations(user_id,scheduled_at,address,lat,lon,item_desc,forgotten_items,notes,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (u["id"], d["scheduled_at"], d["address"], d["lat"], d["lon"], d["item_desc"],
             d["forgotten_items"], d["notes"], now, now))
        log_event(tx, rid, f"user:{u['name']}", "reservation_created", "notice",
                  f"{d['scheduled_at']} / {d['address']}")
    return jsonify(ok=True, id=rid), 201


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


# ---------- 管理者API ----------
def overview(after):
    with db.reader() as tx:
        rows = tx.query("SELECT r.*, u.name AS user_name FROM reservations r JOIN users u ON u.id=r.user_id "
                        "ORDER BY r.id DESC LIMIT 200")
        if after > 0:
            evs = tx.query("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 300", (after,))
        else:
            evs = list(reversed(tx.query("SELECT * FROM events ORDER BY id DESC LIMIT 200")))
    return {"reservations": [admin_view(r) for r in rows], "events": evs}


@app.post("/api/admin/tick")
@admin_required
def admin_tick(u):
    """1秒ごとに管理者画面が呼ぶ。run=true の間だけ台車を進める(閉じれば台車は動かない)。"""
    d = request.get_json(silent=True) or {}
    try:
        dt = float(d.get("dt", 0))
        after = int(d.get("after", 0))
    except (TypeError, ValueError):
        return err("不正な値です")
    dt = max(0.0, min(dt, sim.MAX_DT_S)) if d.get("run") else 0.0
    with db.reader() as tx:
        ids = [r["id"] for r in tx.query("SELECT id FROM reservations WHERE status IN ('en_route','arrived','unlock_pending','unlocked')")]
    for rid in ids:
        mutate(rid, lambda r: sim.apply_tick(r, dt, sim.now_utc()), "system")
    out = overview(after)
    out["max_speed_kmh"] = sim.MAX_SPEED_KMH
    return jsonify(out)


def _a_dispatch(r, b, u): return sim.apply_dispatch(r, DEPOT, sim.now_utc())
def _a_resume(r, b, u): return sim.apply_resume(r, b.get("confirmed") is True, sim.now_utc())
def _a_estop(r, b, u): return sim.apply_estop(r, sim.now_utc())
def _a_sim(r, b, u): return sim.apply_sim(r, {k: v for k, v in b.items()}, sim.now_utc())
def _a_face(r, b, u): return sim.apply_face_detected(r, sim.now_utc())
def _a_unlock(r, b, u): return sim.apply_unlock(r, b.get("decision"), sim.now_utc())
def _a_complete(r, b, u): return sim.apply_complete(r, sim.now_utc())


ADMIN_ACTIONS = {"dispatch": _a_dispatch, "resume": _a_resume, "estop": _a_estop, "sim": _a_sim,
                 "face-detected": _a_face, "unlock": _a_unlock, "complete": _a_complete}


@app.post("/api/admin/reservations/<int:rid>/<action>")
@admin_required
def admin_action(u, rid, action):
    fn = ADMIN_ACTIONS.get(action)
    if not fn:
        return err("不明な操作です", 404)
    body = request.get_json(silent=True) or {}
    new, e = mutate(rid, lambda r: fn(r, body, u), f"admin:{u['name']}")
    if isinstance(e, LookupError):
        return err(str(e), 404)
    if e:
        return err(str(e), 409)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", "5000")))
