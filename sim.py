"""配送台車シミュレーション: 状態遷移と安全ルール(DBや画面に依存しない純粋関数)。

各 apply_* は (新しい状態dict, イベントlist) を返す。拒否する操作は ActionError を送出する。
イベントは (kind, severity, detail)。
経路(route)は {"points":[[lat,lon]..], "total":m, "stops":[{"dist":m,"kind":"corner|signal"}..], "source":"road|fallback"}。
停止ポイントは順番に通過し、通過済みの数を stop_idx、いま停止ポイントで止まっているかを stop_reached で持つ。
"""
import bisect
import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache

MAX_SPEED_KMH = 6.0  # 希望最高速度(上限として固定)
MAX_SPEED_MS = MAX_SPEED_KMH * 1000 / 3600
UNLOCK_WINDOW_S = 30
MAX_DT_S = 60.0
ACTIVE = ("en_route", "arrived", "unlock_pending", "unlocked", "returning")
MOVING = ("en_route", "returning")  # 走行中(配送中・基地へ帰還中)。どちらも同じ安全ルール
FINAL = ("completed", "cancelled")
STOP_HOLDS = ("corner", "signal_red", "signal_unknown", "signal_green")

STATUS_LABEL = {
    "reserved": "予約受付",
    "en_route": "配送中",
    "arrived": "到着(本人確認待ち)",
    "unlock_pending": "開錠判断待ち",
    "unlocked": "開錠中",
    "returning": "配送完了・基地へ帰還中",
    "completed": "配送完了",
    "denied": "開錠見送り",
    "cancelled": "キャンセル",
}
HOLD_LABEL = {
    "obstacle": "障害物を検知して停止",
    "corner": "曲がり角で停止",
    "signal_red": "赤信号で停止",
    "signal_unknown": "信号の状態が確認できず停止",
    "signal_green": "青信号を確認(再開許可待ち)",
    "comms_lost": "通信断のため停止",
    "sensor_fault": "センサー異常のため停止",
    "estop": "非常停止",
}
VOICE_PROMPT = "カメラの前に顔を近づけてください。配送の確認を行います"


class ActionError(Exception):
    def __init__(self, msg, events=None):
        super().__init__(msg)
        self.events = events or []


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------- 地理・経路 ----------
def haversine(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _bearing(a, b):
    la1, la2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def _cumulative(points):
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return cum


def point_at(points, cum, d):
    d = max(0.0, min(d, cum[-1]))
    i = max(0, min(bisect.bisect_right(cum, d) - 1, len(points) - 2))
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg <= 0 else (d - cum[i]) / seg
    a, b = points[i], points[i + 1]
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def pos_at(route, dist):
    return point_at(route["points"], route["cum"], dist)


@lru_cache(maxsize=64)
def load_route(route_json):
    r = json.loads(route_json)
    if len(r["points"]) == 1:
        r["points"].append(list(r["points"][0]))
    r["cum"] = _cumulative(r["points"])
    r["total"] = r["cum"][-1]
    return r


def build_route(origin, dest):
    """道路経路を取得できないときの簡易経路(L字の直線)。曲がり角と擬似信号を置く。"""
    corner = (dest[0], origin[1])
    leg1, leg2 = haversine(origin, corner), haversine(corner, dest)
    pts = [origin, corner, dest] if leg1 >= 5 and leg2 >= 5 else [origin, dest]
    total = sum(haversine(a, b) for a, b in zip(pts, pts[1:]))
    stops = []
    if len(pts) == 3:
        stops.append({"dist": leg1, "kind": "corner"})
    if total > 20:
        stops.append({"dist": total * 0.85, "kind": "signal"})
    stops.sort(key=lambda x: x["dist"])
    cum = _cumulative(pts)
    for st in stops:
        st["lat"], st["lon"] = point_at(pts, cum, st["dist"])
    return {"points": [list(p) for p in pts], "total": total, "stops": stops, "source": "fallback"}


def _corners(pts, cum, total, span=12.0, min_turn=50.0):
    """道の形から曲がり角を探す(前後12m先の進行方向が50度以上変わる所)。"""
    cands = []
    for i in range(1, len(pts) - 1):
        d = cum[i]
        if d < span * 0.6 or total - d < span * 0.6:
            continue
        a = point_at(pts, cum, d - span)
        b = point_at(pts, cum, d + span)
        turn = abs((_bearing(pts[i], b) - _bearing(a, pts[i]) + 540) % 360 - 180)
        if turn >= min_turn:
            cands.append((d, turn))
    out, group = [], []
    for c in cands:
        if group and c[0] - group[-1][0] > 25:
            out.append(max(group, key=lambda x: x[1])[0])
            group = []
        group.append(c)
    if group:
        out.append(max(group, key=lambda x: x[1])[0])
    return out


def _snap_signals(pts, cum, signals, max_m=20.0):
    """信号機の座標を経路上の距離に変換(経路から20m以内のものだけ)。"""
    if not signals:
        return []
    lat0, lon0 = pts[0]
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110540
    P = [((p[1] - lon0) * kx, (p[0] - lat0) * ky) for p in pts]
    total = cum[-1]
    dists = []
    for s in signals:
        sx, sy = (s[1] - lon0) * kx, (s[0] - lat0) * ky
        best = None
        for i in range(len(P) - 1):
            ax, ay = P[i]
            bx, by = P[i + 1]
            if sx < min(ax, bx) - max_m or sx > max(ax, bx) + max_m or sy < min(ay, by) - max_m or sy > max(ay, by) + max_m:
                continue
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((sx - ax) * dx + (sy - ay) * dy) / l2))
            dd = math.hypot(sx - (ax + t * dx), sy - (ay + t * dy))
            if best is None or dd < best[0]:
                best = (dd, cum[i] + t * math.sqrt(l2))
        if best and best[0] <= max_m and 8 < best[1] < total - 8:
            dists.append(best[1])
    dists.sort()
    out = []
    for d in dists:  # 同じ交差点の重複(横断歩道と信号など)をまとめる
        if not out or d - out[-1] > 15:
            out.append(d)
    return out


