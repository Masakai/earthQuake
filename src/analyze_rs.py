#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake 波形分析ツール

指定した時刻範囲のデータを Raspberry Shake 公式クラウド API から取得し、
震源地図・EHZ速度波形・EHZ FFT・合成加速度・STA/LTA・計測震度のグラフを生成する。

使い方:
    # JST で開始・終了を指定
    .venv/bin/python3 src/analyze_rs.py --start "2026-05-24 07:05:00" --end "2026-05-24 07:12:00"

    # 開始時刻 + 継続時間（秒）
    .venv/bin/python3 src/analyze_rs.py --start "2026-05-24 07:05:00" --duration 420

    # P2P履歴から地震を選んで自動設定（対話モード）
    .venv/bin/python3 src/analyze_rs.py --from-p2p

    # P2P履歴から指定インデックス（0始まり）を直接指定
    .venv/bin/python3 src/analyze_rs.py --from-p2p --p2p-index 1

    # キャッシュ済みデータを再利用
    .venv/bin/python3 src/analyze_rs.py --start "2026-05-24 07:05:00" --duration 420 --no-download

    # 縦線マーカーを追加（複数可）
    .venv/bin/python3 src/analyze_rs.py --start "2026-05-24 07:05:00" --duration 420 \\
        --marker "07:07:02 P波"

