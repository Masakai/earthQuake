#!/usr/bin/env python3
"""P2P地震リストの地震に、防災科研F-netのモーメントテンソル解（地震球）を重ねた地図を描く。

データの流れ:
  1. P2P地震情報API（api.p2pquake.net）から指定期間の地震リストを取得
  2. 防災科研F-netの検索（mec_search.php）から同期間のMTカタログを取得
  3. 発生時刻（JST・分単位）で両者を突合
  4. マッチした地震の震源位置に、F-netのモーメントテンソル6成分から
     ObsPyでビーチボール（地震球）を描き、日本白地図に重ねる
  5. 国内RS4D観測点（国内RS4D.json）を点で重ねる

注意:
  - F-netはおおむねMw3.5以上のみMT解を出すため、P2Pの小規模地震は地震球が付かない。
  - F-netのページは認証不要だが公式APIではないため、取得負荷をかけない（期間を絞る）こと。
  - 座標系はWGS84（緯度経度）でそのままプロットする（日本周辺なら歪みは無視できる）。
"""

import argparse
import datetime
import html as _html
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.patheffects as pe  # noqa: E402
import numpy as np  # noqa: E402
import geopandas as gpd  # noqa: E402
from obspy.imaging.beachball import beach  # noqa: E402


# ===== HTTPS用SSLコンテキスト =====
# Python(特に3.12)はシステムCAを見つけられず証明書検証に失敗することがある。
# certifi があればそのCA束を使い、無ければシステムデフォルトにフォールバックする。
def _make_ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


_SSL_CTX = _make_ssl_context()


# ===== パス（analyze_rs.py と同じくプロジェクト直下の data/ を基準にする） =====
_PROJECT = pathlib.Path(__file__).parent.parent
_NE_PROVINCES = _PROJECT / 'data' / 'ne' / 'provinces' / 'ne_10m_admin_1_states_provinces.shp'
_NE_COUNTRIES = _PROJECT / 'data' / 'ne' / 'countries' / 'ne_10m_admin_0_countries_jpn.shp'
_RS4D_JSON = _PROJECT / '国内RS4D.json'
_CACHE_DIR = _PROJECT / 'data' / 'p2p_cache'

# ===== 配色（analyze_rs.py の地図と揃える） =====
_SEA = '#dce8f0'
_LAND = '#f5f5f0'
_BORDER = '#aaaaaa'
_GRID = '#d0d7de'
_TEXT = '#1f2328'
_SUBTEXT = '#57606a'

# 日本語フォント（環境にあれば使う）
for _f in ('Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'YuGothic',
           'Noto Sans CJK JP', 'IPAexGothic'):
    try:
        matplotlib.font_manager.findfont(_f, fallback_to_default=False)
        plt.rcParams['font.family'] = _f
        break
    except Exception:
        continue
plt.rcParams['axes.unicode_minus'] = False


# ===== P2P地震情報の取得 =====
def fetch_p2p(start: datetime.datetime, end: datetime.datetime,
              min_mag: float = 0.0) -> list[dict]:
    """P2P地震情報APIから [start, end] のJST期間に発生した地震を返す（新しい順→古い順に整列）。"""
    quakes: list[dict] = []
    offset = 0
    limit = 100
    # 期間より古い地震に到達したら打ち切る
    while True:
        url = (f'https://api.p2pquake.net/v2/history'
               f'?codes=551&limit={limit}&offset={offset}')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            page = json.loads(r.read())
        if not page:
            break
        reached_older = False
        for eq in page:
            info = eq.get('earthquake', {})
            t_str = info.get('time', '')
            if not t_str:
                continue
            try:
                dt = datetime.datetime.strptime(t_str[:19], '%Y/%m/%d %H:%M:%S')
            except ValueError:
                try:
                    dt = datetime.datetime.strptime(t_str[:16], '%Y/%m/%d %H:%M')
                except ValueError:
                    continue
            if dt < start:
                reached_older = True
                continue
            if dt > end:
                continue
            hypo = info.get('hypocenter', {})
            mag = hypo.get('magnitude', -1)
            lat = hypo.get('latitude', None)
            lon = hypo.get('longitude', None)
            if mag is None or mag < min_mag:
                continue
            if lat in (None, -200, -200.0) or lon in (None, -200, -200.0):
                continue
            quakes.append({
                'dt': dt,
                'time': t_str,
                'name': hypo.get('name', ''),
                'lat': lat,
                'lon': lon,
                'mag': mag,
                'depth': hypo.get('depth', -1),
                'scale': info.get('maxScale', -1),
            })
        if reached_older:
            break
        offset += limit
        if offset > 2000:  # 安全弁
            break
    quakes.sort(key=lambda q: q['dt'])
    return quakes