def plan_from_geometry(points, signals):
    """道路の形(座標列)と信号機の位置から、走行プラン(曲がり角・信号の停止ポイント付き)を作る。"""
    pts = []
    for p in points:
        q = (round(p[0], 6), round(p[1], 6))
        if not pts or haversine(pts[-1], q) >= 2.0:
            pts.append(q)
    end = (round(points[-1][0], 6), round(points[-1][1], 6))
    if pts[-1] != end:
        pts.append(end)
    if len(pts) < 2:
        raise ValueError("経路が短すぎます")
    cum = _cumulative(pts)
    total = cum[-1]
    stops = [{"dist": round(d, 1), "kind": "corner"} for d in _corners(pts, cum, total)]
    stops += [{"dist": round(d, 1), "kind": "signal"} for d in _snap_signals(pts, cum, signals)]
    stops.sort(key=lambda x: x["dist"])
    for st in stops:
        st["lat"], st["lon"] = (round(v, 6) for v in point_at(pts, cum, st["dist"]))
    return {"points": [list(p) for p in pts], "total": total, "stops": stops, "source": "road"}


# ---------- 安全ルール ----------
def fault_cause(s):
    if not s["comms_ok"]:
        return "comms_lost"
    if not s["sensor_ok"]:
        return "sensor_fault"
    if s["obstacle"]:
        return "obstacle"
    return None


def _signal_hold(signal_state):
    # 青でも自動では進まない: 「青を確認・再開許可待ち」という停止として扱う
    return {"red": "signal_red", "green": "signal_green"}.get(signal_state, "signal_unknown")


def _stop_hold(stop, s):
    return "corner" if stop["kind"] == "corner" else _signal_hold(s["signal_state"])


def _relock(s, ev, detail, sev="warning"):
    s["locked"] = 1
    s["unlock_expires_at"] = None
    if s["status"] == "unlocked":
        s["status"] = "unlock_pending"
    ev.append(("relock", sev, detail))


def _enforce_lock(s, now, ev):
    """施錠の強制。開錠時間が正常に終わって施錠したときだけ True を返す(受け取り完了の合図)。"""
    if s["locked"] == 0:
        exp = s["unlock_expires_at"]
        if s["status"] != "unlocked":
            _relock(s, ev, "状態不整合のため施錠")
        elif not exp or now >= parse_iso(exp):
            _relock(s, ev, "開錠時間が終了したため自動施錠", "notice")
            return True
        elif not s["comms_ok"] or not s["sensor_ok"]:
            _relock(s, ev, "通信またはセンサーの異常を検知したため即時施錠", "critical")
    return False


def _finish_delivery(s, ev):
    """配送完了。基地への帰還プランがあれば、自動で帰還走行に入る(安全ルールは配送中と同じ)。"""
    s.update(locked=1, unlock_expires_at=None, hold_reason=None)
    ev.append(("completed", "notice", "配送を完了しました"))
    plan = s.get("return_plan")
    if not plan:
        s["status"] = "completed"
        return
    route = load_route(plan)
    s.update(status="returning", route=plan, progress_m=0.0, stop_idx=0, stop_reached=0,
             cart_lat=route["points"][0][0], cart_lon=route["points"][0][1])
    src = "道路に沿った経路" if route.get("source") == "road" else "簡易経路(直線)"
    ev.append(("return_start", "notice",
               f"基地への帰還を自動で開始(シミュレーション): {src}、{route['total']:.0f}m、停止ポイント{len(route['stops'])}か所。曲がり角・信号・障害物では停止し、管理者の再開許可が必要です"))


