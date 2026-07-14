#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HVSR（水平/上下スペクトル比、Nakamura法）週次モニタリングバッチ

深夜帯（既定 02:00〜05:00 JST）の常時微動データ（ENZ/ENN/ENE）をダウンロードし、
40秒窓・50%オーバーラップでSTA/LTAアンチトリガにより地震・突発ノイズ区間を除外した上で、
H/V比を対数平均でスタッキング、Konno-Ohmachi平滑化を適用して週次のHVSR曲線・
ピーク周波数を算出し、data/hvsr_history.jsonl に1行追記する。

設計書: documents/designs/2026-07-14-hvsr-weekly-monitoring.md
レビュー: documents/reviews/2026-07-14-hvsr-weekly-monitoring-review.md

使い方:
    # 通常実行（当日実行時点から過去の直近の深夜ブロックを対象に計算・追記）
    .venv/bin/python3 src/hvsr_weekly.py

    # ダウンロードのみ確認（追記しない）
    .venv/bin/python3 src/hvsr_weekly.py --dry-run

    # 過去日を指定して手動再計算（バックフィル用）
    .venv/bin/python3 src/hvsr_weekly.py --date 2026-07-13

Copyright (c) 2026 Masanori Sakai
"""

import argparse
import json
import pathlib
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

try:
    from obspy import read as obspy_read
except ImportError:
    sys.exit("[ERROR] obspy がインストールされていません。.venv/bin/pip install obspy を実行してください。")

from obspy.signal.konnoohmachismoothing import konno_ohmachi_smoothing

# ===== 定数 =====
RS_FDSN_URL = "https://data.raspberryshake.org/fdsnws/dataselect/1/query"
NETWORK     = "AM"
STATION     = "R38DC"
LOCATION    = "00"
JST = timezone(timedelta(hours=9))
UTC = timezone.utc

_ROOT = pathlib.Path(__file__).parent.parent
_HISTORY_PATH = _ROOT / "data" / "hvsr_history.jsonl"
_LOG_PATH = _ROOT / "logs" / "hvsr_weekly.log"
_CACHE_DIR = _ROOT / "data"

# 自局Raspberry Shake SeedLink（公式FDSNフォールバック先。analyze_rs.pyと同じ設定）
import os as _os
RS_SEEDLINK_HOST = _os.environ.get('RS_SEEDLINK_HOST', '10.0.1.53')
RS_SEEDLINK_PORT = int(_os.environ.get('RS_SEEDLINK_PORT', '18000'))

# HTTPS用SSLコンテキスト（analyze_rs.pyと同じ理由でコピー。iMac本番のpython.org製
# Pythonは証明書バンドル未設定のことがあり、certifiがあればそのバンドルで検証する）
try:
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None

# ===== HVSR計算パラメータ（設計書「HVSR計算アルゴリズムの詳細方針」準拠）=====
CAPTURE_START_HOUR_JST = 2   # 02:00 JST
CAPTURE_END_HOUR_JST   = 5   # 05:00 JST
WINDOW_LENGTH_S  = 40.0
WINDOW_OVERLAP   = 0.5
TAPER_FRACTION   = 0.05      # 5%コサインテーパー
STA_S = 1.0
LTA_S = 20.0
ANTITRIGGER_LOW  = 0.5
ANTITRIGGER_HIGH = 2.0
TARGET_N_WINDOWS = 45
FREQ_MIN_HZ = 0.2
FREQ_MAX_HZ = 20.0
FREQ_N_POINTS = 81
KONNO_OHMACHI_B = 40

# SESAME (2004) Table 3: 周波数帯域別の安定性クライテリア閾値係数
# (帯域上限[Hz], epsilon係数) の順で、f0未満の帯域から並べる。
_SESAME_TABLE3 = [
    (0.2, 0.25),
    (0.5, 0.20),
    (1.0, 0.15),
    (2.0, 0.10),
    (float("inf"), 0.05),
]


def log(msg: str) -> None:
    """タイムスタンプ付き1行ログ。print とファイル追記の両方に出す
    （fetch_p2p_daily.py の log() と同じパターン）。"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