def load_p2p_cache(year: int, month: int,
                   start: datetime.datetime, end: datetime.datetime,
                   min_mag: float = 0.0) -> list[dict]:
    """P2Pキャッシュ(data/p2p_cache/YYYYMM.jsonl)から [start, end] の地震を返す。

    過去月は API の取得範囲(offset安全弁2000件)を超えて遡れないため、
    fetch_p2p_daily.py が日々ためているキャッシュを使う。無ければ空リスト。
    """
    path = _CACHE_DIR / f'{year}{month:02d}.jsonl'
    if not path.exists():
        return []
    quakes: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t_str = rec.get('time', '')
        try:
            dt = datetime.datetime.strptime(t_str[:19], '%Y/%m/%d %H:%M:%S')
        except ValueError:
            try:
                dt = datetime.datetime.strptime(t_str[:16], '%Y/%m/%d %H:%M')
            except ValueError:
                continue
        if dt < start or dt > end:
            continue
        mag = rec.get('mag', rec.get('magnitude', -1))
        lat = rec.get('lat', rec.get('latitude', None))
        lon = rec.get('lon', rec.get('longitude', None))
        if mag is None or mag < min_mag:
            continue
        if lat in (None, -200, -200.0) or lon in (None, -200, -200.0):
            continue
        quakes.append({
            'dt': dt,
            'time': t_str,
            'name': rec.get('name', ''),
            'lat': lat,
            'lon': lon,
            'mag': mag,
            'depth': rec.get('depth', -1),
            'scale': rec.get('scale', rec.get('maxScale', -1)),
        })
    quakes.sort(key=lambda q: q['dt'])
    return quakes