def apply_tick(res, dt, now):
    s, ev = dict(res), []
    dt = max(0.0, min(float(dt), MAX_DT_S))
    received = _enforce_lock(s, now, ev)
    if received and s.get("return_plan"):  # 受け取り時間が終わって施錠 → 配送完了 → 自動で帰還
        _finish_delivery(s, ev)
        return s, ev
    if s["status"] not in MOVING:
        return s, ev

    cause = fault_cause(s)
    if cause and not s["hold_reason"]:
        s["hold_reason"] = cause
        ev.append(("safety_stop", "critical", "停止: " + HOLD_LABEL[cause]))
    if s["hold_reason"] and s["hold_reason"].startswith("signal_"):
        s["hold_reason"] = _signal_hold(s["signal_state"])  # 表示を最新の信号に合わせる(再開はしない)
    if s["hold_reason"]:
        return s, ev

    route = load_route(s["route"])
    stops, idx = route["stops"], s["stop_idx"]
    nxt = stops[idx] if idx < len(stops) else None
    if nxt and s["stop_reached"]:  # 通常は到達しないが、念のため停止を維持
        s["hold_reason"] = _stop_hold(nxt, s)
        return s, ev

    limit = nxt["dist"] if nxt else route["total"]
    dist = min(s["progress_m"] + MAX_SPEED_MS * dt, limit)
    s["progress_m"] = dist
    s["cart_lat"], s["cart_lon"] = pos_at(route, dist)

    if nxt and dist >= nxt["dist"]:
        s["stop_reached"] = 1
        reason = _stop_hold(nxt, s)
        s["hold_reason"] = reason
        kind = "corner_stop" if nxt["kind"] == "corner" else "signal_stop"
        ev.append((kind, "warning", "停止: " + HOLD_LABEL[reason] + "。映像と位置を確認して再開を許可してください"))
    elif not nxt and dist >= route["total"] and s["status"] == "returning":
        s["status"] = "completed"
        s["cart_lat"], s["cart_lon"] = route["points"][-1]
        ev.append(("returned", "notice", "基地に帰還しました(収納部は施錠中)"))
    elif not nxt and dist >= route["total"]:
        s["status"] = "arrived"
        s["cart_lat"], s["cart_lon"] = route["points"][-1]
        ev.append(("arrived", "warning", "配送先に到着しました(収納部は施錠中)"))
        ev.append(("voice_prompt", "info", VOICE_PROMPT))
    return s, ev


def apply_dispatch(res, now):
    s, ev = dict(res), []
    if s["status"] != "reserved":
        raise ActionError("この予約は出発できる状態ではありません")
    cause = fault_cause(s)
    if cause:
        raise ActionError("出発できません: " + HOLD_LABEL[cause])
    if s.get("origin_lat") is None:
        raise ActionError("出発地が設定されていない予約です")
    route_json = s.get("route_plan") or json.dumps(
        build_route((s["origin_lat"], s["origin_lon"]), (s["lat"], s["lon"])))
    route = load_route(route_json)
    s.update(route=route_json, progress_m=0.0, status="en_route", hold_reason=None, stop_idx=0, stop_reached=0,
             locked=1, unlock_expires_at=None, cart_lat=route["points"][0][0], cart_lon=route["points"][0][1])
    src = "道路に沿った経路" if route.get("source") == "road" else "簡易経路(直線)"
    ev.append(("dispatch", "notice", f"配送を開始(シミュレーション): {src}、{route['total']:.0f}m、停止ポイント{len(route['stops'])}か所"))
    return s, ev


def apply_resume(res, confirmed, now):
    s, ev = dict(res), []
    if s["status"] not in MOVING or not s["hold_reason"]:
        raise ActionError("再開できる停止状態ではありません")
    if not confirmed:
        raise ActionError("安全確認のチェックが必要です")
    cause = fault_cause(s)
    if cause:
        raise ActionError("再開できません: " + HOLD_LABEL[cause] + "(解消を確認してください)")
    hold = s["hold_reason"]
    if hold.startswith("signal_") and s["signal_state"] != "green":
        raise ActionError("信号が青と確認できないため再開できません")
    stops, idx = load_route(s["route"])["stops"], s["stop_idx"]
    at_stop = bool(s["stop_reached"]) and idx < len(stops)
    if hold in STOP_HOLDS:
        if at_stop:
            s["stop_idx"], s["stop_reached"] = idx + 1, 0
        s["hold_reason"] = None
    elif at_stop:  # 非常停止などで止まっていても、曲がり角・信号の確認は別途必要
        s["hold_reason"] = _stop_hold(stops[idx], s)
    else:
        s["hold_reason"] = None
    ev.append(("resume", "notice", "管理者が安全を確認し走行再開を許可しました" if not s["hold_reason"]
               else "停止要因は解除。ただし " + HOLD_LABEL[s["hold_reason"]] + " の確認が必要です"))
    return s, ev


