#!/usr/bin/env python3
"""
月次地震レポート生成スクリプト。
P2P地震情報APIから指定月のデータを取得し、震源地図・統計・文章解説をまとめた
HTMLレポートを data/monthly_report/report_YYYYMM.html に出力する。

使い方:
    python src/monthly_report.py              # 当月
    python src/monthly_report.py 2026 5       # 2026年5月
"""

import argparse
import base64
import calendar
import collections
import datetime
import io
import json
import pathlib
import sys
import time
import urllib.request

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import numpy as np


def _setup_font():
    for path in [
        '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
        '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]:
        try:
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            plt.rcParams['font.family'] = prop.get_name()
            return
        except Exception:
            pass

# ===== パス設定 =====
BASE_DIR        = pathlib.Path(__file__).parent.parent
NE_DIR          = BASE_DIR / 'data' / 'ne'
OUT_DIR         = BASE_DIR / 'data' / 'monthly_report'
OUT_DIR.mkdir(parents=True, exist_ok=True)
TRIGGER_LOG     = pathlib.Path.home() / 'Dropbox' / 'earthQuake' / 'logs' / 'trigger_log.jsonl'

COUNTRIES_SHP  = NE_DIR / 'countries'  / 'ne_10m_admin_0_countries_jpn.shp'
PROVINCES_SHP  = NE_DIR / 'provinces'  / 'ne_10m_admin_1_states_provinces.shp'

# P2P震度スケール → 震度文字列
SCALE_LABEL = {
    10: '1', 20: '2', 30: '3', 40: '4',
    45: '5弱', 50: '5強', 55: '6弱', 60: '6強', 70: '7',
}

# 震度 → 色（JMA準拠）
SCALE_COLOR = {
    10: '#3b82f6',   # 1: 青
    20: '#22c55e',   # 2: 緑
    30: '#facc15',   # 3: 黄
    40: '#f97316',   # 4: 橙
    45: '#ef4444',   # 5弱: 赤
    50: '#dc2626',   # 5強
    55: '#9333ea',   # 6弱: 紫
    60: '#7c3aed',   # 6強
    70: '#1e1b4b',   # 7: 濃紺
}

# ===== 自局トリガログ読み込み =====
def load_trigger_hhmm_set(year: int, month: int) -> set[str]:
    """trigger_log.jsonl から指定年月の検出時刻を「YYYY-MM-DD HH:MM」のセットで返す。"""
    result: set[str] = set()
    if not TRIGGER_LOG.exists():
        return result
    prefix = f'{year}-{month:02d}-'
    for line in TRIGGER_LOG.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            date_str = d.get('date', '')
            ts       = d.get('ts', '')          # HH:MM:SS
            if not date_str.startswith(prefix):
                continue
            hhmm = ts[:5]                       # HH:MM
            result.add(f'{date_str} {hhmm}')
        except Exception:
            pass
    return result


# ===== キャッシュ読み込み =====
CACHE_DIR = BASE_DIR / 'data' / 'p2p_cache'

def _load_from_cache(year: int, month: int) -> list[dict]:
    """キャッシュJSONLからデータを読み込む。"""
    path = CACHE_DIR / f'{year}{month:02d}.jsonl'
    if not path.exists():
        return []
    quakes = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            rec['dt'] = datetime.datetime.strptime(rec['time'][:16], '%Y/%m/%d %H:%M')
            quakes.append(rec)
        except Exception:
            pass
    quakes.sort(key=lambda q: q['dt'])
    return quakes


# ===== P2P APIからデータ取得（キャッシュなし時のフォールバック）=====
def _fetch_from_api(year: int, month: int) -> list[dict]:
    """APIから直接取得する。キャッシュが存在しない場合のフォールバック。"""
    first    = datetime.datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    last     = datetime.datetime(year, month, last_day, 23, 59, 59)
    seen: set[str] = set()
    quakes: list[dict] = []
    offset = 0

    while True:
        url = f'https://api.p2pquake.net/v2/history?codes=551&limit=100&offset={offset}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f'[WARN] API取得失敗 offset={offset}: {e}')
            break
        if not batch:
            break
        exhausted = False
        for eq in batch:
            info  = eq.get('earthquake', {})
            t_str = info.get('time', '')
            if not t_str:
                continue
            try:
                dt = datetime.datetime.strptime(t_str[:16], '%Y/%m/%d %H:%M')
            except ValueError:
                continue
            if dt < first:
                exhausted = True
                break
            if dt > last:
                continue
            hypo  = info.get('hypocenter', {})
            lat   = hypo.get('latitude',  None)
            lon   = hypo.get('longitude', None)
            name  = hypo.get('name', '')
            if not name or lat is None or lon is None:
                continue
            key = f"{t_str[:16]}_{name}"
            if key in seen:
                continue
            seen.add(key)
            quakes.append({
                'id':    eq.get('id', ''),
                'time':  t_str,
                'dt':    dt,
                'name':  name,
                'lat':   lat,
                'lon':   lon,
                'mag':   hypo.get('magnitude', -1),
                'depth': hypo.get('depth',     -1),
                'scale': info.get('maxScale',  -1),
            })
        if exhausted:
            break
        offset += 100
        time.sleep(0.3)

    quakes.sort(key=lambda q: q['dt'])
    return quakes


def fetch_p2p(year: int, month: int) -> list[dict]:
    """キャッシュ優先でデータを取得。なければAPIから直接取得。"""
    cached = _load_from_cache(year, month)
    if cached:
        print(f'[INFO] キャッシュから読み込み: {len(cached)}件')
        return cached
    print(f'[INFO] キャッシュなし。APIから取得します...')
    return _fetch_from_api(year, month)


# ===== 震源地図生成（Matplotlib静的画像）=====
def make_epicenter_map(quakes: list[dict], year: int, month: int) -> str:
    """震源地図をPNGとしてbase64エンコードした文字列を返す。"""
    _setup_font()
    fig, ax = plt.subplots(figsize=(8, 9), facecolor='#f0f4f8')
    ax.set_facecolor('#d0e8f8')

    # 日本地図
    try:
        countries = gpd.read_file(str(COUNTRIES_SHP))
        provinces = gpd.read_file(str(PROVINCES_SHP))
        japan_prov = provinces[provinces['admin'] == 'Japan']
        countries.plot(ax=ax, color='#e8e0d0', edgecolor='#aaa', linewidth=0.5)
        japan_prov.plot(ax=ax, color='#e8e0d0', edgecolor='#999', linewidth=0.3)
    except Exception as e:
        print(f"[WARN] 地図データ読み込み失敗: {e}")

    ax.set_xlim(122, 148)
    ax.set_ylim(24, 46)

    # 震源プロット（マグニチュードでサイズ、震度で色）
    for q in quakes:
        lat, lon = q['lat'], q['lon']
        mag = q['mag'] if q['mag'] > 0 else 2.0
        scale = q['scale']
        color = SCALE_COLOR.get(scale, '#94a3b8')
        size  = max(10, (mag ** 2.5) * 3)
        ax.scatter(lon, lat, s=size, c=color, alpha=0.7,
                   edgecolors='#334155', linewidths=0.3, zorder=5)

    # 凡例（震度）
    legend_items = []
    shown_scales = sorted({q['scale'] for q in quakes if q['scale'] in SCALE_COLOR})
    for s in shown_scales:
        label = f'震度{SCALE_LABEL[s]}'
        legend_items.append(mpatches.Patch(color=SCALE_COLOR[s], label=label))
    if legend_items:
        ax.legend(handles=legend_items, loc='lower left', fontsize=8,
                  facecolor='white', edgecolor='#aaa', framealpha=0.9)

    # マグニチュード凡例
    for m, label in [(3, 'M3'), (4, 'M4'), (5, 'M5'), (6, 'M6')]:
        ax.scatter([], [], s=max(10, m**2.5 * 3), c='#94a3b8',
                   edgecolors='#334155', linewidths=0.3, label=label)
    ax.legend(
        handles=legend_items + [
            plt.scatter([], [], s=max(10, m**2.5*3), c='#94a3b8',
                        edgecolors='#334155', linewidths=0.3, label=f'M{m}')
            for m in [3, 4, 5, 6]
        ],
        loc='lower left', fontsize=8,
        facecolor='white', edgecolor='#aaa', framealpha=0.9
    )

    ax.set_xlabel('経度', fontsize=9)
    ax.set_ylabel('緯度', fontsize=9)
    ax.set_title(f'{year}年{month}月 震源分布（最大震度別）', fontsize=12, pad=8)
    ax.grid(True, linestyle='--', linewidth=0.4, alpha=0.5)
    ax.tick_params(labelsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ===== 統計集計 =====
def compute_stats(quakes: list[dict]) -> dict:
    total = len(quakes)
    if total == 0:
        return {}

    # 震度別カウント
    scale_count: dict[int, int] = collections.Counter(
        q['scale'] for q in quakes if q['scale'] in SCALE_LABEL
    )

    # 最大規模
    max_mag_q  = max(quakes, key=lambda q: q['mag'])
    max_scale_q = max(
        (q for q in quakes if q['scale'] in SCALE_LABEL),
        key=lambda q: q['scale'],
        default=None,
    )

    # 地域別カウント
    region_count: dict[str, int] = collections.Counter(q['name'] for q in quakes)
    top_regions = region_count.most_common(5)

    # M5以上
    m5_plus = [q for q in quakes if q['mag'] >= 5.0]

    # 深さ分布
    depths = [q['depth'] for q in quakes if q['depth'] >= 0]
    avg_depth = sum(depths) / len(depths) if depths else 0

    # 日別発生数
    daily: dict[int, int] = collections.Counter(q['dt'].day for q in quakes)
    peak_day = max(daily, key=daily.get)
    peak_day_count = daily[peak_day]

    # 群発地震候補（同一地域で3件以上）
    swarm_regions = {name: cnt for name, cnt in region_count.items() if cnt >= 3}

    # 週別発生数（1〜7日=第1週 …）
    weekly: dict[int, int] = collections.Counter((q['dt'].day - 1) // 7 + 1 for q in quakes)

    return {
        'total':          total,
        'scale_count':    dict(sorted(scale_count.items())),
        'max_mag_q':      max_mag_q,
        'max_scale_q':    max_scale_q,
        'top_regions':    top_regions,
        'm5_plus':        m5_plus,
        'avg_depth':      avg_depth,
        'peak_day':       peak_day,
        'peak_day_count': peak_day_count,
        'daily':          dict(daily),
        'swarm_regions':  swarm_regions,
        'weekly':         dict(weekly),
    }


# ===== 地域の地方分類 =====
REGION_AREA = {
    '北海道': '北海道', '道東': '北海道', '道北': '北海道', '道南': '北海道',
    '十勝': '北海道', '釧路': '北海道', '根室': '北海道', '浦河': '北海道', '留萌': '北海道',
    '青森': '東北', '岩手': '東北', '宮城': '東北', '秋田': '東北',
    '山形': '東北', '福島': '東北', '三陸': '東北',
    '茨城': '関東', '栃木': '関東', '群馬': '関東', '埼玉': '関東',
    '千葉': '関東', '東京': '関東', '神奈川': '関東',
    '新潟': '中部', '富山': '中部', '石川': '中部', '福井': '中部',
    '山梨': '中部', '長野': '中部', '岐阜': '中部', '静岡': '中部', '愛知': '中部',
    '三重': '近畿', '滋賀': '近畿', '京都': '近畿', '大阪': '近畿',
    '兵庫': '近畿', '奈良': '近畿', '和歌山': '近畿', '紀伊': '近畿',
    '鳥取': '中国', '島根': '中国', '岡山': '中国', '広島': '中国', '山口': '中国',
    '徳島': '四国', '香川': '四国', '愛媛': '四国', '高知': '四国',
    '豊後': '九州', '福岡': '九州', '佐賀': '九州', '長崎': '九州',
    '熊本': '九州', '大分': '九州', '宮崎': '九州', '鹿児島': '九州',
    'トカラ': '南西諸島', '奄美': '南西諸島', '沖縄': '南西諸島',
    '西表': '南西諸島', '与那国': '南西諸島', '石垣': '南西諸島',
    '伊豆': '伊豆・小笠原', '小笠原': '伊豆・小笠原', '硫黄島': '伊豆・小笠原',
    '父島': '伊豆・小笠原',
}

def _area_of(name: str) -> str:
    for key, area in REGION_AREA.items():
        if key in name:
            return area
    return 'その他'

def _notable_event_comment(q: dict) -> str:
    """M5以上の注目地震に対する1行コメントを返す。"""
    name  = q['name']
    mag   = q['mag']
    scale = SCALE_LABEL.get(q['scale'], '不明')
    depth = q['depth']
    area  = _area_of(name)

    if mag >= 6.0:
        if area == '東北':
            return '東北太平洋側のプレート境界付近で発生したM6クラスの地震。2011年以降も活動が続くエリアで、広域に揺れが伝わりました。'
        if area == '南西諸島':
            return '南西諸島弧のプレート沈み込み帯で発生したM6クラス。島嶼部では局地的に強い揺れが記録されました。'
        if area == '伊豆・小笠原':
            return '伊豆・小笠原弧のプレート境界付近の活動。遠方のため本土への揺れへの影響は限定的でした。'
        return f'M{mag}の大きな地震で、広域に揺れが伝わりました。'
    if mag >= 5.5:
        if depth <= 20:
            return f'浅い震源（深さ{depth}km）のため、震源地付近では最大震度{scale}の揺れが記録されました。'
        return f'深さ{depth}kmで発生し、広い範囲で揺れが感じられました。最大震度は{scale}です。'
    # M5.0〜5.4
    if area in ('東北', '北海道'):
        return f'東日本の太平洋側で頻発するタイプの地震。最大震度{scale}を記録しました。'
    if area == '南西諸島':
        return f'南西諸島周辺のプレート境界付近の活動。最大震度{scale}でした。'
    return f'最大震度{scale}を記録した注目地震です。'

def _region_characteristic(name: str, cnt: int, quakes_in_region: list[dict]) -> str:
    """地域名と件数から特徴コメントを生成する。"""
    area = _area_of(name)
    mags = [q['mag'] for q in quakes_in_region if q['mag'] > 0]
    max_m = max(mags) if mags else 0
    days  = sorted({q['dt'].day for q in quakes_in_region})

    # 群発性の判定（3日以内に3件以上）
    is_swarm = False
    for i in range(len(days)):
        window = [d for d in days if days[i] <= d <= days[i] + 3]
        if len(window) >= 3:
            is_swarm = True
            break

    if is_swarm:
        day_range = f'{min(days)}日〜{max(days)}日'
        swarm_str = f'{day_range}の間に集中して{cnt}回発生。群発地震的な動きが見られました。'
    else:
        swarm_str = f'月を通じて{cnt}件の地震が散発しました。'

    if area == '東北':
        return f'{swarm_str}東北太平洋側では2011年以降も定常的にM3〜4台の地震が続いており、今月も活動が継続しました。'
    if area == '中部' and '飛騨' in name:
        return f'{swarm_str}飛騨地方は活断層が密集するエリアで、群発地震が起きやすい地域です。'
    if area == '中部' and '長野' in name:
        return f'{swarm_str}長野北部は糸魚川－静岡構造線の近傍にあたり、内陸地震が繰り返し発生します。'
    if area == '南西諸島':
        return f'{swarm_str}南西諸島ではフィリピン海プレートの沈み込みに伴う地震活動が活発です。'
    if area == '関東':
        return f'{swarm_str}関東周辺はフィリピン海プレートと太平洋プレートが複雑に絡み合うエリアで、定常的に小規模地震が続きます。'
    if area == '北海道':
        return f'{swarm_str}北海道周辺では千島海溝・日本海溝沿いのプレート活動が継続しています。'
    return swarm_str


# ===== 文章解説生成 =====
def generate_commentary(quakes: list[dict], stats: dict, year: int, month: int) -> str:
    if not stats:
        return '<p>今月は有感地震の記録がありませんでした。</p>'

    total          = stats['total']
    max_mag_q      = stats['max_mag_q']
    max_scale_q    = stats['max_scale_q']
    top_regions    = stats['top_regions']
    m5_plus        = stats['m5_plus']
    avg_depth      = stats['avg_depth']
    peak_day       = stats['peak_day']
    peak_day_count = stats['peak_day_count']
    scale_count    = stats['scale_count']
    swarm_regions  = stats['swarm_regions']

    # ── 活動レベル ──
    if total >= 80:
        activity_level = '非常に活発'
        activity_comment = '例年と比較しても地震活動が目立つ月でした。'
    elif total >= 50:
        activity_level = '活発'
        activity_comment = '複数のエリアで断続的な活動が続きました。'
    elif total >= 30:
        activity_level = 'やや活発'
        activity_comment = '平常の範囲内ですが、特定地域での集中が見られました。'
    else:
        activity_level = '平常的'
        activity_comment = '全体的に落ち着いた1ヶ月でした。'

    # ── 全体像 ──
    max_scale_str  = SCALE_LABEL.get(max_scale_q['scale'], '不明') if max_scale_q else '記録なし'
    section_overview = f"""
<h2>全体像</h2>
<p>
{year}年{month}月の有感地震は計<strong>{total}件</strong>記録されました。
活動レベルは<strong>{activity_level}</strong>と評価されます。{activity_comment}
最も地震が集中した日は<strong>{peak_day}日</strong>で、1日に<strong>{peak_day_count}件</strong>の有感地震が発生しました。
今月の最大震度は<strong>震度{max_scale_str}</strong>です。
</p>
"""

    # ── 注目イベント（M5以上を規模順に） ──
    if m5_plus:
        notable_items = ''
        for q in sorted(m5_plus, key=lambda q: q['mag'], reverse=True):
            dt    = q['dt']
            label = f"{dt.month}/{dt.day} {q['name']} M{q['mag']}・最大震度{SCALE_LABEL.get(q['scale'], '?')}"
            comment = _notable_event_comment(q)
            notable_items += f'<dt><strong>{label}</strong></dt><dd>{comment}</dd>\n'
        section_notable = f'<h2>注目イベント</h2><dl>{notable_items}</dl>'
    else:
        section_notable = '<h2>注目イベント</h2><p>今月はM5以上の地震は発生しませんでした。</p>'

    # ── 地域別活動傾向 ──
    region_rows = ''
    for name, cnt in top_regions:
        region_quakes = [q for q in quakes if q['name'] == name]
        char = _region_characteristic(name, cnt, region_quakes)
        region_rows += f'<tr><td><strong>{name}</strong></td><td>{char}</td></tr>\n'

    section_region = f"""
<h2>地域別の活動傾向</h2>
<table>
<thead><tr><th style="width:18%">地域</th><th>特徴</th></tr></thead>
<tbody>{region_rows}</tbody>
</table>
"""

    # ── 総括 ──
    # 活動が目立ったエリアを地方単位で集約
    area_count: dict[str, int] = collections.Counter(
        _area_of(q['name']) for q in quakes
    )
    top_areas = [a for a, _ in area_count.most_common(3) if a != 'その他']
    areas_str = '・'.join(top_areas) if top_areas else '全国各地'

    # 深さコメント
    if avg_depth < 20:
        depth_comment = '浅発地震が中心で、震源地周辺では局所的に強い揺れが生じやすい状況でした。'
    elif avg_depth < 60:
        depth_comment = '震源深さは浅〜中程度が中心で、比較的広い範囲に揺れが伝わりやすい状況でした。'
    else:
        depth_comment = '深発地震が多く含まれ、震源から遠距離でも揺れが感じられるケースがありました。'

    # 群発地震言及
    swarm_names = [n for n, c in swarm_regions.items() if c >= 4]
    if swarm_names:
        swarm_note = f'また、{swarm_names[0]}では群発的な動きが目立ちました。'
    else:
        swarm_note = ''

    section_summary = f"""
<h2>総括</h2>
<p>
今月は<strong>{areas_str}</strong>での活動が特に目立ちました。
{depth_comment}
{swarm_note}
関東周辺は震度1〜3程度の小規模地震が平常ペースで続いている状況です。
引き続き、各地域の活動推移に注意が必要です。
</p>
"""

    return section_overview + section_notable + section_region + section_summary


# ===== 日別グラフ生成 =====
def make_daily_chart(quakes: list[dict], year: int, month: int) -> str:
    _setup_font()
    last_day = calendar.monthrange(year, month)[1]
    days = list(range(1, last_day + 1))
    daily_count = collections.Counter(q['dt'].day for q in quakes)
    counts = [daily_count.get(d, 0) for d in days]

    fig, ax = plt.subplots(figsize=(10, 3), facecolor='#f8fafc')
    ax.set_facecolor('#f8fafc')
    ax.bar(days, counts, color='#3b82f6', alpha=0.8, width=0.7)
    ax.set_xlabel('日', fontsize=9)
    ax.set_ylabel('件数', fontsize=9)
    ax.set_title(f'{year}年{month}月 日別有感地震発生数', fontsize=11)
    ax.set_xticks(days)
    ax.tick_params(labelsize=8)
    ax.grid(axis='y', linestyle='--', linewidth=0.4, alpha=0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ===== マグニチュード分布グラフ =====
def make_mag_chart(quakes: list[dict], year: int, month: int) -> str:
    _setup_font()
    mags = [q['mag'] for q in quakes if q['mag'] > 0]
    if not mags:
        return ''

    bins = [m / 10 for m in range(10, 80, 5)]
    fig, ax = plt.subplots(figsize=(7, 3), facecolor='#f8fafc')
    ax.set_facecolor('#f8fafc')
    ax.hist(mags, bins=bins, color='#f97316', alpha=0.85, edgecolor='#7c3aed', linewidth=0.4)
    ax.set_xlabel('マグニチュード', fontsize=9)
    ax.set_ylabel('件数', fontsize=9)
    ax.set_title(f'{year}年{month}月 マグニチュード分布', fontsize=11)
    ax.tick_params(labelsize=8)
    ax.grid(axis='y', linestyle='--', linewidth=0.4, alpha=0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ===== 地震一覧テーブルHTML =====
def make_table(quakes: list[dict], detected_hhmm: set[str] | None = None) -> str:
    detected_hhmm = detected_hhmm or set()
    rows = []
    for q in reversed(quakes):
        scale_str = SCALE_LABEL.get(q['scale'], '-')
        color     = SCALE_COLOR.get(q['scale'], '#94a3b8')
        mag_str   = f"M{q['mag']}" if q['mag'] > 0 else '-'
        dep_str   = f"{q['depth']}km" if q['depth'] >= 0 else '-'

        # P2P時刻 "YYYY/MM/DD HH:MM" → "YYYY-MM-DD HH:MM" に正規化して照合
        p2p_time  = q['time'][:16]   # "YYYY/MM/DD HH:MM"
        norm_key  = p2p_time.replace('/', '-')
        detected  = norm_key in detected_hhmm
        det_cell  = '<td class="det-yes" title="自局で検出">📡</td>' if detected \
                    else '<td class="det-no">－</td>'

        rows.append(
            f'<tr>'
            f'<td>{p2p_time}</td>'
            f'<td>{q["name"]}</td>'
            f'<td>{mag_str}</td>'
            f'<td>{dep_str}</td>'
            f'<td><span class="scale-badge" style="background:{color}">'
            f'震度{scale_str}</span></td>'
            f'{det_cell}'
            f'</tr>'
        )
    return '<table id="quake-table"><thead><tr>'  \
           '<th>日時</th><th>震源地</th><th>M</th><th>深さ</th><th>最大震度</th>'  \
           '<th title="自局(AM.R38DC)での検出">自局検出</th>'  \
           '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'


# ===== HTMLレポート組み立て =====
def build_html(year: int, month: int, quakes: list[dict], stats: dict,
               detected_hhmm: set[str] | None = None) -> str:
    title = f'{year}年{month}月 月次地震レポート'
    map_b64       = make_epicenter_map(quakes, year, month)
    daily_b64     = make_daily_chart(quakes, year, month)
    mag_b64       = make_mag_chart(quakes, year, month)
    commentary    = generate_commentary(quakes, stats, year, month)
    table_html    = make_table(quakes, detected_hhmm)
    det_count     = sum(
        1 for q in quakes
        if q['time'][:16].replace('/', '-') in (detected_hhmm or set())
    )
    generated_at  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    yyyymm = f'{year}{month:02d}'
    ogp_url = f'https://masakai.github.io/earthQuake/reports/ogp_{yyyymm}.png'
    page_url = f'https://masakai.github.io/earthQuake/reports/report_{yyyymm}.html'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="有感地震{stats.get('total', 0)}件・最大M{stats['max_mag_q']['mag'] if stats.get('max_mag_q') else '-'}・最大震度{SCALE_LABEL.get(stats['max_scale_q']['scale'], '-') if stats.get('max_scale_q') else '-'} | AM.R38DC（静岡県三島市）による私的観測記録">
<meta property="og:url" content="{page_url}">
<meta property="og:image" content="{ogp_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="有感地震{stats.get('total', 0)}件・最大M{stats['max_mag_q']['mag'] if stats.get('max_mag_q') else '-'}・最大震度{SCALE_LABEL.get(stats['max_scale_q']['scale'], '-') if stats.get('max_scale_q') else '-'} | AM.R38DC（静岡県三島市）による私的観測記録">
<meta name="twitter:image" content="{ogp_url}">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background:#f1f5f9; color:#1e293b; margin:0; padding:0; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 16px; }}
  header {{ background: #1e3a5f; color: #fff; padding: 24px 32px; border-radius: 12px; margin-bottom: 24px; }}
  header h1 {{ margin: 0 0 4px; font-size: 1.6em; }}
  header p {{ margin: 0; font-size: 0.9em; opacity: 0.8; }}
  .card {{ background: #fff; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); padding: 24px; margin-bottom: 20px; }}
  .card h2 {{ margin: 0 0 16px; font-size: 1.1em; color: #1e3a5f; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
  img.chart {{ width: 100%; border-radius: 6px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .stat-box {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; text-align: center; }}
  .stat-box .val {{ font-size: 2em; font-weight: bold; color: #1e3a5f; }}
  .stat-box .lbl {{ font-size: 0.8em; color: #64748b; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88em; }}
  th {{ background: #1e3a5f; color: #fff; padding: 8px 10px; text-align: left; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #e2e8f0; }}
  tr:hover td {{ background: #f0f9ff; }}
  .scale-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; color: #fff; font-size: 0.85em; font-weight: bold; }}
  h2 {{ color: #1e3a5f; }}
  ul {{ padding-left: 1.4em; }}
  li {{ margin-bottom: 4px; }}
  .det-yes {{ text-align: center; font-size: 1.1em; }}
  .det-no  {{ text-align: center; color: #cbd5e1; }}
  .manual-commentary {{ background: #fffbeb; border: 2px dashed #f59e0b; border-radius: 10px; padding: 24px; margin-bottom: 20px; }}
  .manual-commentary h2 {{ color: #92400e; border-bottom: 2px solid #fde68a; padding-bottom: 8px; margin: 0 0 16px; font-size: 1.1em; }}
  .manual-commentary .placeholder {{ color: #b45309; font-style: italic; }}
  .notice {{ background: #fef9c3; border-left: 4px solid #eab308; border-radius: 6px; padding: 10px 16px; margin-bottom: 20px; font-size: 0.85em; color: #713f12; }}
  .notice a {{ color: #713f12; }}
  footer {{ text-align: center; color: #94a3b8; font-size: 0.8em; margin-top: 24px; }}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>{title}</h1>
  <p>データソース: P2P地震情報 (p2pquake.net) ／ 生成日時: {generated_at}</p>
</header>

<div class="notice">
  ⚠️ このレポートは個人観測点 AM.R38DC（静岡県三島市）による私的な記録・解説です。内容の正確性を保証するものではありません。
  地震情報の正式な発表は <a href="https://www.jma.go.jp/jma/index.html" target="_blank" rel="noopener">気象庁</a> をご参照ください。
</div>

<div class="stats-grid">
  <div class="stat-box"><div class="val">{stats.get('total', 0)}</div><div class="lbl">有感地震 総件数</div></div>
  <div class="stat-box"><div class="val">{len(stats.get('m5_plus', []))}</div><div class="lbl">M5以上の地震</div></div>
  <div class="stat-box"><div class="val">M{stats['max_mag_q']['mag'] if stats.get('max_mag_q') else '-'}</div><div class="lbl">最大マグニチュード</div></div>
  <div class="stat-box"><div class="val">震度{SCALE_LABEL.get(stats['max_scale_q']['scale'], '-') if stats.get('max_scale_q') else '-'}</div><div class="lbl">最大震度</div></div>
  <div class="stat-box"><div class="val">{stats.get('avg_depth', 0):.0f}km</div><div class="lbl">平均震源深さ</div></div>
  <div class="stat-box"><div class="val">{det_count}</div><div class="lbl">自局検出 (AM.R38DC)</div></div>
</div>

<div class="card">
  <h2>震源分布図</h2>
  <img class="chart" src="data:image/png;base64,{map_b64}" alt="震源地図">
</div>

<div class="card">
  {commentary}
</div>

<div class="manual-commentary" id="manual-commentary">
  <h2>解説・総評</h2>
  <p class="placeholder">※ ここに手動で解説を記入してください。<br>
  （このセクションのHTMLを直接編集するか、テキストエディタで <!-- COMMENTARY --> タグを置換してください）</p>
  <!-- COMMENTARY -->
</div>

<div class="card">
  <h2>日別発生件数</h2>
  <img class="chart" src="data:image/png;base64,{daily_b64}" alt="日別グラフ">
</div>

<div class="card">
  <h2>マグニチュード分布</h2>
  <img class="chart" src="data:image/png;base64,{mag_b64}" alt="M分布グラフ">
</div>

<div class="card">
  <h2>地震一覧（新しい順）</h2>
  {table_html}
</div>

<footer>AM.R38DC 月次地震レポート ／ {generated_at} 生成</footer>
</div>
</body>
</html>
"""


# ===== メイン =====
def main():
    ap = argparse.ArgumentParser(description='月次地震レポート生成')
    ap.add_argument('year',  type=int, nargs='?', default=datetime.date.today().year)
    ap.add_argument('month', type=int, nargs='?', default=datetime.date.today().month)
    args = ap.parse_args()

    year, month = args.year, args.month
    print(f'[INFO] {year}年{month}月のデータを取得中...')
    quakes = fetch_p2p(year, month)
    print(f'[INFO] {len(quakes)}件取得')

    if not quakes:
        print('[WARN] データがありません。終了します。')
        sys.exit(1)

    stats = compute_stats(quakes)
    detected_hhmm = load_trigger_hhmm_set(year, month)
    print(f'[INFO] 自局トリガ検出: {len(detected_hhmm)}件（{year}年{month}月）')
    print('[INFO] HTMLレポートを生成中...')
    html  = build_html(year, month, quakes, stats, detected_hhmm)

    out_path = OUT_DIR / f'report_{year}{month:02d}.html'
    out_path.write_text(html, encoding='utf-8')
    print(f'[INFO] 出力完了: {out_path}')


if __name__ == '__main__':
    main()
