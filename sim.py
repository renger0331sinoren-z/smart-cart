"""台車シミュレーション: 状態遷移と安全ルール(DBや画面に依存しない純粋関数)。

各 apply_* は (新しい状態dict, イベントlist) を返す。拒否する操作は ActionError を送出する。
イベントは (kind, severity, detail)。
"""
import json
import math
from datetime import datetime, timedelta, timezone

MAX_SPEED_KMH = 6.0  # 希望最高速度(上限として固定)
MAX_SPEED_MS = MAX_SPEED_KMH * 1000 / 3600
UNLOCK_WINDOW_S = 30
MAX_DT_S = 60.0
ACTIVE = ("en_route", "arrived", "unlock_pending", "unlocked")
FINAL = ("completed", "cancelled")

STATUS_LABEL = {
    "reserved": "予約受付",
    "en_route": "配送中",
    "arrived": "到着(本人確認待ち)",
    "unlock_pending": "開錠判断待ち",
    "unlocked": "開錠中",
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


def haversine(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def build_route(depot, dest):
    """出発地→(曲がり角)→配送先。曲がり角と擬似信号を停止ポイントとして置く。"""
    corner = (dest[0], depot[1])
    leg1, leg2 = haversine(depot, corner), haversine(corner, dest)
    if leg1 >= 5 and leg2 >= 5:
        pts, legs = [list(depot), list(corner), list(dest)], [leg1, leg2]
    else:
        pts, legs = [list(depot), list(dest)], [haversine(depot, dest)]
    total = sum(legs)
    stops = []
    if len(legs) == 2:
        stops.append({"dist": leg1, "kind": "corner", "state": "pending"})
    if total > 20:
        stops.append({"dist": total * 0.85, "kind": "signal", "state": "pending"})
    stops.sort(key=lambda x: x["dist"])
    return {"points": pts, "legs": legs, "total": total, "stops": stops}


def pos_at(route, dist):
    dist = max(0.0, min(dist, route["total"]))
    for i, leg in enumerate(route["legs"]):
        if dist <= leg or i == len(route["legs"]) - 1:
            a, b = route["points"][i], route["points"][i + 1]
            t = 0 if leg == 0 else min(1.0, dist / leg)
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        dist -= leg
    return tuple(route["points"][-1])


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
    if s["locked"] == 0:
        exp = s["unlock_expires_at"]
        if s["status"] != "unlocked":
            _relock(s, ev, "状態不整合のため施錠")
        elif not exp or now >= parse_iso(exp):
            _relock(s, ev, "開錠時間が終了したため自動施錠", "notice")
        elif not s["comms_ok"] or not s["sensor_ok"]:
            _relock(s, ev, "通信またはセンサーの異常を検知したため即時施錠", "critical")


def apply_tick(res, dt, now):
    s, ev = dict(res), []
    dt = max(0.0, min(float(dt), MAX_DT_S))
    _enforce_lock(s, now, ev)
    if s["status"] != "en_route":
        return s, ev

    cause = fault_cause(s)
    if cause and not s["hold_reason"]:
        s["hold_reason"] = cause
        ev.append(("safety_stop", "critical", "停止: " + HOLD_LABEL[cause]))
    if s["hold_reason"] and s["hold_reason"].startswith("signal_"):
        s["hold_reason"] = _signal_hold(s["signal_state"])  # 表示を最新の信号に合わせる(再開はしない)
    if s["hold_reason"]:
        return s, ev

    route = json.loads(s["route"])
    nxt = next((st for st in route["stops"] if st["state"] != "cleared"), None)
    if nxt and nxt["state"] == "reached":  # 通常は到達しないが、念のため停止を維持
        s["hold_reason"] = _stop_hold(nxt, s)
        return s, ev

    limit = nxt["dist"] if nxt else route["total"]
    dist = min(s["progress_m"] + MAX_SPEED_MS * dt, limit)
    s["progress_m"] = dist
    s["cart_lat"], s["cart_lon"] = pos_at(route, dist)

    if nxt and dist >= nxt["dist"]:
        nxt["state"] = "reached"
        s["route"] = json.dumps(route)
        reason = _stop_hold(nxt, s)
        s["hold_reason"] = reason
        kind = "corner_stop" if nxt["kind"] == "corner" else "signal_stop"
        ev.append((kind, "warning", "停止: " + HOLD_LABEL[reason] + "。映像と位置を確認して再開を許可してください"))
    elif not nxt and dist >= route["total"]:
        s["status"] = "arrived"
        s["cart_lat"], s["cart_lon"] = route["points"][-1]
        ev.append(("arrived", "warning", "配送先に到着しました(収納部は施錠中)"))
        ev.append(("voice_prompt", "info", VOICE_PROMPT))
    return s, ev


def apply_dispatch(res, depot, now):
    s, ev = dict(res), []
    if s["status"] != "reserved":
        raise ActionError("この予約は出発できる状態ではありません")
    cause = fault_cause(s)
    if cause:
        raise ActionError("出発できません: " + HOLD_LABEL[cause])
    route = build_route(depot, (s["lat"], s["lon"]))
    s.update(route=json.dumps(route), progress_m=0.0, status="en_route", hold_reason=None,
             locked=1, unlock_expires_at=None, cart_lat=depot[0], cart_lon=depot[1])
    ev.append(("dispatch", "notice", "配送を開始しました(シミュレーション)"))
    return s, ev


def apply_resume(res, confirmed, now):
    s, ev = dict(res), []
    if s["status"] != "en_route" or not s["hold_reason"]:
        raise ActionError("再開できる停止状態ではありません")
    if not confirmed:
        raise ActionError("安全確認のチェックが必要です")
    cause = fault_cause(s)
    if cause:
        raise ActionError("再開できません: " + HOLD_LABEL[cause] + "(解消を確認してください)")
    hold = s["hold_reason"]
    if hold.startswith("signal_") and s["signal_state"] != "green":
        raise ActionError("信号が青と確認できないため再開できません")
    route = json.loads(s["route"])
    reached = next((st for st in route["stops"] if st["state"] == "reached"), None)
    if hold in ("corner", "signal_red", "signal_unknown", "signal_green"):
        if reached:
            reached["state"] = "cleared"
        s["hold_reason"] = None
    elif reached:  # 非常停止などで止まっていても、曲がり角・信号の確認は別途必要
        s["hold_reason"] = _stop_hold(reached, s)
    else:
        s["hold_reason"] = None
    s["route"] = json.dumps(route)
    ev.append(("resume", "notice", "管理者が安全を確認し走行再開を許可しました" if not s["hold_reason"]
               else "停止要因は解除。ただし " + HOLD_LABEL[s["hold_reason"]] + " の確認が必要です"))
    return s, ev


def apply_estop(res, now):
    s, ev = dict(res), []
    if s["status"] != "en_route":
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
    s.update(status="completed", locked=1, unlock_expires_at=None, hold_reason=None)
    if was_open:
        ev.append(("relock", "notice", "施錠しました"))
    ev.append(("completed", "notice", "配送を完了しました"))
    return s, ev


def apply_cancel(res, now):
    s, ev = dict(res), []
    if s["status"] != "reserved":
        raise ActionError("出発前の予約のみキャンセルできます")
    s["status"] = "cancelled"
    ev.append(("cancelled", "notice", "予約がキャンセルされました"))
    return s, ev
