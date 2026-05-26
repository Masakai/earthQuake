#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R38DC マイクロセイズム診断図（改良版）

ENZ・ENE・ENN の3成分（全てMEMS加速度計）を計器応答除去（m/s単位に統一）し、
スペクトログラム・平均PSD・ピーク検出・H/V比・帯域パワー時系列・昼夜比較を1枚に出力。

使い方:
    .venv/bin/python3 src/microseism.py --start "2026-05-25 20:00:00" --end "2026-05-26 02:00:00"
    .venv/bin/python3 src/microseism.py --start "2026-05-25 20:00:00" --duration 21600 --no-download

H/V計算:
    H = sqrt(ENE_power + ENN_power)  [m/s / sqrt(Hz)]
    V = sqrt(ENZ_power)              [m/s / sqrt(Hz)]
    H/V = H / V  （標準振幅比）
    ※ dBで足し算しない。線形パワーで合成。
"""

import argparse
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker
import matplotlib.font_manager as fm
from scipy.signal import welch, find_peaks, medfilt

try:
    from obspy import read as obspy_read
    from obspy.clients.fdsn import Client as FDSNClient
except ImportError:
    sys.exit("[ERROR] obspy がインストールされていません。")

# ===== 定数 =====
NETWORK  = "AM"
STATION  = "R38DC"
LOCATION = "00"
CHANNELS = ["ENZ", "ENE", "ENN"]
JST = timezone(timedelta(hours=9))
UTC = timezone.utc

_ROOT   = pathlib.Path(__file__).parent.parent
_CACHE  = _ROOT / "data" / "microseism_cache"
_OUTDIR = _ROOT / "data"

F_LOW  = 0.05
F_HIGH = 0.5

CHANNEL_LABELS = {"ENZ": "ENZ（垂直）", "ENE": "ENE（東西）", "ENN": "ENN（南北）"}
CHANNEL_COLORS = {"ENZ": "#ff6b6b", "ENE": "#4ecdc4", "ENN": "#ffe66d"}

# スタイル定数
DARK_BG = '#0d1117'
GRID_C  = '#303040'
TEXT_C  = '#e0e0e0'


def _setup_font():
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
    ]
    for path in candidates:
        if pathlib.Path(path).exists():
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            plt.rcParams['font.family'] = prop.get_name()
            return
    plt.rcParams['font.family'] = 'sans-serif'

_setup_font()


def download_channel_obspy(channel: str, t_start: datetime, t_end: datetime, out_path: pathlib.Path):
    """ObsPy FDSNクライアント経由でダウンロード（urllib直接より安定）"""
    from obspy import UTCDateTime
    client = FDSNClient(base_url="https://data.raspberryshake.org")
    t0 = UTCDateTime(t_start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S'))
    t1 = UTCDateTime(t_end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S'))
    print(f"  {channel}: ダウンロード中...", end=" ", flush=True)
    try:
        st = client.get_waveforms(NETWORK, STATION, LOCATION, channel, t0, t1)
        st.write(str(out_path), format='MSEED')
        print(f"{out_path.stat().st_size:,} bytes")
    except Exception as e:
        print(f"\n  [WARN] {channel}: 失敗 ({e})")


def fetch_inventory():
    print("計器応答情報を取得中...", end=" ", flush=True)
    try:
        client = FDSNClient(base_url="https://data.raspberryshake.org")
        inv = client.get_stations(network=NETWORK, station=STATION, level="response")
        print("OK")
        return inv
    except Exception as e:
        print(f"失敗 ({e})")
        return None


def load_and_correct_trace(path: pathlib.Path, inv, pre_filt=(0.03, 0.05, 0.8, 1.0)) -> tuple:
    """
    mseedを読み込み、計器応答除去してm/s単位の速度データを返す。
    戻り値: (data_array, sampling_rate, corrected: bool)
    """
    st = obspy_read(str(path))
    st.merge(fill_value=0)

    corrected = False
    if inv is not None:
        try:
            st.attach_response(inv)
            st.remove_response(output="VEL", pre_filt=pre_filt)
            corrected = True
        except Exception as e:
            print(f"  [WARN] 応答除去失敗: {e} → counts使用")

    tr = st[0]
    tr.detrend('demean')
    tr.detrend('linear')
    tr.taper(max_percentage=0.01)
    return tr.data.astype(float), tr.stats.sampling_rate, corrected


def compute_psd_welch(data: np.ndarray, fs: float, win_sec: float = 512.0) -> tuple:
    """
    Welch法でPSDを計算。
    戻り値: (freqs, Pxx_linear) — Pxx は m^2/s^2/Hz
    """
    nperseg = min(int(win_sec * fs), len(data))
    noverlap = nperseg // 2
    freqs, Pxx = welch(data, fs=fs, window='hann', nperseg=nperseg,
                       noverlap=noverlap, scaling='density')
    return freqs, Pxx


def compute_hrms_psd(psd_e: tuple, psd_n: tuple) -> tuple:
    """
    Horizontal RMS PSD を線形パワーで合成。
    H_rms = sqrt((ENE_power + ENN_power) / 2)
    PSD単位: m^2/s^2/Hz
    戻り値: (freqs, Pxx_hrms)
    """
    freqs_e, Pxx_e = psd_e
    freqs_n, Pxx_n = psd_n
    Pxx_n_i = np.interp(freqs_e, freqs_n, Pxx_n)
    Pxx_hrms = (Pxx_e + Pxx_n_i) / 2.0
    return freqs_e, Pxx_hrms


def detect_microseism_peak(freqs: np.ndarray, Pxx: np.ndarray,
                            f_lo_trend: float = 0.05, f_hi_trend: float = 0.50,
                            f_lo_search: float = 0.12, f_hi_search: float = 0.35) -> dict:
    """
    背景トレンド除去後のprominenceでピーク検出。
    主判定: Horizontal RMS PSD

    手順:
    1. 広帯域（f_lo_trend〜f_hi_trend, 1 decade）でlog-uniform補間
    2. medfilt で背景トレンド推定（kernel_frac=0.25 → 0.25 decade相当）
    3. 残差スペクトルで find_peaks(prominence=2.0)
    4. 採用ピークは f_lo_search〜f_hi_search に限定
    5. エッジピーク警告・鋭すぎる線警告

    トレンド推定に広帯域を使う理由：
    検索帯域（0.12-0.35Hz = 0.46 decade）だけで medfilt すると
    kernel が全域の80%を覆い、背景除去が機能しないため。

    戻り値: dict with keys
      - main_peak_f_Hz, main_peak_period_sec, inferred_ocean_wave_period_sec
      - peak_prominence_dB, peak_band
      - edge_peak_warning, sharp_peak_warning
      - all_peaks (list of dicts)
    """
    mask_trend = (freqs >= f_lo_trend) & (freqs <= f_hi_trend)
    if mask_trend.sum() < 10:
        return {"main_peak_f_Hz": None, "error": "データ不足"}

    f_wide = freqs[mask_trend]
    p_wide = Pxx[mask_trend]

    # log-uniform グリッドに補間（広帯域）
    log_f_wide = np.log10(f_wide)
    n_uni = max(len(f_wide), 200)
    log_f_uni = np.linspace(log_f_wide[0], log_f_wide[-1], n_uni)
    p_db_wide = 10 * np.log10(p_wide + 1e-40)
    p_db_uni = np.interp(log_f_uni, log_f_wide, p_db_wide)

    # 背景トレンド（0.25 decade相当の移動中央値）
    # 広帯域（1 decade）に対して kernel_frac=0.25 → 約0.25 decade
    kernel_size = max(3, int(n_uni * 0.25) | 1)
    trend_db = medfilt(p_db_uni, kernel_size=kernel_size)
    residual_db = p_db_uni - trend_db

    # 全域でピーク検出し、採用は f_lo_search〜f_hi_search に限定
    peaks_idx_all, props_all = find_peaks(residual_db, prominence=2.0, width=1)
    f_uni_all = 10 ** log_f_uni

    search_mask = np.array([
        (f_lo_search <= f_uni_all[idx] <= f_hi_search)
        for idx in peaks_idx_all
    ])
    peaks_idx = peaks_idx_all[search_mask]
    prominences = props_all['prominences'][search_mask]
    widths = props_all['widths'][search_mask]

    result = {
        "main_peak_f_Hz": None,
        "main_peak_period_sec": None,
        "inferred_ocean_wave_period_sec": None,
        "peak_prominence_dB": None,
        "peak_band": None,
        "edge_peak_warning": False,
        "sharp_peak_warning": False,
        "all_peaks": [],
        "freqs_uni": f_uni_all,
        "trend_db": trend_db,
        "residual_db": residual_db,
        "p_db_uni": p_db_uni,
        "f_lo_search": f_lo_search,
        "f_hi_search": f_hi_search,
    }

    if len(peaks_idx) == 0:
        return result

    # 最大 prominence のピークを主ピークとする
    best_i = np.argmax(prominences)
    main_idx = peaks_idx[best_i]
    main_f = f_uni_all[main_idx]
    main_prom = prominences[best_i]
    main_period = 1.0 / main_f
    inferred_wave = 2.0 / main_f  # ダブルフリーケンシー機構

    # エッジピーク判定（検索帯域端 10% 以内）
    f_range = f_hi_search - f_lo_search
    edge_margin = f_range * 0.10
    edge_warning = (main_f < f_lo_search + edge_margin) or (main_f > f_hi_search - edge_margin)

    # 鋭すぎる線判定（width < 2 bin）
    width_bins = widths[best_i]
    sharp_warning = width_bins < 2.0

    # 帯域分類
    if main_f < 0.10:
        band = "0.05-0.10Hz（一次マイクロセイズム）"
    elif main_f < 0.30:
        band = "0.10-0.30Hz（二次マイクロセイズム）"
    else:
        band = "0.30-0.50Hz（局所ノイズ帯）"

    result.update({
        "main_peak_f_Hz": main_f,
        "main_peak_period_sec": main_period,
        "inferred_ocean_wave_period_sec": inferred_wave,
        "peak_prominence_dB": main_prom,
        "peak_band": band,
        "edge_peak_warning": edge_warning,
        "sharp_peak_warning": sharp_warning,
        "all_peaks": [
            {
                "f_Hz": f_uni_all[idx],
                "prominence_dB": prominences[i],
                "width_bins": widths[i],
            }
            for i, idx in enumerate(peaks_idx)
        ],
    })
    return result


def band_power_timeseries(data: np.ndarray, fs: float, f_lo: float, f_hi: float,
                           win_sec: float = 900.0, step_sec: float = 300.0) -> tuple:
    """
    時系列を短窓に分割して各窓の帯域パワー（dB）を返す。
    戻り値: (t_centers_sec, power_dB)
    """
    n_step = int(step_sec * fs)
    n_win  = int(win_sec * fs)
    n_win  = min(n_win, len(data))
    inner_nperseg = min(n_win, int(256 * fs))

    t_centers = []
    powers_db  = []

    pos = 0
    while pos + n_win <= len(data):
        seg = data[pos : pos + n_win]
        seg = seg - seg.mean()
        freqs, Pxx = welch(seg, fs=fs, window='hann', nperseg=inner_nperseg,
                           noverlap=inner_nperseg // 2, scaling='density')
        mask = (freqs >= f_lo) & (freqs <= f_hi)
        if mask.sum() > 0:
            bp = np.trapezoid(Pxx[mask], freqs[mask])
            powers_db.append(10 * np.log10(bp + 1e-40))
        else:
            powers_db.append(np.nan)
        t_centers.append((pos + n_win // 2) / fs)
        pos += n_step

    return np.array(t_centers), np.array(powers_db)


def style_ax(ax):
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors=TEXT_C, labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor('#404050')
    ax.yaxis.label.set_color(TEXT_C)
    ax.xaxis.label.set_color(TEXT_C)
    ax.title.set_color(TEXT_C)


def add_band_guides(ax, horizontal=True):
    """マイクロセイズム帯の帯塗り・補助線"""
    if horizontal:
        ax.axhspan(0.05, 0.10, alpha=0.10, color='gray',  zorder=0)
        ax.axhspan(0.10, 0.30, alpha=0.15, color='cyan',  zorder=0)
        ax.axhspan(0.30, 0.50, alpha=0.08, color='orange', zorder=0)
        for f in [0.1, 0.2, 0.3]:
            ax.axhline(y=f, color='cyan', linewidth=0.7, linestyle='--', alpha=0.6, zorder=2)
        ax.axhline(y=0.05, color='gray', linewidth=0.5, linestyle=':', alpha=0.5)
    else:
        ax.axvspan(0.05, 0.10, alpha=0.10, color='gray',  zorder=0)
        ax.axvspan(0.10, 0.30, alpha=0.15, color='cyan',  zorder=0)
        ax.axvspan(0.30, 0.50, alpha=0.08, color='orange', zorder=0)
        for f in [0.1, 0.2, 0.3]:
            ax.axvline(x=f, color='cyan', linewidth=0.7, linestyle='--', alpha=0.6, zorder=2)


def _save_panel(fig: plt.Figure, out_dir: pathlib.Path, stem: str, panel_id: str) -> str:
    """パネルを PNG に保存して相対ファイル名を返す。"""
    fname = f"{stem}_panel_{panel_id}.png"
    fpath = out_dir / fname
    fig.savefig(str(fpath), dpi=120, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return fname


def _make_panel_fig(nrows: int = 1, ncols: int = 1, figsize: tuple = (10, 4)):
    """ダークテーマの figure を生成して (fig, ax または axes配列) を返す。"""
    fig, axes = plt.subplots(nrows, ncols, figsize=figsize)
    fig.patch.set_facecolor(DARK_BG)
    if nrows == 1 and ncols == 1:
        style_ax(axes)
    elif nrows == 1 or ncols == 1:
        for ax in np.atleast_1d(axes):
            style_ax(ax)
    else:
        for row in axes:
            for ax in row:
                style_ax(ax)
    return fig, axes


def generate_html_report(
    out_dir: pathlib.Path,
    stem: str,
    t_start_jst,
    t_end_jst,
    day_start_jst,
    day_end_jst,
    psd_results: dict,
    day_psd: dict,
    freqs_h,
    Pxx_hrms,
    freqs_h_day,
    Pxx_hrms_day,
    bp_results: dict,
    traces: dict,
    fs_map: dict,
    sg_scales: dict,
    peak_info: dict,
    rep_f: float,
    rep_T: float,
    rep_wave: float,
    ratio_h2lo: float,
    ratio_h2ehz: float,
    corrected_map: dict,
    time_fmt,
) -> pathlib.Path:
    """
    各パネルを個別 PNG で保存し、解説文付き HTML レポートを生成する。

    戻り値: HTML ファイルの pathlib.Path
    """
    print("HTMLレポート生成中...")
    SG_WIN_SEC = 100.0
    SG_OVERLAP = 0.90

    # ===== 1. スペクトログラム パネル（ENZ / ENE / ENN）=====
    sg_fnames = {}
    sg_titles = {
        "ENZ": "ENZ スペクトログラム（垂直動）",
        "ENE": "ENE スペクトログラム（東西水平動）",
        "ENN": "ENN スペクトログラム（南北水平動）",
    }
    for ch in CHANNELS:
        panel_id = f"sg_{ch}"
        fig, ax = _make_panel_fig(figsize=(12, 3))
        ax.set_title(sg_titles[ch], fontsize=10, color=TEXT_C)

        if traces[ch] is not None:
            data = traces[ch]
            fs   = fs_map[ch]
            nfft_sg = int(SG_WIN_SEC * fs)
            novl_sg = int(nfft_sg * SG_OVERLAP)
            vmin_ch, vmax_ch = sg_scales[ch]
            ax.specgram(data, Fs=fs, NFFT=nfft_sg, noverlap=novl_sg,
                        cmap='inferno', scale='dB',
                        vmin=vmin_ch, vmax=vmax_ch)
            ax.set_ylim(F_LOW, F_HIGH)
            add_band_guides(ax, horizontal=True)
            ax.xaxis.set_major_formatter(time_fmt)
            ax.set_xlabel("時刻 (JST)", fontsize=8, color=TEXT_C)
            ax.set_ylabel(f"{CHANNEL_LABELS[ch]}\n周波数 [Hz]", fontsize=8, color=TEXT_C)
            ax.text(0.01, 0.97,
                "灰色:0.05-0.1Hz 一次  シアン:0.1-0.3Hz 二次  橙:0.3-0.5Hz",
                transform=ax.transAxes, fontsize=7, color='#cccccc', va='top',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a1a', alpha=0.75))
        else:
            ax.text(0.5, 0.5, f"{ch}: データなし", ha='center', va='center',
                    transform=ax.transAxes, color=TEXT_C)

        sg_fnames[ch] = _save_panel(fig, out_dir, stem, panel_id)

    # ===== 2. 平均PSD パネル =====
    fig, ax = _make_panel_fig(figsize=(10, 4))
    ax.set_title("平均PSD（Welch法）3成分 + H_RMS", fontsize=10, color=TEXT_C)
    ax.set_xscale('log')

    for ch in CHANNELS:
        freqs_p, Pxx_p = psd_results[ch]
        if freqs_p is None:
            continue
        mask_p = (freqs_p >= F_LOW) & (freqs_p <= F_HIGH)
        p_db = 10 * np.log10(Pxx_p[mask_p] + 1e-40)
        ax.plot(freqs_p[mask_p], p_db, color=CHANNEL_COLORS[ch],
                linewidth=1.0, alpha=0.7, label=CHANNEL_LABELS[ch])

    if freqs_h is not None:
        mask_h = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        hrms_db = 10 * np.log10(Pxx_hrms[mask_h] + 1e-40)
        ax.plot(freqs_h[mask_h], hrms_db, color='white',
                linewidth=1.8, linestyle='-', label='H_RMS（主判定）', zorder=5)

    ax.set_xlim(F_LOW, F_HIGH)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax, horizontal=False)
    ax.grid(alpha=0.2, color=GRID_C, which='both')
    ax.set_xlabel("周波数 [Hz]", fontsize=8, color=TEXT_C)
    ax.set_ylabel("dB re (m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax.legend(fontsize=7, facecolor='#1a1a2a', labelcolor=TEXT_C, loc='best', framealpha=0.8)
    psd_fname = _save_panel(fig, out_dir, stem, "psd")

    # ===== 3. 帯域エネルギー重心 パネル =====
    BAND_DEFS = [
        ("一次帯\n0.05-0.10Hz", 0.05, 0.10, "#888888"),
        ("二次帯\n0.10-0.20Hz", 0.10, 0.20, "#4ecdc4"),
        ("二次帯\n0.20-0.30Hz", 0.20, 0.30, "#45b7d1"),
        ("高周波帯\n0.30-0.50Hz", 0.30, 0.50, "#ff9966"),
    ]

    fig, ax = _make_panel_fig(figsize=(10, 4))
    ax.set_title("H_RMS PSD と帯域エネルギー重心", fontsize=10, color=TEXT_C)
    ax.set_xscale('log')

    if freqs_h is not None and Pxx_hrms is not None:
        mask_disp = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        freqs_hrms = freqs_h[mask_disp]
        Pxx_hrms_arr = Pxx_hrms[mask_disp]
        psd_db = 10 * np.log10(Pxx_hrms_arr + 1e-40)
        ax.plot(freqs_hrms, psd_db, color='white', linewidth=1.2, label='H_RMS PSD', zorder=5)

        for band_label, f_lo_b, f_hi_b, bcolor in BAND_DEFS:
            m = (freqs_hrms >= f_lo_b) & (freqs_hrms <= f_hi_b)
            if m.sum() < 2:
                continue
            f_b = freqs_hrms[m]
            p_b = psd_db[m]
            ax.fill_between(f_b, p_b.min() - 5, p_b, alpha=0.18, color=bcolor, zorder=2)

            p_lin = Pxx_hrms_arr[m]
            centroid_f = np.sum(f_b * p_lin) / (np.sum(p_lin) + 1e-40)
            centroid_db = float(np.interp(centroid_f, freqs_hrms, psd_db))
            ax.axvline(x=centroid_f, color=bcolor, linewidth=1.2, linestyle='--', alpha=0.85, zorder=4)
            ax.annotate(
                f"{centroid_f:.3f}Hz\n(T={1/centroid_f:.1f}s)",
                xy=(centroid_f, centroid_db),
                xytext=(centroid_f * 1.12, centroid_db + 1.5),
                fontsize=6, color=bcolor,
                arrowprops=dict(arrowstyle='->', color=bcolor, lw=0.8),
            )

        # サマリーボックス
        rep_f_str    = f"{rep_f:.4f} Hz"   if not np.isnan(rep_f)    else "N/A"
        rep_T_str    = f"{rep_T:.1f} s"    if not np.isnan(rep_T)    else "N/A"
        rep_wave_str = f"{rep_wave:.1f} s" if not np.isnan(rep_wave) else "N/A"
        ratio_h2lo_str  = f"{ratio_h2lo:+.1f} dB"  if not np.isnan(ratio_h2lo)  else "N/A"
        ratio_h2ehz_str = f"{ratio_h2ehz:+.1f} dB" if not np.isnan(ratio_h2ehz) else "N/A"
        summary_txt = (
            f"H_RMS representative f  =  {rep_f_str}\n"
            f"Ground period               =  {rep_T_str}\n"
            f"Inferred ocean-wave period  =  {rep_wave_str}\n"
            f"\nPower ratio\n"
            f"  H_RMS(0.1–0.3) / H_RMS(0.3–0.5)  =  {ratio_h2lo_str}\n"
            f"  H_RMS(0.1–0.3) / ENZ(0.1–0.3)      =  {ratio_h2ehz_str}"
        )
        ax.text(
            0.02, 0.03, summary_txt,
            transform=ax.transAxes,
            fontsize=7.5,
            color='#ffe082',
            va='bottom',
            fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a00', alpha=0.88, edgecolor='#666600'),
        )

    ax.set_xlim(F_LOW, F_HIGH)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax, horizontal=False)
    ax.grid(alpha=0.2, color=GRID_C, which='both')
    ax.set_xlabel("周波数 [Hz]", fontsize=8, color=TEXT_C)
    ax.set_ylabel("dB re 1(m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax.legend(fontsize=7, facecolor='#1a1a2a', labelcolor=TEXT_C, loc='upper right', framealpha=0.8)
    centroid_fname = _save_panel(fig, out_dir, stem, "centroid")

    # ===== 4. H/V 比 パネル =====
    fig, ax = _make_panel_fig(figsize=(10, 4))
    ax.set_title("H/V比（振幅比）", fontsize=10, color=TEXT_C)
    ax.set_xscale('log')

    freqs_z, Pxx_z = psd_results["ENZ"]
    freqs_e, Pxx_e = psd_results["ENE"]
    freqs_n, Pxx_n = psd_results["ENN"]

    if freqs_z is not None and freqs_e is not None and freqs_n is not None:
        f_ref = freqs_z
        Pxx_e_i = np.interp(f_ref, freqs_e, Pxx_e)
        Pxx_n_i = np.interp(f_ref, freqs_n, Pxx_n)
        H_amp = np.sqrt(Pxx_e_i + Pxx_n_i)
        V_amp = np.sqrt(Pxx_z + 1e-40)
        HV = H_amp / (V_amp + 1e-40)

        mask_hv = (f_ref >= F_LOW) & (f_ref <= F_HIGH)
        ax.plot(f_ref[mask_hv], HV[mask_hv], color='#c084fc', linewidth=1.3)
        ax.axhline(y=1.0, color='white', linewidth=0.8, linestyle='--', alpha=0.5)
        ax.set_yscale('log')
        ax.set_xlim(F_LOW, F_HIGH)
        ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        add_band_guides(ax, horizontal=False)
        ax.grid(alpha=0.2, color=GRID_C, which='both')
        ax.set_xlabel("周波数 [Hz]", fontsize=8, color=TEXT_C)
        ax.set_ylabel("H/V", fontsize=7, color=TEXT_C)
        ax.axvspan(F_LOW, 0.10, alpha=0.35, color='gray', zorder=1)
        ax.text(0.98, 0.97, "H/V is diagnostic only;\nnot site amplification",
                ha='right', va='top', transform=ax.transAxes,
                fontsize=7, color='#888888', style='italic')
    else:
        ax.text(0.5, 0.5, "データ不足", ha='center', va='center',
                transform=ax.transAxes, color=TEXT_C)

    hv_fname = _save_panel(fig, out_dir, stem, "hv")

    # ===== 5. 帯域パワー時系列（絶対値）パネル =====
    fig, ax = _make_panel_fig(figsize=(12, 3.5))
    ax.set_title("帯域パワー時系列（絶対値）　15分窓・5分ステップ", fontsize=10, color=TEXT_C)

    for key, (t_bp, p_bp, color, label) in bp_results.items():
        if t_bp is not None:
            ax.plot(t_bp, p_bp, color=color, linewidth=1.1, label=label, alpha=0.85)

    ax.xaxis.set_major_formatter(time_fmt)
    ax.set_xlabel("時刻 (JST)", fontsize=8, color=TEXT_C)
    ax.set_ylabel("帯域パワー [dB]", fontsize=8, color=TEXT_C)
    ax.legend(fontsize=6.5, facecolor='#1a1a2a', labelcolor=TEXT_C,
              loc='upper right', framealpha=0.8, ncol=2)
    ax.grid(alpha=0.2, color=GRID_C)
    bp_abs_fname = _save_panel(fig, out_dir, stem, "bp_abs")

    # ===== 6. 帯域パワー時系列（相対変化）パネル =====
    fig, ax = _make_panel_fig(figsize=(12, 3.5))
    ax.set_title("帯域パワー時系列（相対変化・期間平均差し引き）", fontsize=10, color=TEXT_C)
    ax.axhline(y=0, color='white', linewidth=0.7, linestyle='--', alpha=0.4)

    for key, (t_bp, p_bp, color, label) in bp_results.items():
        if t_bp is not None:
            valid = ~np.isnan(p_bp)
            if valid.sum() > 0:
                mean_val = np.nanmean(p_bp)
                ax.plot(t_bp, p_bp - mean_val, color=color, linewidth=1.1, label=label, alpha=0.85)

    ax.xaxis.set_major_formatter(time_fmt)
    ax.set_xlabel("時刻 (JST)", fontsize=8, color=TEXT_C)
    ax.set_ylabel("ΔdB（平均差し引き）", fontsize=8, color=TEXT_C)
    ax.legend(fontsize=6.5, facecolor='#1a1a2a', labelcolor=TEXT_C,
              loc='upper right', framealpha=0.8, ncol=2)
    ax.grid(alpha=0.2, color=GRID_C)
    bp_rel_fname = _save_panel(fig, out_dir, stem, "bp_rel")

    # ===== 7. 昼夜比較 パネル（PSD + 棒グラフ横並び）=====
    fig, (ax_day_psd, ax_day_power) = _make_panel_fig(nrows=1, ncols=2, figsize=(14, 4))

    # PSD比較
    ax_day_psd.set_title("昼夜比較 PSD（H_RMS）", fontsize=10, color=TEXT_C)
    ax_day_psd.set_xscale('log')

    if freqs_h is not None:
        mask_c = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        night_db = 10 * np.log10(Pxx_hrms[mask_c] + 1e-40)
        ax_day_psd.plot(freqs_h[mask_c], night_db,
                        color='#4477cc', linewidth=1.5,
                        label=f"夜間 {t_start_jst.strftime('%H:%M')}-{t_end_jst.strftime('%H:%M')} JST")

    if freqs_h_day is not None:
        mask_d = (freqs_h_day >= F_LOW) & (freqs_h_day <= F_HIGH)
        day_db = 10 * np.log10(Pxx_hrms_day[mask_d] + 1e-40)
        ax_day_psd.plot(freqs_h_day[mask_d], day_db,
                        color='#ee8833', linewidth=1.5,
                        label=f"昼間 {day_start_jst.strftime('%H:%M')}-{day_end_jst.strftime('%H:%M')} JST")

    if freqs_h is None and freqs_h_day is None:
        ax_day_psd.text(0.5, 0.5, "データなし", ha='center', va='center',
                        transform=ax_day_psd.transAxes, color=TEXT_C)

    ax_day_psd.set_xlim(F_LOW, F_HIGH)
    ax_day_psd.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax_day_psd.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax_day_psd, horizontal=False)
    ax_day_psd.grid(alpha=0.2, color=GRID_C, which='both')
    ax_day_psd.set_xlabel("周波数 [Hz]", fontsize=8, color=TEXT_C)
    ax_day_psd.set_ylabel("dB re (m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax_day_psd.legend(fontsize=7.5, facecolor='#1a1a2a', labelcolor=TEXT_C, framealpha=0.8)

    # 棒グラフ
    ax_day_power.set_title("昼夜 帯域パワー比較", fontsize=10, color=TEXT_C)

    compare_bands = [
        (0.05, 0.10, "一次\n0.05-0.1Hz"),
        (0.10, 0.30, "二次\n0.1-0.3Hz"),
        (0.30, 0.50, "0.3-0.5Hz"),
    ]

    def _band_mean_db(freqs, Pxx, f_lo, f_hi):
        if freqs is None or Pxx is None:
            return np.nan
        mask = (freqs >= f_lo) & (freqs <= f_hi)
        if mask.sum() == 0:
            return np.nan
        bp = np.trapezoid(Pxx[mask], freqs[mask])
        return 10 * np.log10(bp + 1e-40)

    x_pos = np.arange(len(compare_bands))
    bar_w = 0.35
    night_vals = [_band_mean_db(freqs_h, Pxx_hrms, flo, fhi) for flo, fhi, _ in compare_bands]
    day_vals   = [_band_mean_db(freqs_h_day, Pxx_hrms_day, flo, fhi) for flo, fhi, _ in compare_bands]

    ax_day_power.bar(x_pos - bar_w / 2, night_vals, bar_w,
                     color='#4477cc', alpha=0.85, label='夜間')
    ax_day_power.bar(x_pos + bar_w / 2, day_vals, bar_w,
                     color='#ee8833', alpha=0.85, label='昼間')
    ax_day_power.set_xticks(x_pos)
    ax_day_power.set_xticklabels([lbl for _, _, lbl in compare_bands], fontsize=8, color=TEXT_C)
    ax_day_power.set_ylabel("帯域パワー [dB]", fontsize=8, color=TEXT_C)
    ax_day_power.legend(fontsize=8, facecolor='#1a1a2a', labelcolor=TEXT_C, framealpha=0.8)
    ax_day_power.grid(axis='y', alpha=0.2, color=GRID_C)

    for xi, (nv, dv) in enumerate(zip(night_vals, day_vals)):
        if not (np.isnan(nv) or np.isnan(dv)):
            diff = nv - dv
            clr = '#88ff88' if diff > 0 else '#ff8888'
            ax_day_power.text(xi, max(nv, dv) + 0.5, f"Δ{diff:+.1f}dB",
                              ha='center', va='bottom', fontsize=8, color=clr)

    fig.tight_layout()
    daynight_fname = _save_panel(fig, out_dir, stem, "daynight")

    # ===== 昼夜差サマリー（HTML用文字列）=====
    # 二次帯（0.1-0.3Hz）の昼夜差を文章化
    nv_sec = _band_mean_db(freqs_h, Pxx_hrms, 0.10, 0.30)
    dv_sec = _band_mean_db(freqs_h_day, Pxx_hrms_day, 0.10, 0.30)
    if not (np.isnan(nv_sec) or np.isnan(dv_sec)):
        diff_sec = nv_sec - dv_sec
        if diff_sec > 0:
            daynight_summary = f"0.1〜0.3Hz 帯の昼夜差 = Δ{diff_sec:+.1f} dB → 夜間の方がパワーが高い（マイクロセイズム優勢）"
        else:
            daynight_summary = f"0.1〜0.3Hz 帯の昼夜差 = Δ{diff_sec:+.1f} dB → 昼間の方がパワーが高い（人工ノイズの影響を要確認）"
    else:
        daynight_summary = "昼夜比較データなし"

    # ===== HTML 文字列生成 =====
    correction_status = "計器応答除去済（m/s 単位）" if any(corrected_map.get(ch, False) for ch in CHANNELS) else "生カウント値（counts 単位）"

    rep_f_str    = f"{rep_f:.4f} Hz"   if not np.isnan(rep_f)    else "N/A"
    rep_T_str    = f"{rep_T:.1f} s"    if not np.isnan(rep_T)    else "N/A"
    rep_wave_str = f"{rep_wave:.1f} s" if not np.isnan(rep_wave) else "N/A"
    ratio_h2lo_str  = f"{ratio_h2lo:+.1f} dB"  if not np.isnan(ratio_h2lo)  else "N/A"
    ratio_h2ehz_str = f"{ratio_h2ehz:+.1f} dB" if not np.isnan(ratio_h2ehz) else "N/A"
    rep_f_disp    = f"{rep_f:.4f} Hz"   if not np.isnan(rep_f)    else "—"
    rep_wave_disp = f"{rep_wave:.1f} s" if not np.isnan(rep_wave) else "—"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <title>R38DC マイクロセイズム診断レポート</title>
  <style>
    /* ダークテーマ */
    body {{ background: #0d1117; color: #e0e0e0; font-family: 'Helvetica Neue', sans-serif; max-width: 1100px; margin: 0 auto; padding: 2em; }}
    h1 {{ color: #ffffff; border-bottom: 2px solid #333; padding-bottom: 0.3em; }}
    h2 {{ color: #aaddff; margin-top: 2em; }}
    h3 {{ color: #88ccff; }}
    .summary-box {{ background: #1a1a00; border: 1px solid #666600; border-radius: 6px; padding: 1em 1.5em; font-family: monospace; font-size: 1.05em; color: #ffe082; margin: 1em 0; }}
    .panel-section {{ margin: 2em 0; }}
    .panel-section img {{ width: 100%; border-radius: 4px; }}
    .caption {{ background: #111827; border-left: 4px solid #4477cc; padding: 0.8em 1em; margin-top: 0.5em; font-size: 0.92em; line-height: 1.6; }}
    .caption ul {{ margin: 0.3em 0 0 1.2em; padding: 0; }}
    .wave-box {{ background: #001a2a; border: 1px solid #226688; border-radius: 6px; padding: 1em 1.5em; margin: 1em 0; }}
    .judge-box {{ background: #111; border: 1px solid #444; border-radius: 6px; padding: 1em 1.5em; margin: 1em 0; font-size: 0.9em; }}
    .judge-box li {{ margin: 0.3em 0; }}
    .highlight {{ color: #4ecdc4; }}
    .warn {{ color: #ff9966; }}
  </style>
</head>
<body>

<!-- ===== 1. ヘッダー ===== -->
<h1>R38DC マイクロセイズム診断レポート</h1>
<p>
  <strong>観測点:</strong> AM.R38DC.00（静岡県・標高120m・海岸から約20km）<br>
  <strong>夜間期間:</strong> {t_start_jst.strftime('%Y-%m-%d %H:%M')} 〜 {t_end_jst.strftime('%Y-%m-%d %H:%M')} JST<br>
  <strong>昼間期間:</strong> {day_start_jst.strftime('%Y-%m-%d %H:%M')} 〜 {day_end_jst.strftime('%Y-%m-%d %H:%M')} JST<br>
  <strong>計器応答除去:</strong> {correction_status}
</p>

<!-- ===== 2. 総合サマリーボックス ===== -->
<h2>総合サマリー</h2>
<div class="summary-box"><pre>
H_RMS representative f  =  {rep_f_str}
Ground period            =  {rep_T_str}
Inferred ocean-wave period = {rep_wave_str}

Power ratio
  H_RMS(0.1–0.3) / H_RMS(0.3–0.5)  =  {ratio_h2lo_str}
  H_RMS(0.1–0.3) / ENZ(0.1–0.3)     =  {ratio_h2ehz_str}
</pre></div>

<!-- ===== 3. スペクトログラム（ENZ）===== -->
<h2>スペクトログラム</h2>
<h3>ENZ（垂直動）</h3>
<div class="panel-section">
  <img src="{sg_fnames['ENZ']}" alt="ENZ スペクトログラム">
  <div class="caption">
    垂直動。R38DC の ENZ は MEMS 加速度計で、低周波まで感度がフラットです。ENE・ENN と同一センサー系統のため H/V 比の系統整合が良好です。
    <ul>
      <li>カラースケールはチャンネルごとに個別設定。</li>
    </ul>
  </div>
</div>

<!-- ENE（東西動）-->
<h3>ENE（東西水平動）</h3>
<div class="panel-section">
  <img src="{sg_fnames['ENE']}" alt="ENE スペクトログラム">
  <div class="caption">
    東西方向水平動。マイクロセイズムの主判定に使用。0.1〜0.3Hz 帯（二次マイクロセイズム）に持続的なエネルギーがあるかを確認します。
    <ul>
      <li>カラースケールはチャンネルごとに個別設定。</li>
    </ul>
  </div>
</div>

<!-- ENN（南北動）-->
<h3>ENN（南北水平動）</h3>
<div class="panel-section">
  <img src="{sg_fnames['ENN']}" alt="ENN スペクトログラム">
  <div class="caption">
    南北方向水平動。ENE と合わせて H_RMS（水平 RMS）を構成します。
    <ul>
      <li>カラースケールはチャンネルごとに個別設定。</li>
    </ul>
  </div>
</div>

<!-- ===== 4. 平均PSD ===== -->
<h2>平均PSD</h2>
<div class="panel-section">
  <img src="{psd_fname}" alt="平均PSD">
  <div class="caption">
    <ul>
      <li>Welch 法（窓長512秒・Hann窓・50%オーバーラップ）で推定した平均パワースペクトル密度です。</li>
      <li>白線（H_RMS）は ENE と ENN の線形パワー平均の平方根で、水平動の代表スペクトルです。dB で足し算していません。</li>
      <li>ENZ・ENE・ENN は全て MEMS 加速度計で、低周波まで感度フラットです。</li>
    </ul>
  </div>
</div>

<!-- ===== 5. 帯域エネルギー重心 ===== -->
<h2>帯域エネルギー重心</h2>
<div class="panel-section">
  <img src="{centroid_fname}" alt="帯域エネルギー重心">
  <div class="caption">
    <ul>
      <li>各周波数帯域内での H_RMS パワーの重心周波数（= Σ(f × P(f)) / ΣP(f)）を破線マーカーで示しています。</li>
      <li>重心周波数はピークよりも帯域全体のエネルギー分布を反映するため、散乱したスペクトルでも安定して読み取れます。</li>
      <li>代表周波数 = <span class="highlight">{rep_f_str}</span> / 地面周期 = <span class="highlight">{rep_T_str}</span> / 推定波浪周期 = <span class="highlight">{rep_wave_str}</span></li>
    </ul>
  </div>
</div>

<!-- ===== 6. H/V 比 ===== -->
<h2>H/V 比</h2>
<div class="panel-section">
  <img src="{hv_fname}" alt="H/V 比">
  <div class="caption">
    <ul>
      <li>水平動振幅 / 垂直動振幅の比率です（H = √(ENE_power + ENN_power)、V = √ENZ_power）。</li>
      <li>マイクロセイズムが等方的な体波であれば H/V ≈ 1 が期待されます。</li>
      <li>ENZ・ENE・ENN は同一 MEMS センサー系統のため、H/V 比の系統誤差が小さい。</li>
      <li>H/V is diagnostic only; not site amplification（地盤増幅係数ではない）。</li>
    </ul>
  </div>
</div>

<!-- ===== 7. 帯域パワー時系列（絶対値）===== -->
<h2>帯域パワー時系列（絶対値）</h2>
<div class="panel-section">
  <img src="{bp_abs_fname}" alt="帯域パワー時系列（絶対値）">
  <div class="caption">
    <ul>
      <li>15分窓・5分ステップで計算した各帯域の積分パワー（dB）の時系列です。</li>
      <li>H_RMS 0.1〜0.3Hz（シアン）がマイクロセイズム帯の主指標です。</li>
    </ul>
  </div>
</div>

<!-- ===== 8. 帯域パワー時系列（相対変化）===== -->
<h2>帯域パワー時系列（相対変化）</h2>
<div class="panel-section">
  <img src="{bp_rel_fname}" alt="帯域パワー時系列（相対変化）">
  <div class="caption">
    <ul>
      <li>上段の絶対値から期間平均を差し引いた相対変化（ΔdB）です。</li>
      <li>±2〜3dB の変動は海洋性マイクロセイズムとして自然な範囲です。</li>
      <li>大きな急増は嵐・うねりの到来、急減は静穏化を示す場合があります。</li>
    </ul>
  </div>
</div>

<!-- ===== 9. 昼夜比較 ===== -->
<h2>昼夜比較</h2>
<div class="panel-section">
  <img src="{daynight_fname}" alt="昼夜比較">
  <div class="caption">
    <ul>
      <li>夜間（{t_start_jst.strftime('%H:%M')}〜{t_end_jst.strftime('%H:%M')} JST）と昼間（{day_start_jst.strftime('%H:%M')}〜{day_end_jst.strftime('%H:%M')} JST）の H_RMS PSD を重ねて比較します。</li>
      <li>昼夜差が 0.1〜0.3Hz 帯で &lt; 3dB なら、人工ノイズの影響が少なくマイクロセイズムとして信頼度が高い。</li>
      <li>{daynight_summary}</li>
    </ul>
  </div>
</div>

<!-- ===== 10. 波浪データ照合 ===== -->
<h2>波浪データ照合</h2>
<div class="wave-box"><pre>
Observed H_RMS representative f  =  {rep_f_disp}
Inferred ocean-wave period (2/f)  =  {rep_wave_disp}
Nearby wave period (buoy/model)   =  [要照合: 気象庁・NOWPHAS 駿河湾沖]
Agreement                         =  [照合後に記入]
</pre>
<p>
  照合先:<br>
  気象庁 波浪実況: https://www.jma.go.jp/bosai/map.html#8/34.7/137.0/&amp;elem=wave<br>
  NOWPHAS（国土交通省港湾局）: 駿河湾沖ブイデータ
</p>
</div>

<!-- ===== 11. 総合判定ガイド ===== -->
<h2>総合判定ガイド</h2>
<div class="judge-box">
  <ul>
    <li>H_RMS 0.1〜0.3Hz に重心エネルギー集中 → 二次マイクロセイズム候補</li>
    <li>H/V ≈ 1（0.1〜0.3Hz）→ 等方な体波、マイクロセイズム支持</li>
    <li>昼夜差Δ &lt; 3dB（0.1〜0.3Hz）→ 地域人工ノイズ影響が少ない</li>
    <li>H_RMS(0.1-0.3)/H_RMS(0.3-0.5) &gt; +6dB → 低周波エネルギー優勢（マイクロセイズム支持）</li>
    <li>ENZ・ENE・ENN は全て MEMS 加速度計 → 低周波まで感度フラットで H/V 比の系統整合が良い</li>
  </ul>
</div>

</body>
</html>
"""

    html_path = out_dir / f"{stem}.html"
    html_path.write_text(html, encoding='utf-8')
    print(f"  HTMLレポート保存: {html_path}")
    return html_path