# =====================================================================
# 以下2関数は src/analyze_rs.py の同名関数をコピーして複製したものです。
# このロジックは analyze_rs.py/hvsr_weekly.py の対となる関数と重複しています。
# 修正時は両方を確認してください。
#
# importではなくコピーする理由: analyze_rs.py はモジュールのトップレベルで
# geopandas・matplotlib（Agg初期化含む）を読み込んでおり、週次バッチである
# 本ファイルがグラフ描画・地図描画を一切行わないにもかかわらず、素朴に
# `from analyze_rs import download_channel` すると不要な重量級依存が
# 毎回ロードされてしまう。これを避けるため関数のみを複製する。
# =====================================================================

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
        log(f"[INFO] {channel}: 区間が未来のためSeedLink取得不可")
        return False
    try:
        cli = Client(RS_SEEDLINK_HOST, port=RS_SEEDLINK_PORT, timeout=30)
        st = cli.get_waveforms(NETWORK, station, LOCATION, channel,
                               UTCDateTime(t_start), UTCDateTime(req_end))
    except Exception as e:
        # 自局に到達できない（LAN外実行など）場合はここに来る。フォールバック断念。
        log(f"[INFO] {channel}: 自局SeedLink取得不可 ({e})")
        return False
    if not st or len(st) == 0:
        return False
    try:
        st.write(str(out_path), format='MSEED')
    except Exception as e:
        log(f"[WARN] {channel}: SeedLink結果の書き出し失敗 ({e})")
        return False
    total = sum(tr.stats.npts for tr in st)
    log(f"[自局SeedLink] {channel}: {out_path.stat().st_size:,} bytes ({total:,} samples)")
    return True