# ===== F-net モーメントテンソル解の取得 =====
# mec_search.php が返す <pre> ブロックの列順（実測）
#   0:発生時刻(JST) 1:lat 2:lon 3:深さ(JMA) 4:Mj 5:震央地名
#   6:走向(a;b) 7:傾斜(a;b) 8:すべり角(a;b) 9:Mo(Nm) 10:CMT深さ 11:Mw 12:品質(VR)
#   13-18:mxx mxy mxz myy myz mzz 19:Unit(Nm) 20:観測点数 21:観測点 22:URL
def fetch_fnet(start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """F-netの検索結果からMT解の一覧を返す。"""
    days = (end.date() - start.date()).days + 1
    form = {
        'LANG': 'ja',
        'tm_flg': 'jst',
        'year1': f'{start.year}', 'month1': f'{start.month:02d}',
        'day1': f'{start.day:02d}', 'hour1': f'{start.hour:02d}',
        'min1': f'{start.minute:02d}',
        'end_flg': 'days', 'days': str(days),
        # 各条件はフィルタせず全件（AND結合・空値）
        'latitude_flg': 'and', 'latitude1': '', 'latitude2': '',
        'longitude_flg': 'and', 'longitude1': '', 'longitude2': '',
        'depth_flg': 'and', 'depth1': '', 'depth2': '',
        'mj_flg': 'and', 'mj1': '', 'mj2': '',
        'mw_flg': 'and', 'mw1': '', 'mw2': '',
        'mo_flg': 'and', 'mo1': '', 'mo2': '',
        'strike_flg': 'and', 'strike1': '', 'strike2': '',
        'dip_flg': 'and', 'dip1': '', 'dip2': '',
        'rake_flg': 'and', 'rake1': '', 'rake2': '',
        'varred_flg': 'and', 'varred1': '', 'varred2': '',
        'cmt_depth_flg': 'and', 'cmt_depth1': '', 'cmt_depth2': '',
        'time_flg': 'and',
        'nofstns_flg': 'ge', 'nofstns': '1',
        'region_flg': '', 'region_name': '',
        'sphere_lat': '', 'sphere_lon': '', 'sphere_dis': '',
    }
    data = urllib.parse.urlencode(form).encode('euc-jp', 'replace')
    req = urllib.request.Request(
        'https://www.fnet.bosai.go.jp/event/mec_search.php',
        data=data,
        headers={
            'Referer': 'https://www.fnet.bosai.go.jp/event/search.php?LANG=ja',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0',
        },
    )
    with urllib.request.urlopen(req, timeout=40, context=_SSL_CTX) as r:
        raw = r.read().decode('euc-jp', 'replace')

    m = re.search(r'<pre class="search_form_base">(.*?)</pre>', raw, re.S)
    if not m:
        return []
    block = re.sub(r'<a [^>]*>|</a>', '', m.group(1))
    block = _html.unescape(block)

    events: list[dict] = []
    for line in block.splitlines():
        line = line.strip()
        if not re.match(r'^20\d\d/\d\d/\d\d', line):
            continue
        p = line.split()
        if len(p) < 22:
            continue
        try:
            dt = datetime.datetime.strptime(p[0], '%Y/%m/%d,%H:%M:%S.%f')
        except ValueError:
            try:
                dt = datetime.datetime.strptime(p[0][:19], '%Y/%m/%d,%H:%M:%S')
            except ValueError:
                continue
        try:
            unit = float(p[19])
            mt = [float(p[i]) * unit for i in range(13, 19)]  # mxx..mzz を実値(Nm)に
        except ValueError:
            continue
        # 走向・傾斜・すべり角（2節面ぶん、'a;b'形式）。解説のタイプ判定に使う。
        def _first(s):
            try:
                return float(s.split(';')[0])
            except (ValueError, IndexError):
                return None
        events.append({
            'dt': dt,
            'lat': float(p[1]),
            'lon': float(p[2]),
            'depth': float(p[3]),
            'mj': float(p[4]),
            'region': p[5],
            'mw': float(p[11]),
            'mt': mt,  # [mrr相当ではなくmxx,mxy,mxz,myy,myz,mzz] = F-net定義(NED)
            'strike': _first(p[6]),
            'dip': _first(p[7]),
            'rake': _first(p[8]),
            'url': p[-1],
        })
    return events


# ===== P2P と F-net の突合 =====
def match_quakes(p2p: list[dict], fnet: list[dict],
                 tol_minutes: int = 3) -> list[dict]:
    """発生時刻が近いP2P地震とF-net MT解を突合し、両方の情報を持つレコードを返す。"""
    tol = datetime.timedelta(minutes=tol_minutes)
    matched: list[dict] = []
    used = set()
    for q in p2p:
        best = None
        best_dt = None
        for i, f in enumerate(fnet):
            if i in used:
                continue
            diff = abs((f['dt'] - q['dt']).total_seconds())
            if diff <= tol.total_seconds():
                if best is None or diff < best_dt:
                    best = i
                    best_dt = diff
        if best is not None:
            used.add(best)
            f = fnet[best]
            matched.append({
                'time': q['time'],
                'name': q['name'],
                'lat': f['lat'],          # 震源位置はF-net（精度が高い）
                'lon': f['lon'],
                'depth': f['depth'],
                'mag': q['mag'],
                'mw': f['mw'],
                'scale': q['scale'],
                'mt': f['mt'],
                'region': f['region'],
                'strike': f.get('strike'),
                'dip': f.get('dip'),
                'rake': f.get('rake'),
            })
    return matched


# ===== 地図データ（遅延ロード） =====
_gdf_provinces = None
_gdf_countries = None


def _load_map_data():
    global _gdf_provinces, _gdf_countries
    if _gdf_provinces is not None:
        return
    if _NE_PROVINCES.exists():
        all_prov = gpd.read_file(str(_NE_PROVINCES))
        _gdf_provinces = all_prov[all_prov['admin'] == 'Japan']
    if _NE_COUNTRIES.exists():
        _gdf_countries = gpd.read_file(str(_NE_COUNTRIES))


def _load_rs4d() -> list[dict]:
    if not _RS4D_JSON.exists():
        return []
    data = json.loads(_RS4D_JSON.read_text(encoding='utf-8'))
    return data.get('stations', [])


def _scale_label(scale: int) -> str:
    return {10: '1', 20: '2', 30: '3', 40: '4', 45: '5弱', 50: '5強',
            55: '6弱', 60: '6強', 70: '7'}.get(scale, '不明')


def fault_type(rake) -> str:
    """すべり角(rake)から断層タイプを判定する。rakeはNoneなら'不明'。"""
    if rake is None:
        return '不明'
    r = rake
    while r > 180:
        r -= 360
    while r < -180:
        r += 360
    a = abs(r)
    if a <= 30 or a >= 150:
        return '横ずれ断層'
    if 60 <= r <= 120:
        return '逆断層'
    if -120 <= r <= -60:
        return '正断層'
    if 30 < r < 60 or 120 < r < 150:
        return '逆断層成分を含む斜めずれ'
    return '正断層成分を含む斜めずれ'


def _depth_class(depth) -> str:
    """深さの区分。"""
    if depth is None:
        return ''
    if depth >= 150:
        return '深発地震（沈み込んだプレート内部）'
    if depth >= 60:
        return 'やや深い地震'
    return '浅い地震'


# 断層タイプごとの読み方の一言
_TYPE_HINT = {
    '逆断層': 'プレートや地塊が水平に押し合う（圧縮）力で生じます。海溝沿いの沈み込み帯に典型的で、'
              '地震球は中央が塗り（圧縮側）になります。',
    '正断層': '地塊が水平に引かれる（伸張）力で生じます。地震球は中央が白く、縁が塗りになります。',
    '横ずれ断層': '断層が水平方向にずれる動きです。地震球は縦4分割の市松模様になります。',
    '逆断層成分を含む斜めずれ': '圧縮の力に横ずれが加わった動きです。',
    '正断層成分を含む斜めずれ': '伸張の力に横ずれが加わった動きです。',
    '不明': '',
}


def make_commentary(matched: list[dict], top_n: int = 6) -> list[dict]:
    """主要地震（Mw上位）の個別解説データを返す。

    各要素: {time, name, mag, mw, scale, depth, strike, dip, rake, ftype, text}
    text はルールベースで生成した1〜2文の解説。
    """
    ranked = sorted(matched, key=lambda m: m.get('mw', 0), reverse=True)[:top_n]
    out = []
    for m in ranked:
        ftype = fault_type(m.get('rake'))
        dcls = _depth_class(m.get('depth'))
        hint = _TYPE_HINT.get(ftype, '')
        sd = ''
        if m.get('strike') is not None and m.get('dip') is not None and m.get('rake') is not None:
            sd = f"走向{m['strike']:.0f}°・傾斜{m['dip']:.0f}°・すべり角{m['rake']:.0f}°。"
        depth_txt = f"深さ{m['depth']:.0f}km" if m.get('depth') is not None else ''
        # 深発の注意喚起
        deep_note = ''
        if m.get('depth') is not None and m['depth'] >= 150:
            deep_note = '深さが大きいため、規模の割に地表の揺れは小さくなりがちです。'
        text = (
            f"{m['name']}で発生したMw{m['mw']:.1f}の地震。"
            f"{depth_txt}（{dcls}）。{sd}"
            f"発震機構は{ftype}型で、{hint}{deep_note}"
        ).replace('（）', '')
        out.append({
            'time': m['time'], 'name': m['name'], 'mag': m['mag'], 'mw': m['mw'],
            'scale': m['scale'], 'depth': m.get('depth'),
            'strike': m.get('strike'), 'dip': m.get('dip'), 'rake': m.get('rake'),
            'ftype': ftype, 'text': text,
        })
    return out


def _layout_offsets(srcs, radii, lon_span, lat_span, land=None,
                    x_scale=1.0, iters=1500):
    """震源点(srcs=[(lon,lat),...])に対し、互いに重ならず陸地を避けた配置点を返す。

    各ビーチボールは初期位置=震源とし、以下の力を繰り返し適用して落ち着かせる:
      - 円同士の反発（重なり解消）
      - 震源へ戻ろうとする弱いバネ（離れすぎ防止）
      - 陸地(land, shapely geometry)にかかっていれば海側へ押し出す力
    引き出し線で震源と結ぶ前提なので、震源から離れても重なり・陸地回避を優先する。

    x_scale: 経度方向の表示拡大率(map_aspect)。表示上の見た目距離は
        x方向がデータ距離×x_scale になるため、反発の距離計算をこのスケールで
        補正しないと横方向の重なりが残る。
    返り値は配置後の中心座標リスト [(lon,lat),...]。
    """
    from shapely.geometry import Point

    n = len(srcs)
    pos = np.array(srcs, dtype=float)
    src = np.array(srcs, dtype=float)
    rad = np.array(radii, dtype=float)

    # 海岸線からこの距離（地図度）だけ余白を取って海側に出す
    sea_margin = float(np.mean(radii)) * 0.5 if len(radii) else 0.1

    def land_penetration(x, y, r):
        """点(x,y)半径rのビーチボールが陸地にかかる/近すぎる場合、海側への押し出しを返す。

        ビーチボールの円（半径r）が陸地に重ならず、さらに海岸線から sea_margin の
        余白を持つまで海側へ確実に押し出す。海上の震源でも地表に重なるなら追い出される。
        """
        if land is None:
            return None
        p = Point(x, y)
        need = r + sea_margin  # 海岸線から確保したい最小距離
        if land.contains(p):
            # 陸内: 最寄り海岸の反対（海側）へ、円半径＋余白ぶん出す
            try:
                near = land.boundary.interpolate(land.boundary.project(p))
                v = np.array([x - near.x, y - near.y])
                d = np.hypot(*v)
                if d < 1e-9:
                    # 海岸線上: 法線が取れないので任意方向（東=海側が多い）へ
                    v = np.array([1.0, -0.3])
                    d = np.hypot(*v)
                # near へ向かって海岸を越え、さらに need ぶん海へ
                return -v / d * (d + need)
            except Exception:
                return None
        else:
            dist = land.distance(p)
            if dist < need:
                # 海上だが陸に近すぎる（ビーチボールが地表にかかる）→海岸線から遠ざける
                near = land.boundary.interpolate(land.boundary.project(p))
                v = np.array([x - near.x, y - near.y])
                dd = np.hypot(*v)
                if dd < 1e-9:
                    return None
                return v / dd * (need - dist)
        return None

    for _ in range(iters):
        moved = False
        for i in range(n):
            disp = np.zeros(2)
            # ビーチボール同士の反発。表示上の見た目距離で判定するため、x方向を
            # x_scale倍した「スケール空間」で距離を測る（経度方向の表示拡大を反映）。
            for j in range(n):
                if i == j:
                    continue
                d = pos[i] - pos[j]
                ds = np.array([d[0] * x_scale, d[1]])  # スケール空間の差分
                dist = np.hypot(*ds)
                # 半径和より少し離す（1.05で接触ゼロ＝完全分離。重なりを無くす）
                min_d = (rad[i] + rad[j]) * 1.05
                if dist < 1e-9:
                    ang = (i * 2.399963)  # 黄金角で決定的に散らす
                    ds = np.array([np.cos(ang), np.sin(ang)]) * 1e-3
                    dist = 1e-3
                if dist < min_d:
                    # スケール空間の押し出しをデータ座標へ戻す（x成分は1/x_scale）
                    push_s = ds / dist * (min_d - dist) * 0.5
                    disp += np.array([push_s[0] / x_scale, push_s[1]])
                    moved = True
            # 陸地回避（振動を抑えるため減衰係数を掛けて少しずつ海へ寄せる）
            push = land_penetration(pos[i, 0], pos[i, 1], rad[i])
            on_land = push is not None
            if on_land:
                disp += push * 0.5
                moved = True
            # 震源へ引き戻す弱いバネ（陸地にかかっている間は引き戻さない）。
            # 反発を優先して重なりを確実に消すため、引き戻しは弱めにする。
            if not on_land:
                disp += (src[i] - pos[i]) * 0.008
            pos[i] += disp
        if not moved:
            break
    return [tuple(p) for p in pos]


# ===== 描画 =====
def _build_figure(matched: list[dict], title: str, show_stations: bool = True):
    """地震球マップのfigureを構築して返す（保存はしない）。matchedが空ならNone。"""
    if not matched:
        return None

    lats = [m['lat'] for m in matched]
    lons = [m['lon'] for m in matched]
    stations = _load_rs4d() if show_stations else []
    if stations:
        lats += [s['lat'] for s in stations]
        lons += [s['lon'] for s in stations]

    # 余白は広めに取る（ビーチボールが海側へ押し出されて枠外に出るのと、震源分布の
    # 外側にある陸地=北海道北部・サハリン等が切れるのを防ぐ）。
    margin = 2.5
    lat_min, lat_max = min(lats) - margin, max(lats) + margin
    lon_min, lon_max = min(lons) - margin, max(lons) + margin

    # 緯度補正: 経度1度の実距離は緯度1度の cos(緯度) 倍しかないため、aspect='equal'
    # （1度=1度）だと地図が東西に間延びして「斜めから見た」形になる。中央緯度で
    # aspect = 1/cos(lat_mid) を設定し、経度方向を縮めて地理的に正しい比率にする
    # （メルカトル相当の簡易版）。
    lat_mid = (lat_min + lat_max) / 2
    map_aspect = 1.0 / np.cos(np.radians(lat_mid))

    # figsizeは「画面上の地図の縦横比」= (緯度幅) : (経度幅 / map_aspect) に合わせる。
    lon_w = lon_max - lon_min
    lat_h = lat_max - lat_min
    disp_w = lon_w / map_aspect   # 経度方向の表示上の相対幅
    disp_h = lat_h
    base = 13.0
    if disp_w >= disp_h:
        fig_w, fig_h = base, base * disp_h / disp_w
    else:
        fig_w, fig_h = base * disp_w / disp_h, base
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_facecolor(_SEA)
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect(map_aspect)

    _load_map_data()
    bbox = (lon_min, lat_min, lon_max, lat_max)
    if _gdf_countries is not None:
        _gdf_countries.clip(bbox).plot(ax=ax, facecolor=_LAND,
                                       edgecolor=_BORDER, lw=0.5, zorder=1)
    if _gdf_provinces is not None:
        _gdf_provinces.clip(bbox).plot(ax=ax, facecolor=_LAND,
                                       edgecolor=_BORDER, lw=0.4, zorder=2)

    # geopandasの.plot()がxlim/ylim/aspectを自前のautoscaleで上書きするので、
    # 地図描画後に表示範囲と緯度補正アスペクトを再固定する。
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect(map_aspect)

    # グリッド
    lon_ticks = np.arange(np.ceil(lon_min), np.floor(lon_max) + 0.5, 1.0)
    lat_ticks = np.arange(np.ceil(lat_min), np.floor(lat_max) + 0.5, 1.0)
    ax.set_xticks(lon_ticks)
    ax.set_yticks(lat_ticks)
    ax.set_xticklabels([f'{v:.0f}°E' for v in lon_ticks], fontsize=8, color=_SUBTEXT)
    ax.set_yticklabels([f'{v:.0f}°N' for v in lat_ticks], fontsize=8, color=_SUBTEXT)
    ax.grid(color=_GRID, lw=0.4, ls=':', zorder=3)

    # ビーチボールのサイズ（地図の広さに対して相対）
    span = max(lon_max - lon_min, lat_max - lat_min)
    base_width = span * 0.022

    # 各地震のビーチボール幅（マグニチュードで可変）と震源座標
    widths = [base_width * (0.8 + 0.12 * max(m['mw'], 3.0)) for m in matched]
    srcs = [(m['lon'], m['lat']) for m in matched]
    # 衝突判定用の半径は実描画半径(w/2)に余白(1.25倍)を加える。ビーチボールの縁線・
    # 節線のはみ出しや下に付くラベル分を見込み、見た目で確実に隙間が空くようにする。
    radii = [w / 2 * 1.25 for w in widths]

    # 表示範囲の陸地ジオメトリ（ビーチボールが陸に重ならないよう避ける）
    land_geom = None
    if _gdf_countries is not None:
        try:
            clipped_land = _gdf_countries.clip(bbox)
            if len(clipped_land):
                land_geom = clipped_land.union_all()
        except Exception:
            land_geom = None

    # 重ならず陸地を避けた配置点を計算（引き出し線で震源と結ぶ）。
    # x_scale=map_aspect で経度方向の表示拡大を反発計算に反映する。
    placed = _layout_offsets(srcs, radii, lon_max - lon_min, lat_max - lat_min,
                             land=land_geom, x_scale=map_aspect)

    # 震源点（実際の位置）を先にまとめて描く
    ax.scatter([s[0] for s in srcs], [s[1] for s in srcs],
               s=18, marker='o', color='#bc4c00', edgecolor='white',
               linewidth=0.6, zorder=7)

    for m, w, (sx, sy), (px, py) in zip(matched, widths, srcs, placed):
        # 引き出し線（震源点 → ビーチボール中心）。離れているときだけ描く
        if np.hypot(px - sx, py - sy) > w * 0.15:
            ax.plot([sx, px], [sy, py], color=_SUBTEXT, lw=0.6,
                    ls='-', zorder=4, alpha=0.7)
        try:
            # F-netのテンソルはNED系（mxx,mxy,mxz,myy,myz,mzz）。
            # ObsPyのbeach()は球座標USE系 [Mrr,Mtt,Mpp,Mrt,Mrp,Mtp] を期待するため変換する。
            #   Mrr=Mdd=mzz, Mtt=Mnn=mxx, Mpp=Mee=myy,
            #   Mrt=mxz, Mrp=-myz, Mtp=-mxy
            # この変換則は strike/dip/rake から描いた節面と一致することを実測で確認済み。
            mxx, mxy, mxz, myy, myz, mzz = m['mt']
            fm = [mzz, mxx, myy, mxz, -myz, -mxy]
            # aspect=map_aspect(>1) で経度方向のpx/度が緯度方向の1/map_aspect倍に縮むため、
            # 同じデータ幅だとビーチボールがx方向に潰れて縦長になる。データ座標のx幅を
            # map_aspect倍に拡げて相殺し、表示上で真円にする。
            # beach()はwidth=(幅x, 幅y)のタプルで楕円パッチを作れる。
            bb = beach(fm, xy=(px, py), width=(w * map_aspect, w),
                       linewidth=0.6, facecolor='#bc4c00', edgecolor='#5a2400',
                       zorder=6)
            ax.add_collection(bb)
        except Exception as e:
            # テンソルが描けない場合は星で代替
            ax.scatter([px], [py], s=120, marker='*',
                       color='#bc4c00', zorder=6)
            print(f'  ! ビーチボール描画失敗 {m["time"]} {m["name"]}: {e}')

        label = f"M{m['mag']}"
        ax.text(px, py - w * 0.62, label,
                color=_TEXT, fontsize=6.5, ha='center', va='top', zorder=8,
                path_effects=[pe.withStroke(linewidth=2, foreground='white')])

    # RS4D観測点
    if stations:
        ax.scatter([s['lon'] for s in stations], [s['lat'] for s in stations],
                   s=45, marker='^', color='#0969da', edgecolor='white',
                   linewidth=0.5, zorder=5, label='RS4D観測点')

    # 北矢印
    ax.annotate('N', xy=(0.05, 0.93), xytext=(0.05, 0.85),
                xycoords='axes fraction', textcoords='axes fraction',
                ha='center', color=_TEXT, fontsize=11, fontweight='bold',
                arrowprops=dict(arrowstyle='->', color=_TEXT, lw=1.5))

    if stations:
        ax.legend(loc='lower right', fontsize=9,
                  facecolor='#f6f8fa', edgecolor=_GRID, labelcolor=_TEXT)

    ax.set_title(title, color=_TEXT, fontsize=14, pad=10)
    fig.text(0.5, 0.015,
             '地震球: 防災科研 F-net モーメントテンソル解 / 地震リスト: P2P地震情報',
             ha='center', color=_SUBTEXT, fontsize=8)
    return fig


def draw_map(matched: list[dict], out_path: pathlib.Path,
             title: str, show_stations: bool = True):
    """地震球マップを描いてPNGファイルに保存する。"""
    fig = _build_figure(matched, title, show_stations=show_stations)
    if fig is None:
        print('地震球を描ける地震がありません（F-netのMT解とマッチしませんでした）。')
        return
    # bbox_inches='tight' は使わない（タイトル等のはみ出しで縦横比が崩れ球が歪むため）。
    fig.savefig(str(out_path), dpi=140, facecolor='white')
    plt.close(fig)
    print(f'保存: {out_path}  （地震球 {len(matched)} 個）')


def make_month_beachball_section(year: int, month: int,
                                 show_stations: bool = True,
                                 top_n: int = 6) -> dict | None:
    """指定年月の地震球マップ(base64)と主要地震の個別解説を返す。

    月次レポート(monthly_report.py)への埋め込み用。P2PとF-netを取得・突合し、
    マッチが無ければ None を返す（呼び出し側でセクションを出さない判断に使う）。
    返り値: {'img_b64': str, 'commentary': list[dict], 'count': int}
    """
    start = datetime.datetime(year, month, 1)
    if month == 12:
        end = datetime.datetime(year, 12, 31, 23, 59, 59)
    else:
        end = datetime.datetime(year, month + 1, 1) - datetime.timedelta(seconds=1)
    try:
        # 過去月は API では遡れないため、まずキャッシュを使う。
        # キャッシュが無い（当月など）場合は API へフォールバック。
        p2p = load_p2p_cache(year, month, start, end)
        if not p2p:
            p2p = fetch_p2p(start, end)
        fnet = fetch_fnet(start, end)
    except Exception as e:
        print(f'[WARN] 地震球マップのデータ取得に失敗: {e}')
        return None
    matched = match_quakes(p2p, fnet)
    if not matched:
        return None
    title = f'発震機構解マップ  {year}年{month}月'
    fig = _build_figure(matched, title, show_stations=show_stations)
    if fig is None:
        return None
    import base64
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120, facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return {
        'img_b64': base64.b64encode(buf.read()).decode(),
        'commentary': make_commentary(matched, top_n=top_n),
        'count': len(matched),
    }


def make_month_beachball_b64(year: int, month: int,
                             show_stations: bool = True) -> str | None:
    """互換用: 地震球マップのbase64のみを返す。"""
    sec = make_month_beachball_section(year, month, show_stations=show_stations)
    return sec['img_b64'] if sec else None


def main():
    ap = argparse.ArgumentParser(
        description='P2P地震リストにF-netの地震球を重ねた地図を作る')
    ap.add_argument('--start', help='開始日 YYYY-MM-DD（JST）')
    ap.add_argument('--end', help='終了日 YYYY-MM-DD（JST、当日含む）')
    ap.add_argument('--days', type=int, default=7,
                    help='--start/--end未指定時、直近N日（既定7）')
    ap.add_argument('--min-mag', type=float, default=0.0,
                    help='P2P側のマグニチュード下限')
    ap.add_argument('--tol', type=int, default=3,
                    help='時刻突合の許容差（分、既定3）')
    ap.add_argument('--no-stations', action='store_true',
                    help='RS4D観測点を地図に描かない')
    ap.add_argument('-o', '--out', default=None, help='出力PNGパス')
    args = ap.parse_args()

    now = datetime.datetime.now()
    if args.start:
        start = datetime.datetime.strptime(args.start, '%Y-%m-%d')
    else:
        start = (now - datetime.timedelta(days=args.days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    if args.end:
        end = datetime.datetime.strptime(args.end, '%Y-%m-%d').replace(
            hour=23, minute=59, second=59)
    else:
        end = now

    print(f'期間: {start:%Y-%m-%d %H:%M} 〜 {end:%Y-%m-%d %H:%M} (JST)')
    print('P2P地震情報を取得中...')
    p2p = fetch_p2p(start, end, min_mag=args.min_mag)
    print(f'  P2P地震: {len(p2p)} 件')
    print('F-net MT解を取得中...')
    fnet = fetch_fnet(start, end)
    print(f'  F-net MT解: {len(fnet)} 件')
    matched = match_quakes(p2p, fnet, tol_minutes=args.tol)
    print(f'  突合（地震球を描ける地震）: {len(matched)} 件')

    if args.out:
        out = pathlib.Path(args.out)
    else:
        out = _PROJECT / f'beachball_map_{start:%Y%m%d}_{end:%Y%m%d}.png'

    title = f'発震機構解マップ  {start:%Y/%m/%d} 〜 {end:%Y/%m/%d}'
    draw_map(matched, out, title, show_stations=not args.no_stations)


if __name__ == '__main__':
    sys.exit(main())