Copyright (c) 2026 Masanori Sakai
"""

import argparse
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.signal import butter, sosfilt
import geopandas as gpd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.gridspec as gridspec
import matplotlib.ticker

try:
    from obspy import read as obspy_read
except ImportError:
    sys.exit("[ERROR] obspy がインストールされていません。.venv/bin/pip install obspy を実行してください。")

# ===== 定数 =====
RS_FDSN_URL  = "https://data.raspberryshake.org/fdsnws/dataselect/1/query"
P2P_API_BASE = "https://api.p2pquake.net/v2/history"
NETWORK      = "AM"
LOCATION     = "00"
SENSITIVITY  = 387867.0   # counts/(m/s²)  R38DC実測値
JST          = timezone(timedelta(hours=9))
UTC          = timezone.utc

# 観測点座標（.env から読み込み）
_ENV = pathlib.Path(__file__).parent.parent / '.env'
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"\''))

STATION_LAT  = float(os.environ.get('STATION_LAT', '0.0'))
STATION_LON  = float(os.environ.get('STATION_LON', '0.0'))

# 自局Raspberry Shake SeedLink（公式FDSNにデータが無い発生直後のフォールバック取得先）
# 公式FDSNは発生から20〜30分遅れるため、その穴埋めに自局のリアルタイム配信を使う。
# デフォルトは自局固定IP。.env の RS_SEEDLINK_HOST / RS_SEEDLINK_PORT で上書き可能。
RS_SEEDLINK_HOST = os.environ.get('RS_SEEDLINK_HOST', '10.0.1.53')
RS_SEEDLINK_PORT = int(os.environ.get('RS_SEEDLINK_PORT', '18000'))

# HTTPS用SSLコンテキスト。
# python.org製Python(macOS)はOpenSSLのデフォルト証明書パスが未設定のことがあり、
# その場合 CERTIFICATE_VERIFY_FAILED でFDSN/P2P APIへのHTTPSが全滅する。
# certifi があればそのバンドルで検証コンテキストを作る。無ければ None（標準挙動）。
import ssl as _ssl
try:
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None

_ROOT = pathlib.Path(__file__).parent.parent
_NE_PROVINCES = _ROOT / 'data' / 'ne' / 'provinces' / 'ne_10m_admin_1_states_provinces.shp'
_NE_COUNTRIES = _ROOT / 'data' / 'ne' / 'countries' / 'ne_10m_admin_0_countries_jpn.shp'

_SRC = pathlib.Path(__file__).parent
sys.path.insert(0, str(_SRC))
from jma_intensity_realtime import apply_jma_filter_time, jma_scale_from_I


# ===== 日本語フォント =====
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


# ===== 時刻パース（JST → UTC） =====
def parse_jst(s: str) -> datetime:
    s = s.strip().replace('T', ' ')
    dt_jst = datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=JST)
    return dt_jst.astimezone(UTC)


# ===== P2P地震情報取得 =====
def _parse_p2p_entries(data: list) -> list:
    quakes = []
    for d in data:
        eq = d.get('earthquake', {})
        h  = eq.get('hypocenter', {})
        t_str = eq.get('time', '')
        try:
            t_jst = datetime.strptime(t_str, '%Y/%m/%d %H:%M:%S').replace(tzinfo=JST)
        except ValueError:
            continue
        quakes.append({
            'time_jst': t_jst,
            'name':      h.get('name', '不明'),
            'magnitude': h.get('magnitude', 0.0),
            'depth':     h.get('depth', 0),
            'latitude':  h.get('latitude', None),
            'longitude': h.get('longitude', None),
            'max_scale': eq.get('maxScale', -1),
        })
    return quakes


def fetch_p2p_quakes(limit: int = 20) -> list:
    url = P2P_API_BASE + '?' + urllib.parse.urlencode({'codes': 551, 'limit': limit})
    req = urllib.request.Request(url, headers={"User-Agent": "rs4d-analyze/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    return _parse_p2p_entries(data)


def fetch_p2p_quakes_by_date(date_str: str) -> list:
    """指定日（JST、YYYY-MM-DD）の地震をページングで全件取得する。"""
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        sys.exit(f"[ERROR] --p2p-date の形式が不正です（例: 2026-05-01）: {date_str}")

    batch = 100
    offset = 0
    result = []
    print(f"  {date_str} の地震を検索中...", end='', flush=True)
    while True:
        url = P2P_API_BASE + '?' + urllib.parse.urlencode(
            {'codes': 551, 'limit': batch, 'offset': offset}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "rs4d-analyze/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        if not data:
            break
        entries = _parse_p2p_entries(data)
        past_target = False
        for q in entries:
            d = q['time_jst'].date()
            if d == target:
                result.append(q)
            elif d < target:
                past_target = True
        print('.', end='', flush=True)
        if past_target:
            break
        if len(data) < batch:
            break
        offset += batch
    print(f" {len(result)}件")
    return result


# P2P maxScale の整数値→震度文字列
_SCALE_LABEL = {
    10: '1', 20: '2', 30: '3', 40: '4',
    45: '5弱', 50: '5強', 55: '6弱', 60: '6強', 70: '7',
}

def _scale_str(sc: int) -> str:
    return f"震度{_SCALE_LABEL.get(sc, '?')}" if sc in _SCALE_LABEL else '?'


def select_p2p_quake(quakes: list, index: int = None, min_scale: int = 0) -> dict:
    filtered = [q for q in quakes if q['max_scale'] >= min_scale] if min_scale > 0 else quakes
    if not filtered:
        sys.exit(f"[ERROR] 震度フィルタ（--p2p-min-scale）に合致する地震がありません。--p2p-limit を増やすか、フィルタを緩めてください。")

    print("\n== P2P地震履歴 ==")
    for i, q in enumerate(filtered):
        t = q['time_jst'].strftime('%Y-%m-%d %H:%M:%S')
        sc = q['max_scale']
        print(f"  [{i}] {t} JST  {q['name']}  M{q['magnitude']}  深さ{q['depth']}km  最大{_scale_str(sc)}")

    if index is not None:
        if not (0 <= index < len(filtered)):
            sys.exit(f"[ERROR] --p2p-index {index} は範囲外です（0〜{len(filtered)-1}）")
        return filtered[index]

    while True:
        try:
            s = input(f"\n番号を選択してください [0-{len(filtered)-1}]: ").strip()
            idx = int(s)
            if 0 <= idx < len(filtered):
                return filtered[idx]
        except (ValueError, EOFError):
            pass
        print("  有効な番号を入力してください。")


# ===== ステーション座標取得 =====
def fetch_station_coords(station: str) -> tuple[float, float] | None:
    """RS FDSN station API からステーションの緯度・経度を取得する。"""
    url = (
        f"https://data.raspberryshake.org/fdsnws/station/1/query"
        f"?network={NETWORK}&station={station}&level=station&format=text"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "rs4d-analyze/1.0"})
    lat, lon = None, None
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.split('|')
                if len(parts) >= 4:
                    # 複数エポックがある場合は最後の行（最新）を使う
                    lat, lon = float(parts[2]), float(parts[3])
    except Exception as e:
        print(f"[WARN] ステーション座標の取得に失敗しました: {e}")
        return None
    return (lat, lon) if lat is not None else None


# ===== MiniSEED ダウンロード =====
def download_channel_seedlink(station: str, channel: str, t_start: datetime, t_end: datetime,
                              out_path: pathlib.Path) -> bool:
    """自局Raspberry Shake の SeedLink から指定区間を取得して MiniSEED で保存する。

    公式FDSNにまだデータが無い発生直後の穴埋め用フォールバック。
    取得できたら out_path に書き出して True を返す。
    自局がLAN外で到達不能、またはデータが無い場合は False を返す（静かに諦める）。
    """
    try:
        from obspy import UTCDateTime
        from obspy.clients.seedlink.basic_client import Client
    except ImportError:
        return False
    # SeedLinkは「終端が未来の区間」を要求すると、その時刻のデータが届くまで
    # ブロックし続ける（Clientのtimeoutはこの待機には効かない）。発生直後の地震を
    # 長いdurationで解析すると終端が未来にかかりハングするため、現在時刻の数秒手前で
    # 終端をクランプする。未来のデータは存在しないので取れる範囲（=現在まで）で足りる。
    now_utc = datetime.now(UTC)
    safe_end = now_utc - timedelta(seconds=5)
    req_end = t_end if t_end <= safe_end else safe_end
    if req_end <= t_start:
        # 区間全体が未来＝まだ1サンプルも存在しない。フォールバック断念。
        print(f"\n  [INFO] {channel}: 区間が未来のためSeedLink取得不可", end=" ", flush=True)
        return False
    try:
        cli = Client(RS_SEEDLINK_HOST, port=RS_SEEDLINK_PORT, timeout=30)
        st = cli.get_waveforms(NETWORK, station, LOCATION, channel,
                               UTCDateTime(t_start), UTCDateTime(req_end))
    except Exception as e:
        # 自局に到達できない（LAN外実行など）場合はここに来る。フォールバック断念。
        print(f"\n  [INFO] {channel}: 自局SeedLink取得不可 ({e})", end=" ", flush=True)
        return False
    if not st or len(st) == 0:
        return False
    try:
        st.write(str(out_path), format='MSEED')
    except Exception as e:
        print(f"\n  [WARN] {channel}: SeedLink結果の書き出し失敗 ({e})", end=" ", flush=True)
        return False
    total = sum(tr.stats.npts for tr in st)
    print(f"[自局SeedLink] {out_path.stat().st_size:,} bytes ({total:,} samples)")
    return True


def download_channel(station: str, channel: str, t_start: datetime, t_end: datetime,
                     out_path: pathlib.Path):
    start_str = t_start.strftime('%Y-%m-%dT%H:%M:%S')
    end_str   = t_end.strftime('%Y-%m-%dT%H:%M:%S')
    url = (
        f"{RS_FDSN_URL}"
        f"?network={NETWORK}&station={station}&location={LOCATION}&channel={channel}"
        f"&starttime={start_str}&endtime={end_str}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "rs4d-analyze/1.0"})
    print(f"  {channel}: {start_str} → {end_str} ...", end=" ", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            data = resp.read()
    except Exception as e:
        print(f"\n  [WARN] {channel}: 公式FDSNダウンロード失敗 ({e})")
        data = b''
    if data:
        out_path.write_bytes(data)
        print(f"{len(data):,} bytes")
        return
    # 公式FDSNにデータが無い（発生直後など）→ 自局RSのSeedLinkにフォールバック
    print("公式FDSNにデータなし → 自局SeedLinkを試行 ...", end=" ", flush=True)
    if download_channel_seedlink(station, channel, t_start, t_end, out_path):
        return
    if out_path.exists():
        out_path.unlink()
    print("0 bytes (データなし・スキップ)")


# ===== STA/LTA =====
def compute_stalta(vec: np.ndarray, fs: float, sta_s: float, lta_s: float) -> np.ndarray:
    nsta = max(1, int(sta_s * fs))
    nlta = max(nsta + 1, int(lta_s * fs))
    sq = (vec - np.mean(vec)) ** 2
    cs = np.concatenate([[0.0], np.cumsum(sq)])
    N = len(vec)
    ratio = np.zeros(N)
    for i in range(nlta, N):
        # LTAはSTA区間を除いた直前区間（Withers et al. 1998 標準定義）
        lta_e = (cs[i - nsta] - cs[i - nlta]) / (nlta - nsta) + 1e-18
        sta_e = (cs[i] - cs[i - nsta]) / nsta + 1e-18
        ratio[i] = sta_e / lta_e
    return ratio


# ===== 計測震度スライド窓 =====
def compute_intensity_timeseries(a_gal: np.ndarray, fs: float, window_s: float = 90.0) -> np.ndarray:
    # JMA定義: 合計0.3秒以上 a を超える最大の a を求める
    # realtime.py の a_threshold_for_03s と同じ「合計0.3秒」基準
    # np.partition を2D行列に適用すると数GB の一時コピーが生じるため
    # stride=10サンプル(0.1s)でダウンサンプル計算し線形補間する
    k    = max(1, int(round(0.3 * fs)))
    nwin = int(window_s * fs)
    abs_a = np.abs(a_gal)
    N = len(abs_a)
    if N < k:
        return np.zeros(N)

    stride = max(1, int(fs * 0.1))  # 0.1秒ごとに1点計算
    calc_indices = range(k, N, stride)
    sparse_peaks = np.empty(len(calc_indices))
    for out_i, i in enumerate(calc_indices):
        win = abs_a[max(0, i - nwin):i]
        idx = len(win) - k
        sparse_peaks[out_i] = np.partition(win, idx)[idx]

    # 計算点のインデックスと全インデックスで線形補間
    calc_idx_arr = np.array(list(calc_indices), dtype=float)
    all_idx = np.arange(N, dtype=float)
    peaks_full = np.interp(all_idx, calc_idx_arr, sparse_peaks)
    peaks_full[:k] = 0.0  # データ不足区間はゼロ

    I_arr = np.zeros(N)
    mask = peaks_full > 0
    I_arr[mask] = 2.0 * np.log10(np.maximum(peaks_full[mask], 1e-10)) + 0.94
    return I_arr


# ===== スペクトログラム計算 =====
def compute_spectrogram(sig: np.ndarray, fs: float, nperseg: int = 256, overlap: float = 0.75):
    nperseg = min(nperseg, len(sig))
    step = max(1, int(nperseg * (1 - overlap)))
    win = np.hanning(nperseg)
    n_frames = max(1, (len(sig) - nperseg) // step + 1)
    freq = np.fft.rfftfreq(nperseg, d=1.0 / fs)
    S = np.zeros((len(freq), n_frames))
    for i in range(n_frames):
        seg = sig[i * step: i * step + nperseg]
        S[:, i] = np.abs(np.fft.rfft(seg * win)) * 2.0 / win.sum()
    t_frames = np.arange(n_frames) * step / fs
    return t_frames, freq, S


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


# ===== 震源地図 =====
def plot_map(ax, eq_lat, eq_lon, eq_name, eq_mag, eq_depth,
             sta_lat, sta_lon, dist_km, sta_name='R38DC'):
    ax.set_facecolor('#dce8f0')  # 海の色
    ax.tick_params(colors='#57606a', labelsize=8)
    for spine in ax.spines.values():
        spine.set_color('#d0d7de')

    if eq_lat is None or eq_lon is None:
        ax.text(0.5, 0.5, '震源座標なし', transform=ax.transAxes,
                color='#57606a', ha='center', va='center', fontsize=11)
        ax.set_title('震源地図', color='#1f2328', fontsize=11, pad=6)
        return

    # 表示範囲（震源と観測点を両方含む）
    lats = [eq_lat, sta_lat]
    lons = [eq_lon, sta_lon]
    margin_lat = max(abs(eq_lat - sta_lat) * 0.4, 1.5)
    margin_lon = max(abs(eq_lon - sta_lon) * 0.4, 1.5)
    lat_min, lat_max = min(lats) - margin_lat, max(lats) + margin_lat
    lon_min, lon_max = min(lons) - margin_lon, max(lons) + margin_lon

    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)

    # 白地図描画
    _load_map_data()
    bbox = (lon_min, lat_min, lon_max, lat_max)
    if _gdf_countries is not None:
        clipped = _gdf_countries.clip(bbox)
        clipped.plot(ax=ax, facecolor='#f5f5f0', edgecolor='#aaaaaa', lw=0.5, zorder=1)
    if _gdf_provinces is not None:
        clipped = _gdf_provinces.clip(bbox)
        clipped.plot(ax=ax, facecolor='#f5f5f0', edgecolor='#aaaaaa', lw=0.4, zorder=2)

    # 経度・緯度のグリッド目盛り
    lon_ticks = np.arange(np.ceil(lon_min), np.floor(lon_max) + 0.5, 1.0)
    lat_ticks = np.arange(np.ceil(lat_min), np.floor(lat_max) + 0.5, 1.0)
    ax.set_xticks(lon_ticks)
    ax.set_yticks(lat_ticks)
    ax.set_xticklabels([f'{v:.0f}°E' for v in lon_ticks], fontsize=7, color='#57606a')
    ax.set_yticklabels([f'{v:.0f}°N' for v in lat_ticks], fontsize=7, color='#57606a')
    ax.tick_params(colors='#57606a', labelsize=7)

    # グリッド
    ax.grid(color='#d0d7de', lw=0.4, ls=':', zorder=3)

    # 北矢印（左上内側）
    ax.annotate('N', xy=(0.07, 0.90), xytext=(0.07, 0.80),
                xycoords='axes fraction', textcoords='axes fraction',
                ha='center', color='#1f2328', fontsize=9, fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#1f2328', lw=1.5))

    # 震源→観測点の線
    ax.plot([eq_lon, sta_lon], [eq_lat, sta_lat],
            color='#57606a', lw=1.0, ls='--', zorder=4)

    # 震源（星）
    ax.scatter([eq_lon], [eq_lat], s=200, marker='*',
               color='#bc4c00', zorder=6, label=f'震源 M{eq_mag}')
    ax.text(eq_lon, eq_lat + margin_lat * 0.08, eq_name,
            color='#bc4c00', fontsize=8, ha='center', va='bottom', zorder=7)

    # 観測点（三角）
    ax.scatter([sta_lon], [sta_lat], s=80, marker='^',
               color='#0969da', zorder=6, label=f'観測点 AM.{sta_name}')
    ax.text(sta_lon, sta_lat - margin_lat * 0.1, sta_name,
            color='#0969da', fontsize=8, ha='center', va='top', zorder=7)

    # 距離ラベル（線の中点）
    mid_lat = (eq_lat + sta_lat) / 2
    mid_lon = (eq_lon + sta_lon) / 2
    ax.text(mid_lon, mid_lat, f' {dist_km:.0f}km',
            color='#57606a', fontsize=8, va='center', zorder=5)

    ax.legend(loc='lower right', fontsize=8,
              facecolor='#f6f8fa', edgecolor='#d0d7de', labelcolor='#1f2328')
    ax.set_title(
        f'震源地図  {eq_name}  M{eq_mag}  深さ{eq_depth}km',
        color='#1f2328', fontsize=11, pad=6
    )


# ===== メイングラフ描画 =====
def plot_analysis(
    times_jst, a_comb_gal, ratio_arr, I_arr,
    times_ehz, ehz_raw_dc, fs_ehz,
    sta_s, lta_s, trig_thr,
    title, out_path,
    markers=None,
    stable_offset_s=0.0,
    quake_info=None,
    sta_lat=None, sta_lon=None,
    available_en=None,
    sta_name='R38DC',
    gap_spans=None,
    stalta_src=None,
):
    _setup_font()

    if markers is None:
        markers = []
    if available_en is None:
        available_en = ['ENZ', 'ENN', 'ENE']
    if gap_spans is None:
        gap_spans = []

    N = len(a_comb_gal)
    fs_approx = N / max((times_jst[-1] - times_jst[0]).total_seconds(), 1.0)
    stable_idx = min(int(stable_offset_s * fs_approx), max(0, N - 1))

    a_search   = a_comb_gal[stable_idx:]
    r_search   = ratio_arr[stable_idx:]
    I_search   = I_arr[stable_idx:]
    I_peak     = float(I_search.max())
    r_peak     = float(r_search.max())
    a_peak     = float(a_search.max())
    scale_peak = jma_scale_from_I(I_peak)
    idx_ap     = int(np.argmax(a_search)) + stable_idx
    idx_rp     = int(np.argmax(r_search)) + stable_idx

    has_ehz = ehz_raw_dc is not None

    # レイアウト:
    #   左列: 震源地図（上）+ スペクトログラム（中）+ FFTグラフ（下）
    #   右列: EHZ波形 / 合成加速度 / STA/LTA / 計測震度（縦4段、sharex）
    n_right = 4 if has_ehz else 3
    fig = plt.figure(figsize=(18, 4 * n_right))
    fig.patch.set_facecolor('#ffffff')

    gs = gridspec.GridSpec(
        n_right, 2,
        width_ratios=[1, 2],
        hspace=0.55, wspace=0.30,
        left=0.08, right=0.97, top=0.93, bottom=0.10,
    )

    # 左列を3段（地図・スペクトログラム・FFT）に分割
    gs_left = gridspec.GridSpecFromSubplotSpec(
        3, 1, subplot_spec=gs[:, 0],
        hspace=0.55,
        height_ratios=[n_right, n_right, n_right],
    )
    ax_map  = fig.add_subplot(gs_left[0])
    ax_sgram = fig.add_subplot(gs_left[1])
    ax_fft  = fig.add_subplot(gs_left[2])

    # 右列: 各パネル独立（全パネルに時間軸ラベルを表示するため sharex を使わない）
    ax_right = []
    for i in range(n_right):
        ax_right.append(fig.add_subplot(gs[i, 1]))

    for ax in [ax_map, ax_sgram, ax_fft] + ax_right:
        ax.set_facecolor('#f6f8fa')
        ax.tick_params(colors='#57606a', labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#d0d7de')

    # ===== 震源地図 =====
    dist_km = 0.0
    if quake_info and quake_info.get('latitude') is not None and sta_lat is not None:
        dlat = quake_info['latitude'] - sta_lat
        dlon = quake_info['longitude'] - sta_lon
        dist_km = np.sqrt((dlat * 111.0)**2 + (dlon * 111.0 * np.cos(np.radians(sta_lat)))**2)
    plot_map(
        ax_map,
        eq_lat=quake_info.get('latitude') if quake_info else None,
        eq_lon=quake_info.get('longitude') if quake_info else None,
        eq_name=quake_info.get('name', '') if quake_info else '',
        eq_mag=quake_info.get('magnitude', 0) if quake_info else 0,
        eq_depth=quake_info.get('depth', 0) if quake_info else 0,
        sta_lat=sta_lat, sta_lon=sta_lon,
        dist_km=dist_km,
        sta_name=sta_name,
    )

    # ===== EHZ スペクトログラム =====
    if has_ehz and len(ehz_raw_dc) >= 8:
        nperseg = min(512, max(4, len(ehz_raw_dc) // 8))
        nperseg = min(nperseg, len(ehz_raw_dc))
        t_fr, freq, S = compute_spectrogram(ehz_raw_dc, fs_ehz, nperseg=nperseg)
        t0_jst = times_ehz[0]
        t_abs = [t0_jst + timedelta(seconds=float(t)) for t in t_fr]
        f_min, f_max = 0.5, fs_ehz / 2
        fmask = (freq >= f_min) & (freq <= f_max)
        S_db = 20 * np.log10(np.clip(S[fmask, :], 1e-10, None))
        freq_plot = freq[fmask]
        t_num = mdates.date2num(t_abs)
        t_edges = np.concatenate([[t_num[0] - (t_num[1]-t_num[0])/2],
                                   (t_num[:-1] + t_num[1:]) / 2,
                                   [t_num[-1] + (t_num[-1]-t_num[-2])/2]])
        f_edges = np.concatenate([[freq_plot[0]], (freq_plot[:-1]+freq_plot[1:])/2, [freq_plot[-1]]])
        vmin = np.percentile(S_db, 5)
        vmax = np.percentile(S_db, 99)
        ax_sgram.pcolormesh(t_edges, f_edges, S_db, cmap='inferno',
                            vmin=vmin, vmax=vmax, shading='flat')
        ax_sgram.set_yscale('log')
        ax_sgram.set_ylim(f_min, f_max)
        ax_sgram.set_ylabel('Hzの周波数', color='#57606a', fontsize=8)
        ax_sgram.set_title('EHZ スペクトログラム', color='#1f2328', fontsize=11, pad=6)
        ax_sgram.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
        ax_sgram.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax_sgram.set_yticks([0.5, 1, 2, 4, 8, 10, 20])
        ax_sgram.tick_params(colors='#57606a', labelsize=7)
        ax_sgram.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=JST))
        _sgram_dur = (times_jst[-1] - times_jst[0]).total_seconds()
        _sgram_interval = max(60, int(_sgram_dur / 5 / 60) * 60)
        ax_sgram.xaxis.set_major_locator(mdates.SecondLocator(interval=_sgram_interval))
        ax_sgram.set_xlim(times_jst[0], times_jst[-1])
        ax_sgram.set_xlabel('時刻 (JST)', color='#57606a', fontsize=8)
        ax_sgram.tick_params(axis='x', labelbottom=True, labelsize=8)
        for t_m, lbl in markers:
            ax_sgram.axvline(t_m, color='#8250df', lw=1.2, ls='--', alpha=0.8, zorder=5)
    else:
        ax_sgram.text(0.5, 0.5, 'EHZデータなし\n（または短すぎます）', transform=ax_sgram.transAxes,
                      color='#57606a', ha='center', va='center')
        ax_sgram.set_title('EHZ スペクトログラム', color='#1f2328', fontsize=11, pad=6)

    # ===== EHZ FFT（振幅スペクトル折れ線） =====
    if has_ehz and len(ehz_raw_dc) >= 2:
        N = len(ehz_raw_dc)
        win = np.hanning(N)
        spec = np.abs(np.fft.rfft(ehz_raw_dc * win)) * 2.0 / win.sum()
        freq_fft = np.fft.rfftfreq(N, d=1.0 / fs_ehz)
        mask = freq_fft >= 0.1
        ax_fft.semilogy(freq_fft[mask], spec[mask], color='#1a7f37', lw=0.8, alpha=0.9)
        ax_fft.set_xlabel('周波数 [Hz]', color='#57606a', fontsize=8)
        ax_fft.set_ylabel('振幅 [counts]', color='#57606a', fontsize=8)
        ax_fft.set_title('EHZ 振幅スペクトル（FFT）', color='#1f2328', fontsize=11, pad=6)
        ax_fft.set_xlim(0.1, fs_ehz / 2)
        ax_fft.axvline(1.0,  color='#57606a', lw=0.8, ls=':', alpha=0.7)
        ax_fft.axvline(10.0, color='#57606a', lw=0.8, ls=':', alpha=0.7)
        ax_fft.text(1.0,  spec[mask].max() * 0.5, ' 1Hz',  color='#57606a', fontsize=8)
        ax_fft.text(10.0, spec[mask].max() * 0.5, ' 10Hz', color='#57606a', fontsize=8)
        ax_fft.grid(color='#d0d7de', lw=0.4, ls=':')
        ax_fft.tick_params(colors='#57606a', labelsize=7)
    else:
        ax_fft.text(0.5, 0.5, 'EHZデータなし', transform=ax_fft.transAxes,
                    color='#57606a', ha='center', va='center')
        ax_fft.set_title('EHZ 振幅スペクトル（FFT）', color='#1f2328', fontsize=11, pad=6)

    # ===== 右列: 時系列パネル =====
    def draw_markers(ax):
        for t_m, lbl in markers:
            ax.axvline(t_m, color='#8250df', lw=1.2, ls='--', alpha=0.8, zorder=5)
            ylim = ax.get_ylim()
            ax.text(t_m, ylim[1] - (ylim[1] - ylim[0]) * 0.05, f' {lbl}',
                    color='#8250df', fontsize=8, va='top', zorder=6)

    def draw_gaps(ax):
        for gap_start, gap_end, dur_s in gap_spans:
            ax.axvspan(gap_start, gap_end, color='#cf222e', alpha=0.15, zorder=3)
            ylim = ax.get_ylim()
            ax.text(gap_start, ylim[1] - (ylim[1] - ylim[0]) * 0.05,
                    f' ⚠ギャップ{dur_s:.1f}s',
                    color='#cf222e', fontsize=8, va='top', zorder=6)

    panel = 0

    # EHZ 速度波形
    if has_ehz:
        peak = np.abs(ehz_raw_dc).max()
        ehz_norm = ehz_raw_dc / peak if peak > 0 else ehz_raw_dc
        ax = ax_right[panel]; panel += 1
        ax.plot(times_ehz, ehz_norm, color='#1a7f37', lw=0.6, alpha=0.9)
        ax.set_ylabel('EHZ 正規化振幅', color='#57606a')
        ax.set_title('EHZ 速度波形（DC除去・正規化）', color='#1f2328', fontsize=11, pad=6)
        ax.axhline(0, color='#d0d7de', lw=0.5)
        ax.set_ylim(-1.3, 1.3)
        draw_markers(ax)
        draw_gaps(ax)

    # 合成加速度
    ax = ax_right[panel]; panel += 1
    ax.plot(times_jst, a_comb_gal, color='#0969da', lw=0.6, alpha=0.9)
    ax.set_ylabel('加速度 [gal]', color='#57606a')
    ax.set_title(f'合成加速度（JMAフィルタ後、{"+".join(available_en)}）', color='#1f2328', fontsize=11, pad=6)
    ax.annotate(f'最大 {a_peak:.3f} gal',
                xy=(times_jst[idx_ap], a_comb_gal[idx_ap]),
                xytext=(10, 5), textcoords='offset points',
                color='#0969da', fontsize=9,
                arrowprops=dict(arrowstyle='->', color='#0969da', lw=0.8))
    draw_markers(ax)
    draw_gaps(ax)

    # STA/LTA
    ax = ax_right[panel]; panel += 1
    ax.plot(times_jst, ratio_arr, color='#0969da', lw=1.2, label='STA/LTA')
    ax.axhline(trig_thr, color='#bc4c00', lw=1.5, ls='--', label=f'閾値 {trig_thr}')
    ax.fill_between(times_jst, 0, ratio_arr, where=ratio_arr >= trig_thr,
                    color='#bc4c00', alpha=0.3, label='トリガ')
    ax.set_ylabel('STA/LTA', color='#57606a')
    src_label = stalta_src if stalta_src else "+".join(available_en)
    ax.set_title(f'STA/LTA比  ({src_label}  STA={sta_s}s / LTA={lta_s}s)', color='#1f2328', fontsize=11, pad=6)
    ax.legend(loc='upper right', fontsize=9,
              facecolor='#f6f8fa', edgecolor='#d0d7de', labelcolor='#1f2328')
    ax.set_ylim(bottom=0)
    ax.annotate(f'最大 {r_peak:.2f}',
                xy=(times_jst[idx_rp], ratio_arr[idx_rp]),
                xytext=(10, 5), textcoords='offset points',
                color='#bc4c00', fontsize=9,
                arrowprops=dict(arrowstyle='->', color='#bc4c00', lw=0.8))
    draw_markers(ax)
    draw_gaps(ax)

    # 計測震度
    ax = ax_right[panel]; panel += 1
    ax.plot(times_jst, I_arr, color='#bc4c00', lw=1.8, label='計測震度 I')
    y_bottom = min(I_arr.min() - 0.3, -1.0)
    y_top    = max(I_arr.max() + 0.5, 1.0)
    for thresh, lbl in [(0.5, '1'), (1.5, '2'), (2.5, '3'), (3.5, '4'), (4.5, '5弱'),
                        (5.0, '5強'), (5.5, '6弱'), (6.0, '6強'), (6.5, '7')]:
        if y_bottom <= thresh <= y_top:
            ax.axhline(thresh, color='#d0d7de', lw=0.8, ls=':')
            ax.text(times_jst[30], thresh + 0.05, f'震度{lbl}',
                    color='#57606a', fontsize=8, va='bottom')
    ax.set_ylabel('計測震度 I', color='#57606a')
    ax.set_title(f'計測震度（窓=90s）  最大: I={I_peak:.2f}（震度{scale_peak}）',
                 color='#1f2328', fontsize=11, pad=6)
    ax.legend(loc='upper right', fontsize=9,
              facecolor='#f6f8fa', edgecolor='#d0d7de', labelcolor='#1f2328')
    ax.set_ylim(bottom=y_bottom, top=y_top)
    draw_markers(ax)
    draw_gaps(ax)

    # X軸（全パネルにラベル表示）
    duration_s = (times_jst[-1] - times_jst[0]).total_seconds()
    interval = max(10, int(duration_s / 10 / 10) * 10)
    for ax in ax_right:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S', tz=JST))
        ax.xaxis.set_major_locator(mdates.SecondLocator(interval=interval))
        ax.set_xlabel('時刻 (JST)', color='#57606a', fontsize=8)
        ax.tick_params(axis='x', labelbottom=True, labelsize=8)
        ax.xaxis.set_tick_params(which='both', labelbottom=True)

    dist_str = f'  /  震源距離 {dist_km:.0f}km' if dist_km > 0 else ''

    # Si-Midorikawa (1999) 距離減衰式の逆算で推定M
    # log10(a[gal]) = 0.61M - 1.73*log10(r[km]) - 0.00030*r + 0.167
    # 地殻内地震向け係数。プレート境界地震（駿河トラフ等）には誤差±0.5程度。
    est_M_str = ''
    if dist_km > 0 and a_peak > 0:
        r = dist_km
        est_M = (np.log10(a_peak) - 0.167 + 1.73 * np.log10(r) + 0.00030 * r) / 0.61
        official_mag = quake_info.get('magnitude', 0) if quake_info else 0
        # Si-Midorikawa (1999) 式は r<200km 程度の近地強震向けであり、遠地は誤差が大きい
        caveat = '参考値' if dist_km > 200 else '±0.5程度'
        est_M_str = f'  /  推定M {est_M:.1f}（公式M{official_mag}、{caveat}）'

    fig.suptitle(
        f'{title}\n'
        f'最大加速度: {a_peak:.3f} gal  /  計測震度: I={I_peak:.2f}（震度{scale_peak}）'
        f'  /  STA/LTA最大: {r_peak:.2f}{dist_str}{est_M_str}',
        color='#1f2328', fontsize=12,
    )

    plt.savefig(str(out_path), dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"グラフ保存: {out_path}")


# ===== メイン =====
def main():
    ap = argparse.ArgumentParser(
        description='Raspberry Shake 波形取得・分析グラフ生成',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument('--start',
                     help='開始時刻（JST）例: "2026-05-24 07:05:00"')
    grp.add_argument('--from-p2p', action='store_true',
                     help='P2P地震履歴から地震を選択して時刻・マーカーを自動設定')
    ap.add_argument('--p2p-index', type=int, default=None,
                    help='--from-p2p 使用時: 地震リストのインデックス（0始まり）を直接指定')
    ap.add_argument('--p2p-limit', type=int, default=20,
                    help='--from-p2p 時: P2P APIから取得する件数（デフォルト: 20、最大100）')
    ap.add_argument('--p2p-min-scale', type=int, default=0,
                    help='--from-p2p 時: 最大震度の最小値フィルタ（P2P整数値: 45=5弱, 50=5強, 55=6弱, 60=6強, 70=7）')
    ap.add_argument('--p2p-date', type=str, default=None,
                    help='--from-p2p 時: 指定日（JST）の地震を検索 例: 2026-05-01')
    ap.add_argument('--end',
                    help='終了時刻（JST）例: "2026-05-24 07:12:00"')
    ap.add_argument('--duration', type=float, default=420.0,
                    help='継続時間（秒）--end の代わりに指定可 (デフォルト: 420)')
    ap.add_argument('--pre', type=float, default=120.0,
                    help='--from-p2p 時: 地震発生時刻の何秒前から取得するか (デフォルト: 120)')
    ap.add_argument('--station', default='R38DC',
                    help='ステーションコード (デフォルト: R38DC)')
    ap.add_argument('--sta-lat', type=float, default=None,
                    help='観測点緯度（省略時はFDSNから自動取得、次いで.envを使用）')
    ap.add_argument('--sta-lon', type=float, default=None,
                    help='観測点経度（省略時はFDSNから自動取得、次いで.envを使用）')
    ap.add_argument('--sensitivity', type=float, default=SENSITIVITY,
                    help=f'感度 counts/(m/s²) (デフォルト: {SENSITIVITY})')
    ap.add_argument('--sta', type=float, default=1.0, help='STA秒数 (デフォルト: 1.0)')
    ap.add_argument('--lta', type=float, default=20.0, help='LTA秒数 (デフォルト: 20.0)')
    ap.add_argument('--trig', type=float, default=3.5, help='STA/LTA閾値 (デフォルト: 3.5)')
    ap.add_argument('--marker', action='append', default=[],
                    help='縦線マーカー（JST）例: "07:07:02 P波"  複数指定可')
    ap.add_argument('--no-ehz', action='store_true', help='EHZパネルを省略')
    ap.add_argument('--no-download', action='store_true',
                    help='ダウンロードをスキップ（キャッシュ済みを使用）')
    ap.add_argument('--cache-dir', default='data',
                    help='MiniSEEDキャッシュディレクトリ (デフォルト: data/)')
    ap.add_argument('--out', help='出力PNGパス（省略時は自動生成）')
    ap.add_argument('--eq-name',  type=str,   default=None, help='震源名（WebUIから渡される）')
    ap.add_argument('--eq-lat',   type=float, default=None, help='震源緯度')
    ap.add_argument('--eq-lon',   type=float, default=None, help='震源経度')
    ap.add_argument('--eq-mag',   type=float, default=None, help='マグニチュード')
    ap.add_argument('--eq-depth', type=float, default=None, help='震源深さ(km)')
    args = ap.parse_args()

    # ===== WebUI config.json から sta/lta/trig を引き継ぐ =====
    # コマンドラインで明示指定された場合はそちらを優先する
    _cli_specified = set()
    for token in sys.argv[1:]:
        if token.startswith("--"):
            _cli_specified.add(token.lstrip("-").split("=")[0].replace("-", "_"))
    _web_config_path = pathlib.Path.home() / ".config" / "jma_intensity" / "config.json"
    if _web_config_path.exists():
        try:
            _web_cfg = json.loads(_web_config_path.read_text())
            for key in ("sta", "lta", "trig"):
                if key in _web_cfg and key not in _cli_specified:
                    setattr(args, key, float(_web_cfg[key]))
            print(f"[INFO] WebUI設定を反映: sta={args.sta}s  lta={args.lta}s  trig={args.trig}")
        except Exception as e:
            print(f"[WARN] WebUI config.json 読み込み失敗: {e}")

    # ===== 観測点座標の解決 =====
    # 優先順位: --sta-lat/lon 引数 > FDSN自動取得 > .env
    if args.sta_lat is None or args.sta_lon is None:
        print(f"ステーション座標を取得中 (AM.{args.station})...")
        coords = fetch_station_coords(args.station)
        if coords:
            args.sta_lat, args.sta_lon = coords
            print(f"  FDSN取得: 緯度={args.sta_lat:.6f}  経度={args.sta_lon:.6f}")
        else:
            args.sta_lat = STATION_LAT
            args.sta_lon = STATION_LON
            print(f"  .env使用: 緯度={args.sta_lat}  経度={args.sta_lon}")

    quake_info = None
    auto_marker = None

    # ===== 時刻解決 =====
    if args.from_p2p:
        print("P2P地震履歴を取得中...")
        if args.p2p_date:
            quakes = fetch_p2p_quakes_by_date(args.p2p_date)
        else:
            quakes = fetch_p2p_quakes(limit=args.p2p_limit)
        q = select_p2p_quake(quakes, args.p2p_index, min_scale=args.p2p_min_scale)
        quake_info = q
        t_eq_utc   = q['time_jst'].astimezone(UTC)
        t_start_utc = t_eq_utc - timedelta(seconds=args.pre)
        t_end_utc   = t_start_utc + timedelta(seconds=args.duration)
        # P波到達目安: 震源距離（深さ込み）/ 6km/s（P波速度概算）
        if q['latitude'] is not None and q['longitude'] is not None:
            dlat = q['latitude'] - args.sta_lat
            dlon = q['longitude'] - args.sta_lon
            horiz_km = np.sqrt((dlat * 111.0)**2 +
                               (dlon * 111.0 * np.cos(np.radians(args.sta_lat)))**2)
            depth_km = max(0.0, q.get('depth', 0) or 0)
            dist_km = np.sqrt(horiz_km**2 + depth_km**2)
            p_delay = dist_km / 6.0
            t_p = (q['time_jst'] + timedelta(seconds=p_delay))
            auto_marker = (t_p, f'P波推定({dist_km:.0f}km/{p_delay:.0f}s)')
            print(f"震源距離: {dist_km:.0f}km（水平{horiz_km:.0f}km 深さ{depth_km:.0f}km）  P波推定到達: {t_p.strftime('%H:%M:%S')} JST")
    else:
        if not args.start:
            ap.error('--start または --from-p2p を指定してください。')
        t_start_utc = parse_jst(args.start)
        if args.end:
            t_end_utc = parse_jst(args.end)
        else:
            t_end_utc = t_start_utc + timedelta(seconds=args.duration)
        if args.eq_lat is not None and args.eq_lon is not None:
            quake_info = {
                'name':      args.eq_name or '',
                'latitude':  args.eq_lat,
                'longitude': args.eq_lon,
                'magnitude': args.eq_mag if args.eq_mag is not None else 0.0,
                'depth':     args.eq_depth if args.eq_depth is not None else 0,
            }
            dlat = args.eq_lat - args.sta_lat
            dlon = args.eq_lon - args.sta_lon
            horiz_km = np.sqrt((dlat * 111.0)**2 +
                               (dlon * 111.0 * np.cos(np.radians(args.sta_lat)))**2)
            depth_km = max(0.0, quake_info['depth'] or 0)
            dist_km  = np.sqrt(horiz_km**2 + depth_km**2)
            p_delay  = dist_km / 6.0
            t_eq_jst = datetime.strptime(args.start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=JST)
            t_p = t_eq_jst + timedelta(seconds=p_delay)
            auto_marker = (t_p, f'P波推定({dist_km:.0f}km/{p_delay:.0f}s)')
            print(f"震源距離: {dist_km:.0f}km（水平{horiz_km:.0f}km 深さ{depth_km:.0f}km）  P波推定到達: {t_p.strftime('%H:%M:%S')} JST")

    if t_end_utc <= t_start_utc:
        ap.error('終了時刻は開始時刻より後にしてください。')

    t_start_jst = t_start_utc.astimezone(JST)
    t_end_jst   = t_end_utc.astimezone(JST)
    duration_s  = (t_end_utc - t_start_utc).total_seconds()
    print(f"対象区間: {t_start_jst.strftime('%Y-%m-%d %H:%M:%S')} 〜 "
          f"{t_end_jst.strftime('%H:%M:%S')} JST  ({duration_s:.0f}秒)")

    cache_dir = pathlib.Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag = t_start_jst.strftime('%Y%m%d_%H%M%S') + f'_{int(duration_s)}s'

    def ms_path(ch: str) -> pathlib.Path:
        return cache_dir / f"AM.{args.station}.00.{ch}.{tag}.ms"

    # ===== ダウンロード =====
    channels_en = ['ENZ', 'ENN', 'ENE']
    need_ehz = not args.no_ehz
    if not args.no_download:
        print("MiniSEEDをダウンロード中...")
        for ch in channels_en + (['EHZ'] if need_ehz else []):
            download_channel(args.station, ch, t_start_utc, t_end_utc, ms_path(ch))
    else:
        print("--no-download: キャッシュを使用します。")

    # ===== EN3成分 読み込み（データが存在する成分のみ）=====
    print("波形を読み込み中...")
    from obspy import Stream as ObspyStream
    streams = {}
    for ch in channels_en:
        p = ms_path(ch)
        if not p.exists() or p.stat().st_size == 0:
            print(f"  [WARN] {ch}: データなし（スキップ）")
            continue
        try:
            st = obspy_read(str(p))
            streams[ch] = st
        except Exception as e:
            print(f"  [WARN] {ch}: 読み込み失敗 ({e})（スキップ）")

    if not streams:
        sys.exit("[ERROR] EN成分のデータが1つも取得できませんでした。")

    available_en = list(streams.keys())
    print(f"  利用可能なEN成分: {available_en}")

    # ギャップ検出（最初の成分のStreamで代表）
    ref_st = list(streams.values())[0]
    raw_gaps = ref_st.get_gaps()
    gap_spans_utc = [(g[4], g[5], g[6]) for g in raw_gaps]  # (start, end, duration_s)
    if gap_spans_utc:
        for gs, ge, gd in gap_spans_utc:
            print(f"  [INFO] データギャップ検出: {gs} → {ge}  ({gd:.2f}秒)")

    # ギャップをNaN埋めでマージ（int32→float64に変換してからmerge）
    traces = {}
    for ch, st in streams.items():
        st_copy = st.copy()
        for tr in st_copy:
            tr.data = tr.data.astype(np.float64)
        st_merged = st_copy.merge(fill_value=np.nan)
        tr = st_merged[0]
        traces[ch] = tr

    t0_obspy = max(tr.stats.starttime for tr in traces.values())
    t1_obspy = min(tr.stats.endtime   for tr in traces.values())
    fs       = list(traces.values())[0].stats.sampling_rate
    N        = int((t1_obspy - t0_obspy) * fs)

    segs = {}
    for ch, tr in traces.items():
        i0 = int((t0_obspy - tr.stats.starttime) * fs)
        segs[ch] = tr.data[i0:i0 + N]

    # 成分間の配列長を最小値に揃える
    min_len = min(len(s) for s in segs.values())
    segs = {ch: s[:min_len] for ch, s in segs.items()}
    N = min_len

    # ===== STA/LTA =====
    print("STA/LTA計算中...")
    # NaN（ギャップ埋め）はゼロ置換して計算（ゼロ区間でLTAが小さくなるが可視化目的では許容）
    def nan_to_zero(arr):
        out = arr.copy()
        out[np.isnan(out)] = 0.0
        return out

    # リアルタイム検出と同じロジック: EHZに1-10Hzバンドパスを適用して使用、
    # EHZデータがなければENZ3成分合成にフォールバック
    ehz_path = ms_path('EHZ')
    stalta_src = None
    if ehz_path.exists() and ehz_path.stat().st_size > 0:
        try:
            tr_ehz_stalta = obspy_read(str(ehz_path))[0]
            fs_stalta = tr_ehz_stalta.stats.sampling_rate
            i0_s = max(0, int((t0_obspy - tr_ehz_stalta.stats.starttime) * fs_stalta))
            N_s  = int((t1_obspy - t0_obspy) * fs_stalta)
            ehz_seg = tr_ehz_stalta.data.astype(float)[i0_s:i0_s + N_s]
            ehz_dc  = ehz_seg - np.nanmean(ehz_seg)
            nan_to_zero(ehz_dc)  # in-place ではないので再代入
            ehz_dc  = nan_to_zero(ehz_dc)
            nyq = fs_stalta / 2.0
            sos = butter(4, [1.0 / nyq, 10.0 / nyq], btype='band', output='sos')
            ehz_filtered = sosfilt(sos, ehz_dc)
            vec_raw = np.abs(ehz_filtered)
            fs_stalta_used = fs_stalta  # STA/LTA計算用fsは別変数に保持（fsはEN成分を維持）
            stalta_src = "EHZ(1-10Hz BP)"
            print(f"  STA/LTA: EHZチャンネルを使用（{stalta_src}）")
        except Exception as e:
            print(f"  [WARN] EHZ読み込み失敗、ENZ3成分にフォールバック: {e}")
            stalta_src = None

    if stalta_src is None:
        vec_raw = np.sqrt(sum(
            (nan_to_zero(segs[ch]) - np.nanmean(segs[ch]))**2 for ch in available_en
        ))
        fs_stalta_used = fs
        stalta_src = "+".join(available_en)
        print(f"  STA/LTA: EN3成分合成を使用（{stalta_src}）")

    ratio_arr = compute_stalta(vec_raw, fs_stalta_used, args.sta, args.lta)
    # EN成分（fs）とEHZ（fs_stalta_used）でサンプリングレートが異なる場合、
    # ratio_arrの長さをNに合わせてトリム（超過分を切り捨て、短い場合はNaNパディング）
    if len(ratio_arr) > N:
        ratio_arr = ratio_arr[:N]
    elif len(ratio_arr) < N:
        ratio_arr = np.concatenate([ratio_arr, np.zeros(N - len(ratio_arr))])

    # ===== JMAフィルタ → 計測震度 =====
    print("計測震度計算中...")
    filtered = [
        apply_jma_filter_time((nan_to_zero(segs[ch]) - np.nanmean(segs[ch])) / args.sensitivity, fs)
        for ch in available_en
    ]
    a_comb_gal = np.sqrt(sum(f**2 for f in filtered)) * 100.0
    I_arr = compute_intensity_timeseries(a_comb_gal, fs, window_s=90.0)

    t0_dt     = t0_obspy.datetime.replace(tzinfo=UTC).astimezone(JST)
    times_jst = [t0_dt + timedelta(seconds=k / fs) for k in range(N)]

    # ギャップ時刻をJSTに変換（グラフ描画用）
    gap_spans_jst = [
        (
            gs.datetime.replace(tzinfo=UTC).astimezone(JST),
            ge.datetime.replace(tzinfo=UTC).astimezone(JST),
            gd,
        )
        for gs, ge, gd in gap_spans_utc
    ]

    # ===== EHZ =====
    ehz_raw_dc = None
    times_ehz  = None
    fs_ehz     = fs
    if need_ehz and ms_path('EHZ').exists():
        tr_ehz = obspy_read(str(ms_path('EHZ')))[0]
        fs_ehz = tr_ehz.stats.sampling_rate
        i0_ehz = max(0, int((t0_obspy - tr_ehz.stats.starttime) * fs_ehz))
        N_ehz  = int((t1_obspy - t0_obspy) * fs_ehz)
        ehz_raw = tr_ehz.data.astype(float)[i0_ehz:i0_ehz + N_ehz]
        if len(ehz_raw) > 0:
            ehz_raw_dc = ehz_raw - np.mean(ehz_raw)
            t0_ehz_dt = t0_obspy.datetime.replace(tzinfo=UTC).astimezone(JST)
            times_ehz = [t0_ehz_dt + timedelta(seconds=k / fs_ehz) for k in range(len(ehz_raw_dc))]
        else:
            print("[WARN] EHZ: スライス結果が空です（時刻範囲がトレースと合っていない可能性）")
    elif need_ehz:
        print(f"[WARN] EHZファイルが見つかりません: {ms_path('EHZ')}")

    # ===== マーカー組み立て =====
    markers = []
    if auto_marker:
        markers.append(auto_marker)
    for m in args.marker:
        try:
            parts = m.strip().split(' ', 1)
            hms   = parts[0]
            lbl   = parts[1] if len(parts) > 1 else hms
            hms_parts = hms.split(':')
            if len(hms_parts) != 3:
                raise ValueError
            h, mi, s = (int(x) for x in hms_parts)
            t_m = t_start_jst.replace(hour=h, minute=mi, second=s, microsecond=0)
            markers.append((t_m, lbl))
        except ValueError:
            ap.error(f'--marker の形式が不正です: {m!r}  正しい形式: "HH:MM:SS ラベル"')

    # ===== 出力パス =====
    out_path = pathlib.Path(args.out) if args.out else cache_dir / f"analysis_{tag}.png"

    q_name = quake_info['name'] if quake_info else ''
    q_mag  = quake_info['magnitude'] if quake_info else ''
    title  = (
        f"AM.{args.station}  "
        f"{t_start_jst.strftime('%Y-%m-%d %H:%M:%S')} 〜 {t_end_jst.strftime('%H:%M:%S')} JST"
        f"（{duration_s:.0f}秒）"
        + (f"  {q_name} M{q_mag}" if q_name else "")
    )

    print("グラフを生成中...")
    plot_analysis(
        times_jst=times_jst,
        a_comb_gal=a_comb_gal,
        ratio_arr=ratio_arr,
        I_arr=I_arr,
        times_ehz=times_ehz,
        ehz_raw_dc=ehz_raw_dc,
        fs_ehz=fs_ehz,
        sta_s=args.sta,
        lta_s=args.lta,
        trig_thr=args.trig,
        title=title,
        out_path=out_path,
        markers=markers,
        stable_offset_s=args.lta,
        quake_info=quake_info,
        sta_lat=args.sta_lat,
        sta_lon=args.sta_lon,
        available_en=available_en,
        sta_name=args.station,
        gap_spans=gap_spans_jst,
        stalta_src=stalta_src,
    )

    import subprocess, platform
    opener = 'open' if platform.system() == 'Darwin' else 'xdg-open'
    try:
        subprocess.Popen([opener, str(out_path)])
    except Exception:
        pass


if __name__ == '__main__':
    main()
