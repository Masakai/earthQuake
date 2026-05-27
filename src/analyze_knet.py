#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K-NET / KiK-net 強震波形分析ツール

NIED（防災科研）の K-NET / KiK-net 強震波形（ASCII形式）を読み込み、
合成加速度・STA/LTA・計測震度・スペクトログラム・震源地図を生成する。

データ入手:
    1. https://www.kyoshin.bosai.go.jp/ から手動DL（NIEDアカウント必要）
    2. tar.gz を解凍し、3成分（NS/EW/UD）ASCII を data/knet/ に配置
    3. ファイル名規則: {観測点コード}{YYMMDDHHmm}.{NS|EW|UD}
       例: SZO0010605240705.NS

使い方:
    # 観測点コードとイベント時刻（ファイル名のYYMMDDHHmm部分）で指定
    .venv/bin/python3 src/analyze_knet.py --station SZO001 --event 0605240705

    # ファイルパス直接指定（同一プレフィクスの3成分を自動検出）
    .venv/bin/python3 src/analyze_knet.py --file data/knet/SZO0010605240705.NS

    # 複数の地震・観測点を data/knet/ から自動検出してリスト
    .venv/bin/python3 src/analyze_knet.py --list

設計方針:
    analyze_rs.py（Raspberry Shake版）とは独立。共通関数のみ再利用する。