def download_channel(station: str, channel: str, t_start: datetime, t_end: datetime,
                     out_path: pathlib.Path):
    """公式FDSN→自局SeedLinkフォールバックでMiniSEEDを取得し out_path に保存する。"""
    start_str = t_start.strftime('%Y-%m-%dT%H:%M:%S')
    end_str   = t_end.strftime('%Y-%m-%dT%H:%M:%S')
    url = (
        f"{RS_FDSN_URL}"
        f"?network={NETWORK}&station={station}&location={LOCATION}&channel={channel}"
        f"&starttime={start_str}&endtime={end_str}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "hvsr-weekly/1.0"})
    log(f"{channel}: {start_str} -> {end_str} ダウンロード中...")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            data = resp.read()
    except Exception as e:
        log(f"[WARN] {channel}: 公式FDSNダウンロード失敗 ({e})")
        data = b''
    if data:
        out_path.write_bytes(data)
        log(f"{channel}: {len(data):,} bytes")
        return
    # 公式FDSNにデータが無い（発生直後など）→ 自局RSのSeedLinkにフォールバック
    log(f"{channel}: 公式FDSNにデータなし -> 自局SeedLinkを試行")
    if download_channel_seedlink(station, channel, t_start, t_end, out_path):
        return
    if out_path.exists():
        out_path.unlink()
    log(f"{channel}: 0 bytes (データなし・スキップ)")


def compute_stalta(vec: np.ndarray, fs: float, sta_s: float, lta_s: float) -> np.ndarray:
    """STA/LTA比の時系列を計算する（analyze_rs.py::compute_stalta と同一ロジック）。

    注意: このロジックはanalyze_rs.py（リアルタイム地震検知・trig=3.5でトリガ判定）
    と同じ計算式だが、本ファイルでは閾値の意味論が全く別物（常時微動の定常性判定
    のためのアンチトリガ、[0.5, 2.0]範囲外を棄却）である点に注意すること。
    """
    nsta = max(1, int(sta_s * fs))
    nlta = max(nsta + 1, int(lta_s * fs))
    sq = (vec - np.mean(vec)) ** 2
    cs = np.concatenate([[0.0], np.cumsum(sq)])
    N = len(vec)
    ratio = np.zeros(N)
    for i in range(nlta, N):
        lta_e = (cs[i - nsta] - cs[i - nlta]) / (nlta - nsta) + 1e-18
        sta_e = (cs[i] - cs[i - nsta]) / nsta + 1e-18
        ratio[i] = sta_e / lta_e
    return ratio


# =====================================================================
# ここからHVSR計算のコアロジック（hvsr_weekly.py独自実装）
# =====================================================================

def split_windows(n_samples: int, fs: float, window_length_s: float = WINDOW_LENGTH_S,
                   overlap: float = WINDOW_OVERLAP) -> list[tuple[int, int]]:
    """波形を窓長・オーバーラップで分割し、(開始インデックス, 終了インデックス) のリストを返す。"""
    nwin = int(round(window_length_s * fs))
    step = max(1, int(round(nwin * (1.0 - overlap))))
    windows = []
    start = 0
    while start + nwin <= n_samples:
        windows.append((start, start + nwin))
        start += step
    return windows


def is_window_valid(stalta_ratio: np.ndarray, start: int, end: int,
                     low: float = ANTITRIGGER_LOW, high: float = ANTITRIGGER_HIGH) -> bool:
    """窓内のSTA/LTA比が[low, high]の範囲を外れる時刻を1つでも含む場合、無効（棄却）とする。

    SESAME準拠のアンチトリガ。analyze_rs.pyのtrig=3.5（地震検知トリガ）とは
    意味論が全く別物であり、混同しないこと（設計書「地震区間の除外」参照）。
    """
    seg = stalta_ratio[start:end]
    if seg.size == 0:
        return False
    return bool(np.all((seg >= low) & (seg <= high)))


def apply_cosine_taper(vec: np.ndarray, fraction: float = TAPER_FRACTION) -> np.ndarray:
    """両端に指定割合のコサイン（Tukey窓）テーパーを適用する。"""
    n = len(vec)
    taper = np.ones(n)
    n_taper = int(n * fraction)
    if n_taper > 0:
        edge = 0.5 * (1 - np.cos(np.pi * np.arange(n_taper) / n_taper))
        taper[:n_taper] = edge
        taper[-n_taper:] = edge[::-1]
    return vec * taper


def compute_window_hv(enz: np.ndarray, enn: np.ndarray, ene: np.ndarray, fs: float
                       ) -> tuple[np.ndarray, np.ndarray]:
    """1窓分の3成分波形からFFT後のH/V比（生、未平滑化）を計算する。

    戻り値: (正の周波数配列, HV比配列)。DC成分（周波数0）は除く。
    """
    n = len(enz)
    enz_t = apply_cosine_taper(enz - np.mean(enz))
    enn_t = apply_cosine_taper(enn - np.mean(enn))
    ene_t = apply_cosine_taper(ene - np.mean(ene))

    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    amp_z = np.abs(np.fft.rfft(enz_t))
    amp_n = np.abs(np.fft.rfft(enn_t))
    amp_e = np.abs(np.fft.rfft(ene_t))

    # DC成分（freq=0）は比を取る対象として無意味なため除外する
    mask = freqs > 0
    freqs = freqs[mask]
    amp_z = amp_z[mask]
    amp_n = amp_n[mask]
    amp_e = amp_e[mask]

    h_amp = np.sqrt(amp_n * amp_e)
    with np.errstate(divide='ignore', invalid='ignore'):
        hv = np.where(amp_z > 0, h_amp / amp_z, 0.0)
    return freqs, hv


def peak_frequency_from_curve(freqs: np.ndarray, hv: np.ndarray) -> float | None:
    """HV(f)曲線からピーク周波数を抽出する。空・全ゼロの場合はNoneを返す。"""
    if freqs.size == 0 or not np.any(np.isfinite(hv)) or np.nanmax(hv) <= 0:
        return None
    idx = int(np.nanargmax(hv))
    return float(freqs[idx])


def stack_log_average(curves: list[np.ndarray]) -> np.ndarray:
    """複数窓のHV(f)を対数平均（幾何平均）でスタッキングする。

    H/V比は対数正規分布に近い性質を持つため、算術平均ではなく対数平均を用いる
    （SESAMEガイドラインでも標準的に採用される、設計書「FFT・H/V比計算・スタッキング」参照）。
    """
    stacked = np.vstack(curves)
    # 0以下の値はlog計算で-infになるため、微小な下限値でクリップする
    stacked = np.maximum(stacked, 1e-12)
    log_mean = np.mean(np.log(stacked), axis=0)
    return np.exp(log_mean)


def smooth_and_resample(freqs: np.ndarray, hv: np.ndarray,
                         freq_min: float = FREQ_MIN_HZ, freq_max: float = FREQ_MAX_HZ,
                         n_points: int = FREQ_N_POINTS, b: int = KONNO_OHMACHI_B
                         ) -> tuple[np.ndarray, np.ndarray]:
    """スタッキング済みHV(f)にKonno-Ohmachi平滑化を適用し、対数等間隔周波数軸に補間する。

    konno_ohmachi_smoothing() は元のFFT周波数ビン上でスペクトルを平滑化するのみで、
    出力周波数軸を独自に指定する引数を持たない（実装前にhelp()で確認済み）。
    そのため (1) 元の周波数軸上で平滑化 → (2) 対数等間隔n_points点へ線形補間、
    の2段階で処理する。

    normalize=True を指定する（SESAMEガイドライン標準・Geopsy実装に合わせる）。
    normalize=False（デフォルト）だと平滑化窓が対数尺度で正規化されないため、
    40秒窓・100Hzサンプリングの周波数ビン構成ではナイキスト周波数（50Hz）に
    近い高周波側で窓の重み和が縮小し、本来ノイズフロア相当の値が不当に
    増幅されることを実測で確認した（合成波形テストで20Hz付近が本来の
    ピークより大きくなる現象として現れた）。normalize=True によりこの
    アーティファクトが解消され、期待通りの周波数にピークが出ることを確認済み。
    """
    smoothed = konno_ohmachi_smoothing(
        hv.astype(np.float64), freqs.astype(np.float64), bandwidth=b, normalize=True,
    )
    log_freq_axis = np.logspace(np.log10(freq_min), np.log10(freq_max), n_points)
    # 元データの範囲外は外挿せず、範囲内でのみ線形補間する
    resampled = np.interp(log_freq_axis, freqs, smoothed, left=smoothed[0], right=smoothed[-1])
    return log_freq_axis, resampled


def sesame_criteria_ok(peak_frequency_hz: float, peak_amplitude: float,
                        peak_freq_std_hz: float, window_length_s: float = WINDOW_LENGTH_S
                        ) -> dict:
    """SESAME (2004) 信頼性クライテリアの一部（3項目のみ）を算出する。

    設計書「品質指標」節・データ形式定義を参照。SESAME原典9下位クライテリアのうち
    3個のみを選択的に記録したものであり、正式な「reliable curve」「clear peak」の
    合否判定ではない（警報・自動判定には使わない）。
    """
    window_length_ok = peak_frequency_hz > (10.0 / window_length_s)
    amplitude_ok = peak_amplitude >= 2.0

    epsilon = None
    for upper, coef in _SESAME_TABLE3:
        if peak_frequency_hz < upper:
            epsilon = coef * peak_frequency_hz
            break
    if epsilon is None:
        epsilon = _SESAME_TABLE3[-1][1] * peak_frequency_hz
    stability_ok = peak_freq_std_hz < epsilon

    return {
        "window_length_ok": bool(window_length_ok),
        "amplitude_ok": bool(amplitude_ok),
        "stability_ok": bool(stability_ok),
        "peak_freq_std_hz": float(peak_freq_std_hz),
    }


def compute_hvsr_from_traces(enz: np.ndarray, enn: np.ndarray, ene: np.ndarray, fs: float
                              ) -> dict:
    """3成分の連続波形（同一長・同一サンプリングレート）からHVSR週次レコードを計算する。

    戻り値は data/hvsr_history.jsonl の1レコード相当の辞書（week_start/computed_at/
    capture_window/weather_note は呼び出し側で付与する）。
    """
    n_samples = min(len(enz), len(enn), len(ene))
    enz, enn, ene = enz[:n_samples], enn[:n_samples], ene[:n_samples]

    windows = split_windows(n_samples, fs)
    n_windows_total = len(windows)

    if n_windows_total == 0:
        return {
            "status": "failed",
            "n_windows_total": 0,
            "n_windows_used": 0,
            "reject_ratio": None,
            "window_length_s": WINDOW_LENGTH_S,
            "window_overlap": WINDOW_OVERLAP,
            "peak_frequency_hz": None,
            "peak_amplitude": None,
            "freq_hz": None,
            "hv_ratio": None,
            "smoothing": {"method": "konno_ohmachi", "b": KONNO_OHMACHI_B},
            "sesame_criteria": None,
        }

    # STA/LTAアンチトリガ判定は3成分それぞれで行い、いずれか1成分でも棄却対象なら
    # その窓全体を棄却する（1成分でも非定常であれば、その窓のH/V比計算に非定常成分が
    # 混入するため）。
    stalta_z = compute_stalta(enz, fs, STA_S, LTA_S)
    stalta_n = compute_stalta(enn, fs, STA_S, LTA_S)
    stalta_e = compute_stalta(ene, fs, STA_S, LTA_S)

    valid_curves = []
    peak_freq_per_window = []
    for start, end in windows:
        ok = (is_window_valid(stalta_z, start, end)
              and is_window_valid(stalta_n, start, end)
              and is_window_valid(stalta_e, start, end))
        if not ok:
            continue
        freqs, hv = compute_window_hv(enz[start:end], enn[start:end], ene[start:end], fs)
        pf = peak_frequency_from_curve(freqs, hv)
        if pf is not None:
            peak_freq_per_window.append(pf)
        valid_curves.append((freqs, hv))

    n_windows_used = len(valid_curves)
    reject_ratio = 1.0 - (n_windows_used / n_windows_total)

    if n_windows_used == 0:
        return {
            "status": "failed",
            "n_windows_total": n_windows_total,
            "n_windows_used": 0,
            "reject_ratio": reject_ratio,
            "window_length_s": WINDOW_LENGTH_S,
            "window_overlap": WINDOW_OVERLAP,
            "peak_frequency_hz": None,
            "peak_amplitude": None,
            "freq_hz": None,
            "hv_ratio": None,
            "smoothing": {"method": "konno_ohmachi", "b": KONNO_OHMACHI_B},
            "sesame_criteria": None,
        }

    # 全有効窓は同じ周波数ビン構成（同じ窓長・fsのため）を持つ前提でスタッキングする
    freqs_ref = valid_curves[0][0]
    stacked_hv = stack_log_average([hv for _, hv in valid_curves])

    freq_hz, hv_ratio = smooth_and_resample(freqs_ref, stacked_hv)

    peak_idx = int(np.argmax(hv_ratio))
    peak_frequency_hz = float(freq_hz[peak_idx])
    peak_amplitude = float(hv_ratio[peak_idx])

    peak_freq_std_hz = float(np.std(peak_freq_per_window)) if len(peak_freq_per_window) > 1 else 0.0
    sesame_criteria = sesame_criteria_ok(peak_frequency_hz, peak_amplitude, peak_freq_std_hz)

    status = "ok" if n_windows_used >= TARGET_N_WINDOWS else "insufficient_data"

    return {
        "status": status,
        "n_windows_total": n_windows_total,
        "n_windows_used": n_windows_used,
        "reject_ratio": reject_ratio,
        "window_length_s": WINDOW_LENGTH_S,
        "window_overlap": WINDOW_OVERLAP,
        "peak_frequency_hz": peak_frequency_hz,
        "peak_amplitude": peak_amplitude,
        "freq_hz": [round(float(f), 6) for f in freq_hz],
        "hv_ratio": [round(float(v), 6) for v in hv_ratio],
        "smoothing": {"method": "konno_ohmachi", "b": KONNO_OHMACHI_B},
        "sesame_criteria": sesame_criteria,
    }


# =====================================================================
# バッチ実行部分
# =====================================================================

def capture_window_for_date(target_date_jst) -> tuple[datetime, datetime]:
    """指定日（JST日付）の深夜取得ブロック 02:00〜05:00 JST の開始・終了UTC時刻を返す。"""
    start_jst = datetime(target_date_jst.year, target_date_jst.month, target_date_jst.day,
                         CAPTURE_START_HOUR_JST, 0, 0, tzinfo=JST)
    end_jst = datetime(target_date_jst.year, target_date_jst.month, target_date_jst.day,
                       CAPTURE_END_HOUR_JST, 0, 0, tzinfo=JST)
    return start_jst.astimezone(UTC), end_jst.astimezone(UTC)


def week_start_for_date(target_date_jst):
    """対象日を含む週の月曜日（JST基準）の日付を返す。"""
    return target_date_jst - timedelta(days=target_date_jst.weekday())


def append_history(entry: dict) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(target_date_jst=None, dry_run: bool = False) -> dict:
    """週次バッチ本体。target_date_jst省略時は実行時点の日付を使う。"""
    if target_date_jst is None:
        target_date_jst = datetime.now(JST).date()

    t_start, t_end = capture_window_for_date(target_date_jst)
    week_start = week_start_for_date(target_date_jst)

    log(f"HVSR週次バッチ開始: 対象日={target_date_jst} 取得区間={t_start.isoformat()} - {t_end.isoformat()}")

    channels = ["ENZ", "ENN", "ENE"]
    paths = {}
    tag = t_start.strftime("%Y%m%d_%H%M%S")
    for ch in channels:
        out_path = _CACHE_DIR / f"AM.{STATION}.{LOCATION}.{ch}.hvsr_{tag}.ms"
        download_channel(STATION, ch, t_start, t_end, out_path)
        paths[ch] = out_path

    if dry_run:
        log("dry-run指定のため、ダウンロードのみで終了します。")
        return {"status": "dry_run", "paths": {k: str(v) for k, v in paths.items()}}

    missing = [ch for ch, p in paths.items() if not p.exists() or p.stat().st_size == 0]
    if missing:
        log(f"[ERROR] チャンネル取得失敗のためHVSR計算を中止します: {missing}")
        entry = {
            "week_start": week_start.isoformat(),
            "computed_at": datetime.now(JST).isoformat(),
            "station": STATION,
            "capture_window": {"start": t_start.isoformat(), "end": t_end.isoformat()},
            "status": "failed",
            "n_windows_total": 0,
            "n_windows_used": 0,
            "reject_ratio": None,
            "window_length_s": WINDOW_LENGTH_S,
            "window_overlap": WINDOW_OVERLAP,
            "peak_frequency_hz": None,
            "peak_amplitude": None,
            "freq_hz": None,
            "hv_ratio": None,
            "smoothing": {"method": "konno_ohmachi", "b": KONNO_OHMACHI_B},
            "sesame_criteria": None,
            "weather_note": "",
        }
        append_history(entry)
        return entry

    traces = {}
    fs = None
    for ch, p in paths.items():
        st = obspy_read(str(p))
        tr = st[0]
        traces[ch] = tr.data.astype(np.float64)
        fs = tr.stats.sampling_rate

    result = compute_hvsr_from_traces(traces["ENZ"], traces["ENN"], traces["ENE"], fs)

    entry = {
        "week_start": week_start.isoformat(),
        "computed_at": datetime.now(JST).isoformat(),
        "station": STATION,
        "capture_window": {"start": t_start.isoformat(), "end": t_end.isoformat()},
        "weather_note": "",
        **result,
    }
    append_history(entry)
    log(f"HVSR週次バッチ完了: status={entry['status']} "
        f"n_windows_used={entry['n_windows_used']}/{entry['n_windows_total']} "
        f"peak_frequency_hz={entry.get('peak_frequency_hz')}")
    return entry


def main():
    ap = argparse.ArgumentParser(description="HVSR週次モニタリングバッチ")
    ap.add_argument("--dry-run", action="store_true", help="ダウンロードのみ確認し、履歴には追記しない")
    ap.add_argument("--date", type=str, default=None,
                    help="過去日を指定して手動再計算（YYYY-MM-DD、バックフィル対応）")
    args = ap.parse_args()

    target_date = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"[ERROR] --date の形式が不正です（例: 2026-07-13）: {args.date}")

    run(target_date_jst=target_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
