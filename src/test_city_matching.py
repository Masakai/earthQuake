"""市区町村塗りつぶし照合（resolveCity）の全局検証テスト。

dashboard.html の resolveCity（JS）と同一の照合ロジックを Python で再現し、
data/jma_stations.json 全局を対応県の GeoJSON から構築した keys で照合する。
塗り漏れ（None）は addr に「空港」を含む局のみ許容し、それ以外は fail とする。
"""
import json
import re
import glob
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEOJSON_DIR = ROOT / "data" / "geojson"
STATIONS_PATH = ROOT / "data" / "jma_stations.json"

# dashboard.html の PREF_CODE（1016-1027行）と同一
PREF_CODE = {
    '北海道': '01', '青森県': '02', '岩手県': '03', '宮城県': '04', '秋田県': '05',
    '山形県': '06', '福島県': '07', '茨城県': '08', '栃木県': '09', '群馬県': '10',
    '埼玉県': '11', '千葉県': '12', '東京都': '13', '神奈川県': '14', '新潟県': '15',
    '富山県': '16', '石川県': '17', '福井県': '18', '山梨県': '19', '長野県': '20',
    '岐阜県': '21', '静岡県': '22', '愛知県': '23', '三重県': '24', '滋賀県': '25',
    '京都府': '26', '大阪府': '27', '兵庫県': '28', '奈良県': '29', '和歌山県': '30',
    '鳥取県': '31', '島根県': '32', '岡山県': '33', '広島県': '34', '山口県': '35',
    '徳島県': '36', '香川県': '37', '愛媛県': '38', '高知県': '39', '福岡県': '40',
    '佐賀県': '41', '長崎県': '42', '熊本県': '43', '大分県': '44', '宮崎県': '45',
    '鹿児島県': '46', '沖縄県': '47',
}

SEIREI = ['札幌市', '仙台市', 'さいたま市', '千葉市', '横浜市', '川崎市', '相模原市',
          '新潟市', '静岡市', '浜松市', '名古屋市', '京都市', '大阪市', '堺市', '神戸市',
          '岡山市', '広島市', '北九州市', '福岡市', '熊本市']
SEIREI_NOCITY = {s[:-1]: s for s in SEIREI}
ADDR_NORM = {'梼': '檮'}


def norm(s):
    return ''.join(ADDR_NORM.get(ch, ch) for ch in s)


def match_seirei(s, keys, cities_with_ku):
    heads = []
    for nocity, full in SEIREI_NOCITY.items():
        if full in cities_with_ku:
            heads.append((full, full))
            heads.append((nocity, full))
    heads.sort(key=lambda x: -len(x[0]))
    for head, full in heads:
        if s.startswith(head):
            rest = s[len(head):]
            m = re.match(r'^(.+?区)', rest)
            if m and (full, m.group(1)) in keys:
                return (full, m.group(1))
    return None


def resolve_city(addr, keys):
    """keys: set of (N03_004, N03_005|None)。戻り値 (city, ku) または None。"""
    addr = norm(addr)
    cities_with_ku = {c for (c, k) in keys if k}
    plain = {c for (c, k) in keys if not k}
    for cut in range(0, 3):
        r = match_seirei(addr[cut:], keys, cities_with_ku)
        if r:
            return r
    for cut in range(0, 5):
        sub = addr[cut:]
        best = None
        for c in plain:
            if sub.startswith(c) and (best is None or len(c) > len(best)):
                best = c
        if best:
            return (best, None)
    return None


def build_keys(code):
    """県コードの GeoJSON 全 feature から (N03_004, N03_005|None) 集合を作る。"""
    keys = set()
    for fp in glob.glob(str(GEOJSON_DIR / code / "*.json")):
        fc = json.load(open(fp, encoding="utf-8"))
        for f in fc.get("features", []):
            p = f.get("properties", {})
            c = p.get("N03_004")
            if not c:
                continue
            keys.add((c, p.get("N03_005") or None))
    return keys


@pytest.fixture(scope="module")
def stations():
    return json.load(open(STATIONS_PATH, encoding="utf-8"))


@pytest.fixture(scope="module")
def keys_by_pref():
    cache = {}
    for pref, code in PREF_CODE.items():
        cache[pref] = build_keys(code)
    return cache


def test_no_unmatched_except_airports(stations, keys_by_pref):
    """塗り対象局の塗り漏れ0。None になるのは addr に『空港』を含む局のみ許容。"""
    misses = []
    for addr, info in stations.items():
        pref = info.get("pref")
        keys = keys_by_pref.get(pref)
        if not keys:
            # PREF_CODE に無い pref（想定外）は記録
            misses.append((addr, pref, "no-geojson"))
            continue
        if resolve_city(addr, keys) is None:
            if "空港" in addr:
                continue  # 市町村ポリゴンを持たない施設＝正常
            misses.append((addr, pref, "unmatched"))
    assert not misses, f"塗り漏れ {len(misses)}件: {misses[:20]}"


@pytest.mark.parametrize("addr,pref,expected", [
    ("浜松中央区高丘東", "静岡県", ("浜松市", "中央区")),
    ("大阪堺市堺区市役所", "大阪府", ("堺市", "堺区")),
    ("大阪堺市美原区黒山", "大阪府", ("堺市", "美原区")),
    ("静岡森町森", "静岡県", ("森町", None)),
    ("四日市市役所", "三重県", ("四日市市", None)),
    ("梼原町梼原", "高知県", ("檮原町", None)),
    ("札幌中央区北２条", "北海道", ("札幌市", "中央区")),
])
def test_representative_cases(addr, pref, expected, keys_by_pref):
    keys = keys_by_pref[pref]
    assert resolve_city(addr, keys) == expected