def plot_diagnostic(args):
    # 時刻設定
    t_start_jst = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    if args.end:
        t_end_jst = datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    else:
        t_end_jst = t_start_jst + timedelta(seconds=args.duration)

    t_start_utc = t_start_jst.astimezone(UTC)
    t_end_utc   = t_end_jst.astimezone(UTC)
    duration_h  = (t_end_jst - t_start_jst).total_seconds() / 3600
    total_sec   = (t_end_utc - t_start_utc).total_seconds()

    print(f"期間: {t_start_jst.strftime('%Y-%m-%d %H:%M')} JST → {t_end_jst.strftime('%Y-%m-%d %H:%M')} JST ({duration_h:.1f}h)")

    _CACHE.mkdir(parents=True, exist_ok=True)
    tag = t_start_jst.strftime('%Y%m%d_%H%M') + f"_{int(total_sec)}s"

    # ===== 夜間データ取得 =====
    ms_paths = {}
    for ch in CHANNELS:
        path = _CACHE / f"AM.{STATION}.{LOCATION}.{ch}.{tag}.ms"
        ms_paths[ch] = path
        if not args.no_download and not path.exists():
            download_channel_obspy(ch, t_start_jst, t_end_jst, path)
        elif path.exists():
            print(f"  {ch}: キャッシュ利用 ({path.stat().st_size:,} bytes)")

    # ===== 昼間データ（昼夜比較用）=====
    # 昼間: 同日 09:00〜15:00 JST
    day_date = t_start_jst.date()
    day_start_jst = datetime(day_date.year, day_date.month, day_date.day, 9, 0, 0, tzinfo=JST)
    day_end_jst   = datetime(day_date.year, day_date.month, day_date.day, 15, 0, 0, tzinfo=JST)
    day_tag = day_start_jst.strftime('%Y%m%d_%H%M') + "_21600s"

    day_paths = {}
    for ch in CHANNELS:
        p = _CACHE / f"AM.{STATION}.{LOCATION}.{ch}.{day_tag}.ms"
        day_paths[ch] = p
        if not p.exists():
            print(f"  昼間データ {ch}: ダウンロード中...")
            download_channel_obspy(ch, day_start_jst, day_end_jst, p)
        else:
            print(f"  昼間データ {ch}: キャッシュ利用")

    # ===== 計器応答情報取得 =====
    if args.no_correction:
        inv = None
        print("計器応答除去: スキップ（--no-correction 指定）")
    else:
        inv = fetch_inventory()

    # ===== 夜間データ読み込み・応答除去 =====
    print("夜間データ読み込み・処理中...")
    traces = {}
    fs_map = {}
    corrected_map = {}
    for ch in CHANNELS:
        path = ms_paths[ch]
        if not path.exists():
            print(f"  [WARN] {ch}: キャッシュなし")
            traces[ch] = None
            continue
        data, fs, corrected = load_and_correct_trace(path, inv)
        traces[ch]       = data
        fs_map[ch]       = fs
        corrected_map[ch] = corrected
        rms = np.sqrt(np.mean(data**2))
        unit = "m/s" if corrected else "counts"
        print(f"  {ch}: {len(data)/fs:.0f}秒  fs={fs}Hz  RMS={rms:.3e} {unit}")

    # ===== 昼間データ読み込み =====
    print("昼間データ読み込み・処理中...")
    day_traces = {}
    day_fs_map = {}
    for ch in CHANNELS:
        p = day_paths[ch]
        if not p.exists():
            day_traces[ch] = None
            continue
        data_d, fs_d, _ = load_and_correct_trace(p, inv)
        day_traces[ch] = data_d
        day_fs_map[ch] = fs_d

    # ===== PSD計算 =====
    WIN_SEC = min(512.0, total_sec / 4)
    print(f"PSD計算（Welch法 窓長={WIN_SEC:.0f}秒）...")
    psd_results = {}
    for ch in CHANNELS:
        if traces[ch] is None:
            psd_results[ch] = (None, None)
            continue
        psd_results[ch] = compute_psd_welch(traces[ch], fs_map[ch], win_sec=WIN_SEC)

    # 昼間PSD
    day_win_sec = min(512.0, 6 * 3600 / 4)
    day_psd = {}
    for ch in CHANNELS:
        if day_traces[ch] is None:
            day_psd[ch] = (None, None)
            continue
        day_psd[ch] = compute_psd_welch(day_traces[ch], day_fs_map[ch], win_sec=day_win_sec)

    # Horizontal RMS PSD（線形パワーで合成）
    freqs_h, Pxx_hrms = None, None
    if psd_results["ENE"][0] is not None and psd_results["ENN"][0] is not None:
        freqs_h, Pxx_hrms = compute_hrms_psd(psd_results["ENE"], psd_results["ENN"])

    freqs_h_day, Pxx_hrms_day = None, None
    if day_psd["ENE"][0] is not None and day_psd["ENN"][0] is not None:
        freqs_h_day, Pxx_hrms_day = compute_hrms_psd(day_psd["ENE"], day_psd["ENN"])

    # ピーク検出（Horizontal RMS を主判定）
    peak_info = {"main_peak_f_Hz": None}
    if freqs_h is not None:
        mask_peak = (freqs_h >= 0.05) & (freqs_h <= 0.50)
        peak_info = detect_microseism_peak(freqs_h[mask_peak], Pxx_hrms[mask_peak],
                                           f_lo_trend=0.05, f_hi_trend=0.50,
                                           f_lo_search=0.12, f_hi_search=0.35)

    # ===== 帯域パワー時系列計算 =====
    print("帯域パワー時系列計算中...")

    def hrms_time_series(f_lo, f_hi):
        datas_h = []
        for hch in ["ENE", "ENN"]:
            if traces[hch] is not None:
                datas_h.append(traces[hch])
        if len(datas_h) == 2:
            n = min(len(datas_h[0]), len(datas_h[1]))
            h_rms = np.sqrt((datas_h[0][:n]**2 + datas_h[1][:n]**2) / 2.0)
            return band_power_timeseries(h_rms, fs_map["ENE"], f_lo, f_hi)
        return None, None

    bp_configs = [
        ("H_RMS_01_03",  hrms_time_series,   0.10, 0.30, "#4ecdc4", "H_RMS 0.1〜0.3Hz（二次マイクロセイズム）"),
        ("ENZ_01_03",    "ENZ",               0.10, 0.30, "#ff6b6b", "ENZ 0.1〜0.3Hz"),
        ("H_RMS_005_01", hrms_time_series,   0.05, 0.10, "#888888", "H_RMS 0.05〜0.1Hz（一次マイクロセイズム）"),
        ("ENZ_03_05",    "ENZ",               0.30, 0.50, "#ff9999", "ENZ 0.3〜0.5Hz（高周波ノイズ参考）"),
        ("ENZ_1_10",     "ENZ",               1.00, 10.0, "#ffaa00", "ENZ 1〜10Hz（昼間人工ノイズ指標）"),
    ]

    bp_results = {}
    for key, src, flo, fhi, color, label in bp_configs:
        if callable(src):
            t_bp, p_bp = src(flo, fhi)
        else:
            ch = src
            if traces[ch] is not None:
                t_bp, p_bp = band_power_timeseries(traces[ch], fs_map[ch], flo, fhi)
            else:
                t_bp, p_bp = None, None
        bp_results[key] = (t_bp, p_bp, color, label)

    # ===== スペクトログラム個別カラースケール =====
    sg_scales = {}
    for ch in CHANNELS:
        if traces[ch] is None:
            sg_scales[ch] = (-200, -100)
            continue
        freqs_tmp, Pxx_tmp = psd_results[ch]
        if freqs_tmp is not None:
            mask = (freqs_tmp >= F_LOW) & (freqs_tmp <= F_HIGH)
            if mask.sum() > 0:
                db_vals = 10 * np.log10(Pxx_tmp[mask] + 1e-40)
                med  = np.median(db_vals)
                p5   = np.percentile(db_vals, 5)
                p95  = np.percentile(db_vals, 95)
                span = max(p95 - p5, 30)
                sg_scales[ch] = (med - span * 0.6, med + span * 0.4)
            else:
                sg_scales[ch] = (-200, -100)
        else:
            sg_scales[ch] = (-200, -100)
        vmin, vmax = sg_scales[ch]
        print(f"  スペクトログラム {ch}: {vmin:.1f} 〜 {vmax:.1f} dB")

    # ===== 図の作成 =====
    print("図作成中...")
    correction_note = "（計器応答除去済・m/s単位）" if any(corrected_map.get(ch, False) for ch in CHANNELS) else "（counts単位）"

    fig = plt.figure(figsize=(24, 28))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(
        f"R38DC 標高120m 海岸から約20km　マイクロセイズム診断{correction_note}\n"
        f"夜間: {t_start_jst.strftime('%Y-%m-%d %H:%M')}〜{t_end_jst.strftime('%H:%M')} JST ({duration_h:.1f}h)",
        fontsize=13, color='white', y=0.995
    )

    # GridSpec レイアウト
    # 最外: 4段
    #   段0: スペクトログラム(左) + PSD/ピーク/H/V(右)  高さ比 3
    #   段1: 帯域パワー絶対dB版                          高さ比 1.3
    #   段2: 帯域パワー相対変化版                        高さ比 1.3
    #   段3: 昼夜比較パネル                              高さ比 1.5
    outer = gridspec.GridSpec(
        4, 1,
        height_ratios=[3.2, 1.3, 1.3, 1.6],
        hspace=0.30,
        left=0.07, right=0.97,
        top=0.975, bottom=0.06,
    )

    # 段0: 左列（スペクトログラム3段）+ 右列（PSD/ピーク/H/V3段）
    top_gs = gridspec.GridSpecFromSubplotSpec(
        3, 2,
        subplot_spec=outer[0],
        width_ratios=[2.5, 1.6],
        hspace=0.08,
        wspace=0.10,
    )
    # 右列をさらに3段に分割
    right_gs = gridspec.GridSpecFromSubplotSpec(
        3, 1,
        subplot_spec=top_gs[:, 1],
        hspace=0.35,
    )

    def make_time_formatter(t0_jst):
        def fmt(x, pos):
            return (t0_jst + timedelta(seconds=x)).strftime('%H:%M')
        return matplotlib.ticker.FuncFormatter(fmt)

    time_fmt = make_time_formatter(t_start_jst)

    # ===== 左列: スペクトログラム =====
    SG_WIN_SEC = 100.0
    SG_OVERLAP = 0.90

    for i, ch in enumerate(CHANNELS):
        ax_sg = fig.add_subplot(top_gs[i, 0])
        style_ax(ax_sg)

        if traces[ch] is None:
            ax_sg.text(0.5, 0.5, f"{ch}: データなし", ha='center', va='center',
                       transform=ax_sg.transAxes, color=TEXT_C)
            continue

        data = traces[ch]
        fs   = fs_map[ch]
        nfft_sg = int(SG_WIN_SEC * fs)
        novl_sg = int(nfft_sg * SG_OVERLAP)
        vmin_ch, vmax_ch = sg_scales[ch]

        ax_sg.specgram(data, Fs=fs, NFFT=nfft_sg, noverlap=novl_sg,
                       cmap='inferno', scale='dB',
                       vmin=vmin_ch, vmax=vmax_ch)
        ax_sg.set_ylim(F_LOW, F_HIGH)
        add_band_guides(ax_sg, horizontal=True)

        # 帯域注記（行0のみ）
        if i == 0:
            ax_sg.text(0.01, 0.97,
                "灰色:0.05-0.1Hz 一次  シアン:0.1-0.3Hz 二次  橙:0.3-0.5Hz",
                transform=ax_sg.transAxes, fontsize=6.5, color='#cccccc', va='top',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a1a', alpha=0.75))

        ax_sg.set_ylabel(f"{CHANNEL_LABELS[ch]}\n周波数 [Hz]", fontsize=8, color=TEXT_C)
        if i < 2:
            ax_sg.set_xticklabels([])
        else:
            ax_sg.xaxis.set_major_formatter(time_fmt)
            ax_sg.set_xlabel("時刻 (JST)", fontsize=8, color=TEXT_C)
            ax_sg.text(0.01, -0.12,
                "水平成分 ENE/ENN を主判定。ENZ・ENE・ENN は全て MEMS 加速度計で低周波まで感度フラット。",
                transform=ax_sg.transAxes, fontsize=6.5, color='#aaaaaa')

    # ===== 右上: 平均PSD（3成分 + H_RMS重ね描き）=====
    ax_psd = fig.add_subplot(right_gs[0])
    style_ax(ax_psd)
    ax_psd.set_title("平均PSD（Welch法）", fontsize=9, color=TEXT_C)
    ax_psd.set_xscale('log')

    for ch in CHANNELS:
        freqs_p, Pxx_p = psd_results[ch]
        if freqs_p is None:
            continue
        mask_p = (freqs_p >= F_LOW) & (freqs_p <= F_HIGH)
        p_db = 10 * np.log10(Pxx_p[mask_p] + 1e-40)
        ax_psd.plot(freqs_p[mask_p], p_db, color=CHANNEL_COLORS[ch],
                    linewidth=1.0, alpha=0.7, label=CHANNEL_LABELS[ch])

    if freqs_h is not None:
        mask_h = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        hrms_db = 10 * np.log10(Pxx_hrms[mask_h] + 1e-40)
        ax_psd.plot(freqs_h[mask_h], hrms_db, color='white',
                    linewidth=1.8, linestyle='-', label='H_RMS（主判定）', zorder=5)

    ax_psd.set_xlim(F_LOW, F_HIGH)
    ax_psd.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax_psd.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax_psd, horizontal=False)
    ax_psd.grid(alpha=0.2, color=GRID_C, which='both')
    ax_psd.set_ylabel("dB re (m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax_psd.legend(fontsize=6.5, facecolor='#1a1a2a', labelcolor=TEXT_C, loc='best', framealpha=0.8)

    # ===== 右中: H_RMS PSD ＋ 帯域重心周波数 =====
    ax_peak = fig.add_subplot(right_gs[1])
    style_ax(ax_peak)
    ax_peak.set_title("H_RMS PSD と帯域エネルギー重心", fontsize=9, color=TEXT_C)
    ax_peak.set_xscale('log')

    # 帯域定義: (ラベル, f_lo, f_hi, 色)
    BAND_DEFS = [
        ("一次帯\n0.05-0.10Hz", 0.05, 0.10, "#888888"),
        ("二次帯\n0.10-0.20Hz", 0.10, 0.20, "#4ecdc4"),
        ("二次帯\n0.20-0.30Hz", 0.20, 0.30, "#45b7d1"),
        ("高周波帯\n0.30-0.50Hz", 0.30, 0.50, "#ff9966"),
    ]

    if freqs_h is not None and Pxx_hrms is not None:
        mask_disp = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        freqs_hrms = freqs_h[mask_disp]
        Pxx_hrms_arr = Pxx_hrms[mask_disp]

        # H_RMS PSDをdB表示
        psd_db = 10 * np.log10(Pxx_hrms_arr + 1e-40)
        ax_peak.plot(freqs_hrms, psd_db, color='white', linewidth=1.2, label='H_RMS PSD', zorder=5)

        # 各帯域を塗り潰し + 重心周波数マーカー
        for band_label, f_lo_b, f_hi_b, bcolor in BAND_DEFS:
            m = (freqs_hrms >= f_lo_b) & (freqs_hrms <= f_hi_b)
            if m.sum() < 2:
                continue
            f_b = freqs_hrms[m]
            p_b = psd_db[m]
            ax_peak.fill_between(f_b, p_b.min() - 5, p_b, alpha=0.18, color=bcolor, zorder=2)

            # 帯域重心周波数（線形パワーで重み付き平均）
            p_lin = Pxx_hrms_arr[m]
            centroid_f = np.sum(f_b * p_lin) / (np.sum(p_lin) + 1e-40)
            # 重心でのdB値（補間）
            centroid_db = float(np.interp(centroid_f, freqs_hrms, psd_db))
            ax_peak.axvline(x=centroid_f, color=bcolor, linewidth=1.2, linestyle='--', alpha=0.85, zorder=4)
            ax_peak.annotate(
                f"{centroid_f:.3f}Hz\n(T={1/centroid_f:.1f}s)",
                xy=(centroid_f, centroid_db),
                xytext=(centroid_f * 1.12, centroid_db + 1.5),
                fontsize=5.5, color=bcolor,
                arrowprops=dict(arrowstyle='->', color=bcolor, lw=0.8),
            )

    # ===== 帯域パワー比を計算（右中パネル内に表示） =====
    def _band_power(freqs, Pxx, f_lo, f_hi):
        """指定帯域の積分パワー（線形スケール）を返す。データなし/空の場合は nan。"""
        if freqs is None or Pxx is None:
            return np.nan
        mask = (freqs >= f_lo) & (freqs <= f_hi)
        if mask.sum() < 2:
            return np.nan
        return float(np.trapezoid(Pxx[mask], freqs[mask]))

    if freqs_h is not None and Pxx_hrms is not None:
        bp_hrms_02  = _band_power(freqs_h, Pxx_hrms, 0.10, 0.30)   # H_RMS 0.1-0.3Hz
        bp_hrms_hi  = _band_power(freqs_h, Pxx_hrms, 0.30, 0.50)   # H_RMS 0.3-0.5Hz
        bp_ehz_02   = _band_power(*psd_results["ENZ"],  0.10, 0.30) # ENZ  0.1-0.3Hz

        ratio_h2lo  = (10 * np.log10(bp_hrms_02 / (bp_hrms_hi + 1e-60))
                       if not np.isnan(bp_hrms_hi) and bp_hrms_hi > 0 else np.nan)
        ratio_h2ehz = (10 * np.log10(bp_hrms_02 / (bp_ehz_02  + 1e-60))
                       if not np.isnan(bp_ehz_02)  and bp_ehz_02  > 0 else np.nan)

        # 二次帯（0.1-0.3Hz）の H_RMS 重心周波数
        mask_sec = (freqs_h >= 0.10) & (freqs_h <= 0.30)
        if mask_sec.sum() >= 2:
            _p = Pxx_hrms[mask_sec]
            _f = freqs_h[mask_sec]
            rep_f    = float(np.sum(_f * _p) / (np.sum(_p) + 1e-60))
            rep_T    = 1.0 / rep_f
            rep_wave = 2.0 / rep_f
        else:
            rep_f = rep_T = rep_wave = np.nan

        # ---- サマリーボックス（大きめフォント）----
        ratio_h2lo_str  = f"{ratio_h2lo:+.1f} dB"  if not np.isnan(ratio_h2lo)  else "N/A"
        ratio_h2ehz_str = f"{ratio_h2ehz:+.1f} dB" if not np.isnan(ratio_h2ehz) else "N/A"
        rep_f_str    = f"{rep_f:.4f} Hz"   if not np.isnan(rep_f)    else "N/A"
        rep_T_str    = f"{rep_T:.1f} s"    if not np.isnan(rep_T)    else "N/A"
        rep_wave_str = f"{rep_wave:.1f} s" if not np.isnan(rep_wave) else "N/A"

        summary_txt = (
            f"H_RMS representative f  =  {rep_f_str}\n"
            f"Ground period               =  {rep_T_str}\n"
            f"Inferred ocean-wave period  =  {rep_wave_str}\n"
            f"\n"
            f"Power ratio\n"
            f"  H_RMS(0.1–0.3) / H_RMS(0.3–0.5)  =  {ratio_h2lo_str}\n"
            f"  H_RMS(0.1–0.3) / ENZ(0.1–0.3)      =  {ratio_h2ehz_str}"
        )
        ax_peak.text(
            0.02, 0.03, summary_txt,
            transform=ax_peak.transAxes,
            fontsize=7.5,
            color='#ffe082',
            va='bottom',
            fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a00', alpha=0.88, edgecolor='#666600'),
        )
    else:
        rep_f = rep_T = rep_wave = np.nan
        ratio_h2lo = ratio_h2ehz = np.nan

    ax_peak.set_xlim(F_LOW, F_HIGH)
    ax_peak.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax_peak.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax_peak, horizontal=False)
    ax_peak.grid(alpha=0.2, color=GRID_C, which='both')
    ax_peak.set_ylabel("dB re 1(m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax_peak.legend(fontsize=6, facecolor='#1a1a2a', labelcolor=TEXT_C, loc='upper right', framealpha=0.8)

    # ===== 右下: H/V比（対数縦軸）=====
    ax_hv = fig.add_subplot(right_gs[2])
    style_ax(ax_hv)
    ax_hv.set_title("H/V比（振幅比）", fontsize=9, color=TEXT_C)
    ax_hv.set_xscale('log')

    freqs_z, Pxx_z = psd_results["ENZ"]
    freqs_e, Pxx_e = psd_results["ENE"]
    freqs_n, Pxx_n = psd_results["ENN"]

    if freqs_z is not None and freqs_e is not None and freqs_n is not None:
        f_ref = freqs_z
        Pxx_e_i = np.interp(f_ref, freqs_e, Pxx_e)
        Pxx_n_i = np.interp(f_ref, freqs_n, Pxx_n)
        # 振幅比（線形パワーから計算）
        H_amp = np.sqrt(Pxx_e_i + Pxx_n_i)
        V_amp = np.sqrt(Pxx_z + 1e-40)
        HV = H_amp / (V_amp + 1e-40)

        mask_hv = (f_ref >= F_LOW) & (f_ref <= F_HIGH)
        ax_hv.plot(f_ref[mask_hv], HV[mask_hv], color='#c084fc', linewidth=1.3)
        ax_hv.axhline(y=1.0, color='white', linewidth=0.8, linestyle='--', alpha=0.5)
        ax_hv.set_yscale('log')
        ax_hv.set_xlim(F_LOW, F_HIGH)
        ax_hv.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
        ax_hv.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        add_band_guides(ax_hv, horizontal=False)
        ax_hv.grid(alpha=0.2, color=GRID_C, which='both')
        ax_hv.set_xlabel("周波数 [Hz]", fontsize=7, color=TEXT_C)
        ax_hv.set_ylabel("H/V", fontsize=7, color=TEXT_C)

        # 0.1Hz未満は網掛け
        ax_hv.axvspan(F_LOW, 0.10, alpha=0.35, color='gray', zorder=1)
        ax_hv.text(0.98, 0.97, "H/V is diagnostic only;\nnot site amplification",
                   ha='right', va='top', transform=ax_hv.transAxes,
                   fontsize=6, color='#888888', style='italic')
    else:
        ax_hv.text(0.5, 0.5, "データ不足", ha='center', va='center',
                   transform=ax_hv.transAxes, color=TEXT_C)

    # ===== 段1: 帯域パワー絶対dB版 =====
    ax_abs = fig.add_subplot(outer[1])
    style_ax(ax_abs)
    ax_abs.set_title("帯域パワー時系列（絶対値）　15分窓・5分ステップ", fontsize=9, color=TEXT_C)

    for key, (t_bp, p_bp, color, label) in bp_results.items():
        if t_bp is not None:
            ax_abs.plot(t_bp, p_bp, color=color, linewidth=1.1, label=label, alpha=0.85)

    ax_abs.xaxis.set_major_formatter(time_fmt)
    ax_abs.set_ylabel("帯域パワー [dB]", fontsize=8, color=TEXT_C)
    ax_abs.legend(fontsize=6.5, facecolor='#1a1a2a', labelcolor=TEXT_C,
                  loc='upper right', framealpha=0.8, ncol=2)
    ax_abs.grid(alpha=0.2, color=GRID_C)

    # ===== 段2: 帯域パワー相対変化版（期間平均差し引き）=====
    ax_rel = fig.add_subplot(outer[2])
    style_ax(ax_rel)
    ax_rel.set_title("帯域パワー時系列（相対変化・期間平均差し引き）", fontsize=9, color=TEXT_C)
    ax_rel.axhline(y=0, color='white', linewidth=0.7, linestyle='--', alpha=0.4)

    for key, (t_bp, p_bp, color, label) in bp_results.items():
        if t_bp is not None:
            valid = ~np.isnan(p_bp)
            if valid.sum() > 0:
                mean_val = np.nanmean(p_bp)
                ax_rel.plot(t_bp, p_bp - mean_val, color=color, linewidth=1.1, label=label, alpha=0.85)

    ax_rel.xaxis.set_major_formatter(time_fmt)
    ax_rel.set_xlabel("時刻 (JST)", fontsize=8, color=TEXT_C)
    ax_rel.set_ylabel("ΔdB（平均差し引き）", fontsize=8, color=TEXT_C)
    ax_rel.legend(fontsize=6.5, facecolor='#1a1a2a', labelcolor=TEXT_C,
                  loc='upper right', framealpha=0.8, ncol=2)
    ax_rel.grid(alpha=0.2, color=GRID_C)

    # ===== 段3: 昼夜比較パネル =====
    day_gs = gridspec.GridSpecFromSubplotSpec(
        1, 2,
        subplot_spec=outer[3],
        wspace=0.12,
    )
    ax_day_psd   = fig.add_subplot(day_gs[0, 0])
    ax_day_power = fig.add_subplot(day_gs[0, 1])
    style_ax(ax_day_psd)
    style_ax(ax_day_power)
    ax_day_psd.set_title("昼夜比較 PSD（H_RMS）", fontsize=9, color=TEXT_C)
    ax_day_psd.set_xscale('log')

    # 夜間 H_RMS
    if freqs_h is not None:
        mask_c = (freqs_h >= F_LOW) & (freqs_h <= F_HIGH)
        night_db = 10 * np.log10(Pxx_hrms[mask_c] + 1e-40)
        ax_day_psd.plot(freqs_h[mask_c], night_db,
                        color='#4477cc', linewidth=1.5, label=f"夜間 {t_start_jst.strftime('%H:%M')}-{t_end_jst.strftime('%H:%M')} JST")

    # 昼間 H_RMS
    if freqs_h_day is not None:
        mask_d = (freqs_h_day >= F_LOW) & (freqs_h_day <= F_HIGH)
        day_db = 10 * np.log10(Pxx_hrms_day[mask_d] + 1e-40)
        ax_day_psd.plot(freqs_h_day[mask_d], day_db,
                        color='#ee8833', linewidth=1.5, label=f"昼間 {day_start_jst.strftime('%H:%M')}-{day_end_jst.strftime('%H:%M')} JST")

    if freqs_h is None and freqs_h_day is None:
        ax_day_psd.text(0.5, 0.5, "データなし", ha='center', va='center',
                        transform=ax_day_psd.transAxes, color=TEXT_C)

    ax_day_psd.set_xlim(F_LOW, F_HIGH)
    ax_day_psd.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter('%.2f'))
    ax_day_psd.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    add_band_guides(ax_day_psd, horizontal=False)
    ax_day_psd.grid(alpha=0.2, color=GRID_C, which='both')
    ax_day_psd.set_xlabel("周波数 [Hz]", fontsize=8, color=TEXT_C)
    ax_day_psd.set_ylabel("dB re (m/s)²/Hz", fontsize=7, color=TEXT_C)
    ax_day_psd.legend(fontsize=7.5, facecolor='#1a1a2a', labelcolor=TEXT_C, framealpha=0.8)

    # 帯域パワー棒グラフ比較
    ax_day_power.set_title("昼夜 帯域パワー比較", fontsize=9, color=TEXT_C)

    compare_bands = [
        (0.05, 0.10, "一次\n0.05-0.1Hz"),
        (0.10, 0.30, "二次\n0.1-0.3Hz"),
        (0.30, 0.50, "0.3-0.5Hz"),
    ]

    def band_mean_db(freqs, Pxx, f_lo, f_hi):
        if freqs is None:
            return np.nan
        mask = (freqs >= f_lo) & (freqs <= f_hi)
        if mask.sum() == 0:
            return np.nan
        bp = np.trapezoid(Pxx[mask], freqs[mask])
        return 10 * np.log10(bp + 1e-40)

    x_pos = np.arange(len(compare_bands))
    bar_w = 0.35
    night_vals = [band_mean_db(freqs_h, Pxx_hrms, flo, fhi) for flo, fhi, _ in compare_bands]
    day_vals   = [band_mean_db(freqs_h_day, Pxx_hrms_day, flo, fhi) for flo, fhi, _ in compare_bands]

    bars_n = ax_day_power.bar(x_pos - bar_w/2, night_vals, bar_w,
                               color='#4477cc', alpha=0.85, label='夜間')
    bars_d = ax_day_power.bar(x_pos + bar_w/2, day_vals, bar_w,
                               color='#ee8833', alpha=0.85, label='昼間')
    ax_day_power.set_xticks(x_pos)
    ax_day_power.set_xticklabels([lbl for _, _, lbl in compare_bands], fontsize=8, color=TEXT_C)
    ax_day_power.set_ylabel("帯域パワー [dB]", fontsize=8, color=TEXT_C)
    ax_day_power.legend(fontsize=8, facecolor='#1a1a2a', labelcolor=TEXT_C, framealpha=0.8)
    ax_day_power.grid(axis='y', alpha=0.2, color=GRID_C)

    # 昼夜差の注記
    for xi, (nv, dv) in enumerate(zip(night_vals, day_vals)):
        if not (np.isnan(nv) or np.isnan(dv)):
            diff = nv - dv
            clr = '#88ff88' if diff > 0 else '#ff8888'
            ax_day_power.text(xi, max(nv, dv) + 0.5, f"Δ{diff:+.1f}dB",
                              ha='center', va='bottom', fontsize=7, color=clr)

    # ===== 波浪データ照合欄（判定ガイド直上）=====
    _rep_f_disp    = f"{rep_f:.4f} Hz"   if not np.isnan(rep_f)    else "—"
    _rep_wave_disp = f"{rep_wave:.1f} s" if not np.isnan(rep_wave) else "—"
    buoy_note = (
        "【波浪データ照合】"
        f"  Observed H_RMS representative f = {_rep_f_disp}"
        f"  │  Inferred ocean-wave period (2/f) = {_rep_wave_disp}"
        "  │  Nearby wave period from buoy/wave model = [要照合: 気象庁・NOWPHAS 駿河湾沖 または 波浪モデル]"
        "  │  Agreement = [照合後に記入]"
    )
    fig.text(0.5, 0.025, buoy_note, ha='center', fontsize=7.0, color='#88ddff', wrap=True)

    # ===== 判定ガイド（図最下部）=====
    judge_text = (
        "【マイクロセイズム判定ガイド】"
        "  ■ H_RMS 0.1〜0.3Hz に重心エネルギー集中 → 二次マイクロセイズム候補"
        "  ■ H/V ≈ 1（0.1〜0.3Hz）→ 等方な体波、マイクロセイズム支持"
        "  ■ 昼夜差Δ < 3dB（0.1〜0.3Hz）→ 地域人工ノイズ影響が少ない"
        "  ■ H_RMS(0.1-0.3)/H_RMS(0.3-0.5) > +6dB → 低周波エネルギー優勢（マイクロセイズム支持）"
        "  ■ ENZ・ENE・ENN は全て MEMS 加速度計 → 低周波まで感度フラットで H/V 比の系統整合が良い"
    )
    fig.text(0.5, 0.005, judge_text, ha='center', fontsize=7.0, color='#aaaaaa', wrap=True)

    # ===== 保存 =====
    out_name = (
        f"R38DC_microseism_diagnostic_improved"
        f"_{t_start_jst.strftime('%Y%m%d_%H%M')}-{t_end_jst.strftime('%H%M')}JST.png"
    )
    out_path = _OUTDIR / out_name
    print(f"保存中: {out_path}")
    fig.savefig(str(out_path), dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"完了: {out_path}")

    # ===== HTML レポート生成 =====
    stem = out_name.replace('.png', '')
    html_path = generate_html_report(
        out_dir=_OUTDIR,
        stem=stem,
        t_start_jst=t_start_jst,
        t_end_jst=t_end_jst,
        day_start_jst=day_start_jst,
        day_end_jst=day_end_jst,
        psd_results=psd_results,
        day_psd=day_psd,
        freqs_h=freqs_h, Pxx_hrms=Pxx_hrms,
        freqs_h_day=freqs_h_day, Pxx_hrms_day=Pxx_hrms_day,
        bp_results=bp_results,
        traces=traces,
        fs_map=fs_map,
        sg_scales=sg_scales,
        peak_info=peak_info,
        rep_f=rep_f, rep_T=rep_T, rep_wave=rep_wave,
        ratio_h2lo=ratio_h2lo, ratio_h2ehz=ratio_h2ehz,
        corrected_map=corrected_map,
        time_fmt=time_fmt,
    )
    print(f"HTML: {html_path}")
    return out_path, html_path


def main():
    ap = argparse.ArgumentParser(description="R38DC マイクロセイズム診断図（改良版）")
    ap.add_argument('--start',         required=True, help='開始時刻 JST (例: "2026-05-25 20:00:00")')
    ap.add_argument('--end',           help='終了時刻 JST')
    ap.add_argument('--duration',      type=int, default=21600, help='継続秒数（--end未指定時）')
    ap.add_argument('--no-download',   action='store_true', help='キャッシュを使用（ダウンロードしない）')
    ap.add_argument('--no-correction', action='store_true', help='計器応答除去をスキップ')
    args = ap.parse_args()

    import subprocess
    out, html_path = plot_diagnostic(args)
    subprocess.Popen(['open', str(out)])
    subprocess.Popen(['open', str(html_path)])


if __name__ == '__main__':
    main()
