"""外部の無料公開サービスとの連携(場所検索・道路の経路・信号機の位置)。

- 場所検索: 国土地理院の住所検索API + OpenStreetMap Nominatim
- 道路経路: FOSSGIS の OSRM(徒歩)  ※環境変数 ROUTING_URL で自前のOSRMに変更可
- 信号機の位置: OpenStreetMap の Overpass API
いずれも無料の公開サービスで、利用規約上「大量アクセス不可・User-Agentで連絡先を明示」が前提です。
環境変数 GEO_CONTACT に連絡先(メールアドレスなど)を設定してください。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONTACT = os.environ.get("GEO_CONTACT", "")
UA = "smart-cart-simulator/1.0" + (f" ({CONTACT})" if CONTACT else "")
GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
_ROUTING = os.environ.get("ROUTING_URL")
ROUTING_URLS = [_ROUTING] if _ROUTING else [
    "https://routing.openstreetmap.de/routed-foot/route/v1/driving",  # 同サーバーは経路名に driving を使う
    "https://routing.openstreetmap.de/routed-foot/route/v1/foot",
]


class ServiceError(Exception):
    """外部サービスに接続できない・想定外の応答。"""


class NoRoute(Exception):
    """2地点を結ぶ道がない。"""


def _http(url, data=None, timeout=12):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(8_000_000)
    except urllib.error.HTTPError as e:  # OSRMは経路なしを400+JSONで返すことがある
        body = e.read(200_000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ServiceError(str(e)) from e
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise ServiceError("応答を解釈できませんでした") from e


# ---------- 場所検索 ----------
_cache, _cache_lock = {}, threading.Lock()
_nom_lock, _nom_last = threading.Lock(), [0.0]


def _cache_get(key):
    with _cache_lock:
        v = _cache.get(key)
        if v and time.time() - v[0] < 3600:
            return v[1]
    return None


def _cache_put(key, val):
    with _cache_lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[key] = (time.time(), val)


def to_katakana(s):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def is_hiragana(s):
    return bool(s) and all("ぁ" <= c <= "ゖ" or c in "ー " for c in s)


# ひらがなで入力されたとき用の簡易辞書(都道府県・主な市)。「みや」→ 宮崎・宮城 のように前方一致で候補にする。
KANA = {
    "ほっかいどう": "北海道", "あおもり": "青森", "いわて": "岩手", "みやぎ": "宮城", "あきた": "秋田", "やまがた": "山形",
    "ふくしま": "福島", "いばらき": "茨城", "とちぎ": "栃木", "ぐんま": "群馬", "さいたま": "埼玉", "ちば": "千葉",
    "とうきょう": "東京", "かながわ": "神奈川", "にいがた": "新潟", "とやま": "富山", "いしかわ": "石川", "ふくい": "福井",
    "やまなし": "山梨", "ながの": "長野", "ぎふ": "岐阜", "しずおか": "静岡", "あいち": "愛知", "みえ": "三重", "しが": "滋賀",
    "きょうと": "京都", "おおさか": "大阪", "ひょうご": "兵庫", "なら": "奈良", "わかやま": "和歌山", "とっとり": "鳥取",
    "しまね": "島根", "おかやま": "岡山", "ひろしま": "広島", "やまぐち": "山口", "とくしま": "徳島", "かがわ": "香川",
    "えひめ": "愛媛", "こうち": "高知", "ふくおか": "福岡", "さが": "佐賀", "ながさき": "長崎", "くまもと": "熊本",
    "おおいた": "大分", "みやざき": "宮崎", "かごしま": "鹿児島", "おきなわ": "沖縄",
    "さっぽろ": "札幌", "はこだて": "函館", "あさひかわ": "旭川", "せんだい": "仙台", "みと": "水戸", "うつのみや": "宇都宮",
    "まえばし": "前橋", "たかさき": "高崎", "かわごえ": "川越", "よこはま": "横浜", "かわさき": "川崎", "さがみはら": "相模原",
    "かなざわ": "金沢", "まつもと": "松本", "はままつ": "浜松", "なごや": "名古屋", "とよた": "豊田", "おかざき": "岡崎",
    "こうべ": "神戸", "ひめじ": "姫路", "さかい": "堺", "おかやま": "岡山", "くらしき": "倉敷", "ふくやま": "福山",
    "しものせき": "下関", "まつやま": "松山", "たかまつ": "高松", "きたきゅうしゅう": "北九州", "くるめ": "久留米",
    "させぼ": "佐世保", "べっぷ": "別府", "のべおか": "延岡", "みやこのじょう": "都城", "ひゅうが": "日向", "にちなん": "日南",
    "こばやし": "小林", "さいき": "佐伯", "なは": "那覇", "みやこじま": "宮古島", "みやこ": "宮古", "みやづ": "宮津",
    "おおぶね": "大船渡", "なんこく": "南国", "あまくさ": "天草", "いぶすき": "指宿", "きりしま": "霧島", "えびの": "えびの",
}


def kana_candidates(q, limit=3):
    """ひらがな入力から漢字の地名候補を返す(完全一致を先頭に、前方一致を続ける)。"""
    key = q.replace(" ", "")
    if len(key) < 2 or not is_hiragana(key):
        return []
    exact = [v for k, v in KANA.items() if k == key]
    prefix = [v for k, v in KANA.items() if k != key and k.startswith(key)]
    out = []
    for v in exact + prefix:
        if v not in out:
            out.append(v)
    return out[:limit]


def _gsi(q):
    data = _http(GSI_URL + "?" + urllib.parse.urlencode({"q": q}))
    out = []
    for f in data if isinstance(data, list) else []:
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
            out.append({"name": f["properties"]["title"], "lat": float(lat), "lon": float(lon), "source": "国土地理院"})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _nominatim(q):
    with _nom_lock:  # 公開サーバーの規約: 1秒に1回まで
        wait = 1.1 - (time.time() - _nom_last[0])
        if wait > 0:
            time.sleep(wait)
        _nom_last[0] = time.time()
        data = _http(NOMINATIM_URL + "?" + urllib.parse.urlencode(
            {"q": q, "format": "jsonv2", "limit": 8, "countrycodes": "jp", "accept-language": "ja"}))
    out = []
    for it in data if isinstance(data, list) else []:
        try:
            name = str(it.get("display_name", "")).replace(", 日本", "")
            out.append({"name": name, "lat": float(it["lat"]), "lon": float(it["lon"]), "source": "OpenStreetMap"})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def search_places(q):
    q = " ".join(q.split())
    hit = _cache_get(("s", q))
    if hit is not None:
        return hit
    kanji = kana_candidates(q)
    # (検索サービス, 検索語) の組。国土地理院は軽いので多めに、Nominatimは1秒1回制限のため絞る
    jobs = [(_gsi, k) for k in kanji] + [(_gsi, q)]
    if is_hiragana(q):
        jobs.append((_gsi, to_katakana(q)))
    jobs += [(_nominatim, k) for k in kanji[:1]] + [(_nominatim, q)]
    found, errors = [], 0
    for fn, qq in jobs:
        try:
            found += fn(qq)
        except ServiceError:
            errors += 1
    if errors == len(jobs):
        raise ServiceError("検索サービスに接続できませんでした")
    out = []
    for p in found:
        if any(p["name"] == o["name"] or (abs(p["lat"] - o["lat"]) < 0.0003 and abs(p["lon"] - o["lon"]) < 0.0003) for o in out):
            continue
        out.append(p)
    out = out[:12]
    _cache_put(("s", q), out)
    return out


# ---------- 道路経路 ----------
def road_route(origin, dest):
    """(points[(lat,lon)...], distance_m, 道路への吸着距離m) を返す。"""
    last = None
    for base in ROUTING_URLS:
        url = f"{base}/{origin[1]:.6f},{origin[0]:.6f};{dest[1]:.6f},{dest[0]:.6f}?overview=full&geometries=geojson&steps=false"
        try:
            j = _http(url, timeout=25)
        except ServiceError as e:
            last = e
            continue
        code = j.get("code")
        if code in ("NoRoute", "NoSegment"):
            raise NoRoute(code)
        if code != "Ok" or not j.get("routes"):
            last = ServiceError(f"経路サービスの応答: {code}")
            continue
        pts = [(lat, lon) for lon, lat in j["routes"][0]["geometry"]["coordinates"]]
        snap = max([w.get("distance", 0) for w in j.get("waypoints", [])] or [0])
        return pts, j["routes"][0].get("distance", 0), snap
    raise last or ServiceError("経路サービスに接続できませんでした")


# ---------- 信号機の位置 ----------
def traffic_signals(points):
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    s, n, w, e = min(lats) - 0.0003, max(lats) + 0.0003, min(lons) - 0.0003, max(lons) + 0.0003
    if (n - s) * 111 > 40 or (e - w) * 90 > 40:
        raise ServiceError("範囲が広すぎるため信号機を調べられません")
    q = (f'[out:json][timeout:20];(node["highway"="traffic_signals"]({s},{w},{n},{e});'
         f'node["crossing"="traffic_signals"]({s},{w},{n},{e}););out;')
    j = _http(OVERPASS_URL, data=urllib.parse.urlencode({"data": q}).encode(), timeout=25)
    return [(el["lat"], el["lon"]) for el in j.get("elements", []) if "lat" in el and "lon" in el]