def apply_estop(res, now):
    s, ev = dict(res), []
    if s["status"] not in MOVING:
        raise ActionError("走行中ではありません")
    s["hold_reason"] = "estop"
    ev.append(("estop", "critical", "非常停止"))
    return s, ev


def apply_sim(res, changes, now):
    """センサー・通信・信号の模擬入力。"""
    s, ev = dict(res), []
    allowed = {"obstacle", "comms_ok", "sensor_ok", "signal_state"}
    if not changes or set(changes) - allowed:
        raise ActionError("不正な入力です")
    live = s["status"] in ACTIVE
    if "obstacle" in changes:
        v = 1 if changes["obstacle"] else 0
        if v != s["obstacle"]:
            s["obstacle"] = v
            if live:
                ev.append(("obstacle_detected", "critical", "距離センサーが人・車・障害物を検知") if v else
                          ("obstacle_cleared", "notice", "障害物は検知されなくなりました(自動再開しません)"))
    if "comms_ok" in changes:
        v = 1 if changes["comms_ok"] else 0
        if v != s["comms_ok"]:
            s["comms_ok"] = v
            if live:
                ev.append(("comms_lost", "critical", "通信が切断されました(施錠維持・停止)") if not v else
                          ("comms_restored", "notice", "通信が回復しました(自動では再開・開錠しません)"))
    if "sensor_ok" in changes:
        v = 1 if changes["sensor_ok"] else 0
        if v != s["sensor_ok"]:
            s["sensor_ok"] = v
            if live:
                ev.append(("sensor_fault", "critical", "センサー異常を検知(施錠維持・停止)") if not v else
                          ("sensor_restored", "notice", "センサーが正常に戻りました(自動では再開しません)"))
    if "signal_state" in changes:
        v = changes["signal_state"]
        if v not in ("red", "green", "unknown"):
            raise ActionError("信号の値が不正です")
        if v != s["signal_state"]:
            s["signal_state"] = v
            if live:
                ev.append(("signal_change", "info", "信号の状態: " + {"red": "赤", "green": "青", "unknown": "確認不可"}[v]))
    s, ev2 = apply_tick(s, 0, now)
    return s, ev + ev2


def apply_face_detected(res, now):
    s, ev = dict(res), []
    if s["status"] != "arrived":
        raise ActionError("到着後の本人確認待ちではありません")
    if not s["comms_ok"] or not s["sensor_ok"]:
        raise ActionError("通信またはセンサーが異常なため確認できません(施錠維持)")
    s["status"] = "unlock_pending"
    ev.append(("face_detected", "notice", "利用者がカメラの前に来ました"))
    ev.append(("unlock_request", "critical", "開錠要求: 映像と配送情報を確認して「開ける/開けない」を判断してください"))
    return s, ev


def apply_unlock(res, decision, now):
    s, ev = dict(res), []
    if s["status"] != "unlock_pending":
        raise ActionError("開錠判断待ちではありません")
    if decision == "deny":
        s["status"], s["locked"] = "denied", 1
        ev.append(("unlock_denied", "notice", "管理者が「開けない」を選択(施錠維持)"))
        return s, ev
    if decision != "open":
        raise ActionError("不正な操作です")
    if not s["comms_ok"] or not s["sensor_ok"]:
        raise ActionError("通信またはセンサー異常のため開錠できません(施錠維持)",
                          [("unlock_result", "critical", "開錠失敗: 通信/センサー異常のため施錠維持")])
    s["status"], s["locked"] = "unlocked", 0
    s["unlock_expires_at"] = iso(now + timedelta(seconds=UNLOCK_WINDOW_S))
    ev.append(("unlock_result", "warning", f"開錠しました({UNLOCK_WINDOW_S}秒後に自動施錠)"))
    return s, ev


def apply_complete(res, now):
    s, ev = dict(res), []
    if s["status"] not in ("arrived", "unlock_pending", "unlocked", "denied"):
        raise ActionError("完了にできる状態ではありません")
    was_open = s["locked"] == 0
    if was_open:
        ev.append(("relock", "notice", "施錠しました"))
    _finish_delivery(s, ev)
    return s, ev


def apply_cancel(res, now):
    s, ev = dict(res), []
    if s["status"] != "reserved":
        raise ActionError("出発前の予約のみキャンセルできます")
    s["status"] = "cancelled"
    ev.append(("cancelled", "notice", "予約がキャンセルされました"))
    return s, ev