Copyright (c) 2026 Masanori Sakai
"""

import argparse
import glob
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.ticker

try:
    from obspy import read as obspy_read
except ImportError:
    sys.exit("[ERROR] obspy がインストールされていません。.venv/bin/pip install obspy を実行してください。")

# analyze_rs.py から共通関数を再利用（analyze_rs.py 自体は変更しない）
_SRC = pathlib.Path(__file__).parent
sys.path.insert(0, str(_SRC))
from analyze_rs import (
    _setup_font,
    compute_stalta,
    compute_intensity_timeseries,
    compute_spectrogram,
    plot_map,
)
from jma_intensity_realtime import apply_jma_filter_time, jma_scale_from_I

# ===== 定数 =====
JST          = timezone(timedelta(hours=9))
UTC          = timezone.utc
NETWORK      = 'BO'   # obspy K-NET reader が割り当てるネットワークコード
KNET_CHANNELS = ['NS', 'EW', 'UD']  # K-NET の3成分


# ===== K-NET ファイル探索 =====
def find_knet_files(knet_dir: pathlib.Path, station: str = None,
                    event: str = None) -> list[tuple[str, str, dict]]:
    """data/knet/ から K-NET ファイル群を探索し、(観測点コード, イベントタグ, {成分: パス}) のリストを返す。

    K-NET ファイル名: {station(6)}{YYMMDDHHmm}.{NS|EW|UD}
    """
    pattern = '*'
    if station:
        pattern = f'{station}*'

    found = {}  # key=(station, event_tag), val={channel: path}
    for ch in KNET_CHANNELS:
        for p in knet_dir.glob(f'{pattern}.{ch}'):
            name = p.stem  # 例: SZO0010605240705
            if len(name) < 10:
                continue
            # 末尾10桁が YYMMDDHHmm、それより前が観測点コード
            event_tag = name[-10:]
            sta_code  = name[:-10]
            if event and event_tag != event:
                continue
            if not sta_code or not event_tag.isdigit():
                continue
            key = (sta_code, event_tag)
            found.setdefault(key, {})[ch] = p

    # 3成分揃ったものだけ返す（NS+EWは必須、UDは任意でも可）
    result = []
    for (sta_code, event_tag), chans in sorted(found.items()):
        result.append((sta_code, event_tag, chans))
    return result


def list_available(knet_dir: pathlib.Path):
    items = find_knet_files(knet_dir)
    if not items:
        print(f"[INFO] {knet_dir} に K-NET ファイルが見つかりません。")
        print(f"       README.md を参照して手動DLしてください。")
        return
    print(f"\n== 利用可能な K-NET 観測点・イベント ({knet_dir}) ==")
    for sta_code, event_tag, chans in items:
        # event_tag = YYMMDDHHmm → 表示用に整形
        try:
            t = datetime.strptime(event_tag, '%y%m%d%H%M').replace(tzinfo=JST)
            t_str = t.strftime('%Y-%m-%d %H:%M JST')
        except ValueError:
            t_str = event_tag
        ch_str = '+'.join(sorted(chans.keys()))
        print(f"  station={sta_code}  event={event_tag}  ({t_str})  channels={ch_str}")


# ===== K-NET 読み込み =====
def load_knet_traces(file_map: dict) -> dict:
    """{成分: パス} → {成分: obspy.Trace} に変換。data を gal 単位に揃える。

    obspy の K-NET reader は data を raw counts のまま返し、
    Trace.stats.calib に「m/s² per count」(= 分子/分母 × 0.01) を入れる。
    本関数は calib を適用したあと 100 倍して gal (= cm/s²) 単位に揃え、
    以後の処理が「Trace.data は gal」を前提にできるようにする。
    """
    traces = {}
    for ch, p in file_map.items():
        st = obspy_read(str(p))
        tr = st[0]
        # raw counts → m/s² → gal (cm/s²)
        tr.data = tr.data.astype(np.float64) * tr.stats.calib * 100.0
        # calib を 1.0 にして「以後は gal 単位」と明示
        tr.stats.calib = 1.0
        traces[ch] = tr
    return traces


# ===== メイン =====
def main():
    ap = argparse.ArgumentParser(
        description='K-NET / KiK-net 強震波形分析グラフ生成',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('--knet-dir', default='data/knet',
                    help='K-NET ASCII配置ディレクトリ (デフォルト: data/knet)')
    ap.add_argument('--station', default=None,
                    help='観測点コード（例: SZO001）。--file 未指定時に使用')
    ap.add_argument('--event', default=None,
                    help='イベントタグ YYMMDDHHmm（例: 0605240705）。--file 未指定時に使用')
    ap.add_argument('--file', default=None,
                    help='K-NET ASCIIファイル1つを指定（同プレフィクスの3成分を自動検出）')
    ap.add_argument('--list', action='store_true',
                    help='--knet-dir 内の観測点・イベント一覧を表示して終了')
    ap.add_argument('--sta', type=float, default=1.0, help='STA秒数 (デフォルト: 1.0)')
    ap.add_argument('--lta', type=float, default=20.0, help='LTA秒数 (デフォルト: 20.0)')
    ap.add_argument('--trig', type=float, default=3.5, help='STA/LTA閾値 (デフォルト: 3.5)')
    ap.add_argument('--marker', action='append', default=[],
                    help='縦線マーカー（JST）例: "07:07:02 P波"  複数指定可')
    ap.add_argument('--out', help='出力PNGパス（省略時は自動生成）')
    args = ap.parse_args()

    knet_dir = pathlib.Path(args.knet_dir)
    if not knet_dir.exists():
        sys.exit(f"[ERROR] ディレクトリが存在しません: {knet_dir}")

    if args.list:
        list_available(knet_dir)
        return

    # ===== 対象ファイル群の解決 =====
    file_map = None
    sta_code = None
    event_tag = None

    if args.file:
        p = pathlib.Path(args.file)
        if not p.exists():
            sys.exit(f"[ERROR] ファイルが存在しません: {p}")
        name = p.stem
        if len(name) < 10 or not name[-10:].isdigit():
            sys.exit(f"[ERROR] ファイル名が K-NET 規則に合いません: {p.name}")
        sta_code  = name[:-10]
        event_tag = name[-10:]
        file_map = {}
        for ch in KNET_CHANNELS:
            cand = p.parent / f"{sta_code}{event_tag}.{ch}"
            if cand.exists():
                file_map[ch] = cand
    else:
        if not args.station or not args.event:
            ap.error('--file または --station+--event を指定してください。--list で一覧表示可。')
        items = find_knet_files(knet_dir, station=args.station, event=args.event)
        if not items:
            sys.exit(f"[ERROR] station={args.station} event={args.event} のファイルが見つかりません。")
        if len(items) > 1:
            print(f"[WARN] 複数候補が見つかりました。最初のものを使用します:")
            for s, e, _ in items:
                print(f"  {s} {e}")
        sta_code, event_tag, file_map = items[0]

    if 'NS' not in file_map or 'EW' not in file_map:
        sys.exit(f"[ERROR] NS と EW の両成分が必要です。検出: {list(file_map.keys())}")

    print(f"K-NET 観測点: {sta_code}  イベントタグ: {event_tag}")
    print(f"  成分: {sorted(file_map.keys())}")

    # ===== 読み込み（calib適用済み・gal単位）=====
    print("波形を読み込み中...")
    traces = load_knet_traces(file_map)

    # メタ情報は NS から取得（3成分とも同一のはず）
    tr_ref = traces['NS']
    knet_meta = tr_ref.stats.knet
    sta_lat = float(knet_meta.stla)
    sta_lon = float(knet_meta.stlo)
    sta_el  = float(knet_meta.stel)
    fs      = float(tr_ref.stats.sampling_rate)

    # 震源情報（K-NET ヘッダに含まれる）
    quake_info = {
        'name':      f'K-NET震源({event_tag})',
        'latitude':  float(knet_meta.evla),
        'longitude': float(knet_meta.evlo),
        'depth':     float(knet_meta.evdp),
        'magnitude': float(knet_meta.mag),
    }
    accmax_header = float(knet_meta.accmax)  # ヘッダの最大加速度（gal）

    print(f"  観測点座標: 緯度={sta_lat:.4f}  経度={sta_lon:.4f}  標高={sta_el:.1f}m")
    print(f"  震源情報: 緯度={quake_info['latitude']:.4f}  経度={quake_info['longitude']:.4f}  "
          f"深さ={quake_info['depth']:.1f}km  M={quake_info['magnitude']:.1f}")
    print(f"  サンプリング: {fs:.1f}Hz  ヘッダ最大加速度: {accmax_header:.3f} gal")

    # 3成分の時刻と長さを揃える
    t0 = max(tr.stats.starttime for tr in traces.values())
    t1 = min(tr.stats.endtime   for tr in traces.values())
    N  = int((t1 - t0) * fs)

    segs = {}
    for ch, tr in traces.items():
        i0 = int((t0 - tr.stats.starttime) * fs)
        segs[ch] = tr.data[i0:i0 + N]
    min_len = min(len(s) for s in segs.values())
    segs = {ch: s[:min_len] for ch, s in segs.items()}
    N = min_len

    available_ch = sorted(segs.keys())
    print(f"  揃え後サンプル数: {N}  ({N/fs:.1f}秒)")

    # ===== STA/LTA（生加速度ベクトル合成）=====
    print("STA/LTA計算中...")
    vec_raw = np.sqrt(sum((segs[ch] - np.mean(segs[ch]))**2 for ch in available_ch))
    ratio_arr = compute_stalta(vec_raw, fs, args.sta, args.lta)

    # ===== JMAフィルタ → 計測震度 =====
    # segs[ch] は gal (cm/s²) 単位 → JMAフィルタは m/s² 入力前提なので 0.01 倍してから入れる
    print("計測震度計算中...")
    filtered = [
        apply_jma_filter_time((segs[ch] - np.mean(segs[ch])) * 0.01, fs)
        for ch in available_ch
    ]
    # apply_jma_filter_time は m/s² を返す → 100倍で gal に戻す
    a_comb_gal = np.sqrt(sum(f**2 for f in filtered)) * 100.0
    I_arr = compute_intensity_timeseries(a_comb_gal, fs, window_s=90.0)

    t0_dt     = t0.datetime.replace(tzinfo=UTC).astimezone(JST)
    times_jst = [t0_dt + timedelta(seconds=k / fs) for k in range(N)]

    t_start_jst = t0_dt
    t_end_jst   = t0_dt + timedelta(seconds=N / fs)
    duration_s  = N / fs

    # ===== マーカー解析 =====
    markers = []
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
    out_dir = pathlib.Path('data/knet')
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = pathlib.Path(args.out) if args.out else out_dir / f"analysis_knet_{sta_code}_{event_tag}.png"

    title = (
        f"K-NET {NETWORK}.{sta_code}  "
        f"{t_start_jst.strftime('%Y-%m-%d %H:%M:%S')} 〜 {t_end_jst.strftime('%H:%M:%S')} JST"
        f"（{duration_s:.0f}秒）  {quake_info['name']} M{quake_info['magnitude']}"
    )

    # ===== グラフ描画 =====
    print("グラフを生成中...")
    plot_knet_analysis(
        times_jst=times_jst,
        a_comb_gal=a_comb_gal,
        ratio_arr=ratio_arr,
        I_arr=I_arr,
        segs=segs,
        available_ch=available_ch,
        fs=fs,
        sta_s=args.sta,
        lta_s=args.lta,
        trig_thr=args.trig,
        title=title,
        out_path=out_path,
        markers=markers,
        stable_offset_s=args.lta,
        quake_info=quake_info,
        sta_lat=sta_lat,
        sta_lon=sta_lon,
        sta_name=sta_code,
        accmax_header=accmax_header,
    )

    import subprocess, platform
    opener = 'open' if platform.system() == 'Darwin' else 'xdg-open'
    try:
        subprocess.Popen([opener, str(out_path)])
    except Exception:
        pass


# ===== 描画 =====
def plot_knet_analysis(
    times_jst, a_comb_gal, ratio_arr, I_arr,
    segs, available_ch, fs,
    sta_s, lta_s, trig_thr,
    title, out_path,
    markers, stable_offset_s,
    quake_info, sta_lat, sta_lon, sta_name,
    accmax_header,
):
    """analyze_rs.py の plot_analysis に相当する K-NET 版描画。

    レイアウト:
        左列: 震源地図 / UD成分スペクトログラム / UD振幅スペクトル
        右列: 3成分加速度波形 / 合成加速度 / STA/LTA / 計測震度
    """
    _setup_font()

    N = len(a_comb_gal)
    fs_approx = N / max((times_jst[-1] - times_jst[0]).total_seconds(), 1.0)
    stable_idx = min(int(stable_offset_s * fs_approx), max(0, N - 1))

    a_search   = a_comb_gal[stable_idx:]
    r_search   = ratio_arr[stable_idx:]
    I_search   = I_arr[stable_idx:]
    I_peak     = float(I_search.max()) if len(I_search) else 0.0
    r_peak     = float(r_search.max()) if len(r_search) else 0.0
    a_peak     = float(a_search.max()) if len(a_search) else 0.0
    scale_peak = jma_scale_from_I(I_peak)
    idx_ap     = int(np.argmax(a_search)) + stable_idx
    idx_rp     = int(np.argmax(r_search)) + stable_idx

    has_ud = 'UD' in segs

    n_right = 4  # 3成分波形 + 合成加速度 + STA/LTA + 計測震度 のうち、3成分は1パネルにまとめる
    fig = plt.figure(figsize=(18, 4 * n_right))
    fig.patch.set_facecolor('#ffffff')

    gs = gridspec.GridSpec(
        n_right, 2,
        width_ratios=[1, 2],
        hspace=0.55, wspace=0.30,
        left=0.08, right=0.97, top=0.93, bottom=0.10,
    )

    gs_left = gridspec.GridSpecFromSubplotSpec(
        3, 1, subplot_spec=gs[:, 0],
        hspace=0.55,
        height_ratios=[n_right, n_right, n_right],
    )
    ax_map   = fig.add_subplot(gs_left[0])
    ax_sgram = fig.add_subplot(gs_left[1])
    ax_fft   = fig.add_subplot(gs_left[2])

    ax_right = [fig.add_subplot(gs[i, 1]) for i in range(n_right)]

    for ax in [ax_map, ax_sgram, ax_fft] + ax_right:
        ax.set_facecolor('#f6f8fa')
        ax.tick_params(colors='#57606a', labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#d0d7de')

    # ===== 震源地図 =====
    dist_km = 0.0
    if quake_info.get('latitude') is not None:
        dlat = quake_info['latitude'] - sta_lat
        dlon = quake_info['longitude'] - sta_lon
        horiz_km = np.sqrt((dlat * 111.0)**2 + (dlon * 111.0 * np.cos(np.radians(sta_lat)))**2)
        depth_km = max(0.0, quake_info.get('depth', 0))
        dist_km  = np.sqrt(horiz_km**2 + depth_km**2)
    plot_map(
        ax_map,
        eq_lat=quake_info.get('latitude'),
        eq_lon=quake_info.get('longitude'),
        eq_name=quake_info.get('name', ''),
        eq_mag=quake_info.get('magnitude', 0),
        eq_depth=quake_info.get('depth', 0),
        sta_lat=sta_lat, sta_lon=sta_lon,
        dist_km=dist_km,
        sta_name=sta_name,
    )

    # ===== UD成分 スペクトログラム =====
    ud_sig = segs.get('UD')
    if has_ud and len(ud_sig) >= 8:
        ud_dc = ud_sig - np.mean(ud_sig)
        nperseg = min(512, max(4, len(ud_dc) // 8))
        t_fr, freq, S = compute_spectrogram(ud_dc, fs, nperseg=nperseg)
        t0_jst = times_jst[0]
        t_abs = [t0_jst + timedelta(seconds=float(t)) for t in t_fr]
        f_min, f_max = 0.5, fs / 2
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
        ax_sgram.set_ylabel('周波数 [Hz]', color='#57606a', fontsize=8)
        ax_sgram.set_title('UD成分 スペクトログラム', color='#1f2328', fontsize=11, pad=6)
        ax_sgram.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
        ax_sgram.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax_sgram.set_yticks([0.5, 1, 2, 4, 8, 10, 20])
        ax_sgram.tick_params(colors='#57606a', labelsize=7)
        ax_sgram.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=JST))
        _sgram_dur = (times_jst[-1] - times_jst[0]).total_seconds()
        _sgram_interval = max(60, int(_sgram_dur / 5 / 60) * 60)
        ax_sgram.xaxis.set_major_locator(mdates.SecondLocator(interval=_sgram_interval))
        ax_sgram.set_xlabel('時刻 (JST)', color='#57606a', fontsize=8)
        ax_sgram.tick_params(axis='x', labelbottom=True, labelsize=8)
        for t_m, _lbl in markers:
            ax_sgram.axvline(t_m, color='#8250df', lw=1.2, ls='--', alpha=0.8, zorder=5)
    else:
        ax_sgram.text(0.5, 0.5, 'UD成分なし', transform=ax_sgram.transAxes,
                      color='#57606a', ha='center', va='center')
        ax_sgram.set_title('UD成分 スペクトログラム', color='#1f2328', fontsize=11, pad=6)

    # ===== UD成分 振幅スペクトル =====
    if has_ud and len(ud_sig) >= 2:
        ud_dc = ud_sig - np.mean(ud_sig)
        nfft = len(ud_dc)
        win = np.hanning(nfft)
        spec = np.abs(np.fft.rfft(ud_dc * win)) * 2.0 / win.sum()
        freq_fft = np.fft.rfftfreq(nfft, d=1.0 / fs)
        mask = freq_fft >= 0.1
        ax_fft.semilogy(freq_fft[mask], spec[mask], color='#1a7f37', lw=0.8, alpha=0.9)
        ax_fft.set_xlabel('周波数 [Hz]', color='#57606a', fontsize=8)
        ax_fft.set_ylabel('振幅 [gal]', color='#57606a', fontsize=8)
        ax_fft.set_title('UD成分 振幅スペクトル（FFT）', color='#1f2328', fontsize=11, pad=6)
        ax_fft.set_xlim(0.1, fs / 2)
        ax_fft.axvline(1.0,  color='#57606a', lw=0.8, ls=':', alpha=0.7)
        ax_fft.axvline(10.0, color='#57606a', lw=0.8, ls=':', alpha=0.7)
        ax_fft.text(1.0,  spec[mask].max() * 0.5, ' 1Hz',  color='#57606a', fontsize=8)
        ax_fft.text(10.0, spec[mask].max() * 0.5, ' 10Hz', color='#57606a', fontsize=8)
        ax_fft.grid(color='#d0d7de', lw=0.4, ls=':')
        ax_fft.tick_params(colors='#57606a', labelsize=7)
    else:
        ax_fft.text(0.5, 0.5, 'UD成分なし', transform=ax_fft.transAxes,
                    color='#57606a', ha='center', va='center')
        ax_fft.set_title('UD成分 振幅スペクトル（FFT）', color='#1f2328', fontsize=11, pad=6)

    # ===== 右列パネル =====
    def draw_markers(ax):
        for t_m, lbl in markers:
            ax.axvline(t_m, color='#8250df', lw=1.2, ls='--', alpha=0.8, zorder=5)
            ylim = ax.get_ylim()
            ax.text(t_m, ylim[1] - (ylim[1] - ylim[0]) * 0.05, f' {lbl}',
                    color='#8250df', fontsize=8, va='top', zorder=6)

    panel = 0
    color_map = {'NS': '#cf222e', 'EW': '#0969da', 'UD': '#1a7f37'}

    # 3成分加速度波形（1パネルに重ね描き）
    ax = ax_right[panel]; panel += 1
    for ch in available_ch:
        sig = segs[ch] - np.mean(segs[ch])
        ax.plot(times_jst, sig, color=color_map.get(ch, '#57606a'),
                lw=0.5, alpha=0.85, label=f'{ch} (max {np.abs(sig).max():.2f} gal)')
    ax.set_ylabel('加速度 [gal]', color='#57606a')
    ax.set_title(f'3成分加速度（{"+".join(available_ch)}、DC除去）',
                 color='#1f2328', fontsize=11, pad=6)
    ax.axhline(0, color='#d0d7de', lw=0.5)
    ax.legend(loc='upper right', fontsize=8,
              facecolor='#f6f8fa', edgecolor='#d0d7de', labelcolor='#1f2328')
    draw_markers(ax)

    # 合成加速度（JMAフィルタ後）
    ax = ax_right[panel]; panel += 1
    ax.plot(times_jst, a_comb_gal, color='#0969da', lw=0.6, alpha=0.9)
    ax.set_ylabel('加速度 [gal]', color='#57606a')
    ax.set_title(f'合成加速度（JMAフィルタ後、{"+".join(available_ch)}）',
                 color='#1f2328', fontsize=11, pad=6)
    ax.annotate(f'最大 {a_peak:.3f} gal',
                xy=(times_jst[idx_ap], a_comb_gal[idx_ap]),
                xytext=(10, 5), textcoords='offset points',
                color='#0969da', fontsize=9,
                arrowprops=dict(arrowstyle='->', color='#0969da', lw=0.8))
    draw_markers(ax)

    # STA/LTA
    ax = ax_right[panel]; panel += 1
    ax.plot(times_jst, ratio_arr, color='#0969da', lw=1.2, label='STA/LTA')
    ax.axhline(trig_thr, color='#bc4c00', lw=1.5, ls='--', label=f'閾値 {trig_thr}')
    ax.fill_between(times_jst, 0, ratio_arr, where=ratio_arr >= trig_thr,
                    color='#bc4c00', alpha=0.3, label='トリガ')
    ax.set_ylabel('STA/LTA', color='#57606a')
    ax.set_title(f'STA/LTA比  (STA={sta_s}s / LTA={lta_s}s)',
                 color='#1f2328', fontsize=11, pad=6)
    ax.legend(loc='upper right', fontsize=9,
              facecolor='#f6f8fa', edgecolor='#d0d7de', labelcolor='#1f2328')
    ax.set_ylim(bottom=0)
    ax.annotate(f'最大 {r_peak:.2f}',
                xy=(times_jst[idx_rp], ratio_arr[idx_rp]),
                xytext=(10, 5), textcoords='offset points',
                color='#bc4c00', fontsize=9,
                arrowprops=dict(arrowstyle='->', color='#bc4c00', lw=0.8))
    draw_markers(ax)

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

    # X軸
    duration_s = (times_jst[-1] - times_jst[0]).total_seconds()
    interval = max(10, int(duration_s / 10 / 10) * 10)
    for ax in ax_right:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S', tz=JST))
        ax.xaxis.set_major_locator(mdates.SecondLocator(interval=interval))
        ax.set_xlabel('時刻 (JST)', color='#57606a', fontsize=8)
        ax.tick_params(axis='x', labelbottom=True, labelsize=8)
        ax.xaxis.set_tick_params(which='both', labelbottom=True)

    # ヘッダ加速度との突き合わせ表示
    accmax_str = f'  /  K-NETヘッダ最大 {accmax_header:.2f} gal'

    # Si-Midorikawa (1999) 距離減衰式の逆算で推定M
    est_M_str = ''
    if dist_km > 0 and a_peak > 0:
        r = dist_km
        est_M = (np.log10(a_peak) - 0.167 + 1.73 * np.log10(r) + 0.00030 * r) / 0.61
        official_mag = quake_info.get('magnitude', 0)
        caveat = '参考値' if dist_km > 200 else '±0.5程度'
        est_M_str = f'  /  推定M {est_M:.1f}（公式M{official_mag}、{caveat}）'

    dist_str = f'  /  震源距離 {dist_km:.0f}km' if dist_km > 0 else ''

    fig.suptitle(
        f'{title}\n'
        f'最大加速度: {a_peak:.3f} gal{accmax_str}  /  計測震度: I={I_peak:.2f}（震度{scale_peak}）'
        f'  /  STA/LTA最大: {r_peak:.2f}{dist_str}{est_M_str}',
        color='#1f2328', fontsize=12,
    )

    fig.text(
        0.99, 0.01,
        '防災科学技術研究所（NIED）が公開する K-NET / KiK-net 強震観測網のデータを利用しています。',
        ha='right', va='bottom', fontsize=7, color='#57606a',
        transform=fig.transFigure,
    )

    plt.savefig(str(out_path), dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"グラフ保存: {out_path}")


if __name__ == '__main__':
    main()
