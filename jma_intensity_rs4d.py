#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake 4D（加速度計）向け 計測震度スクリプト
入力: SeedLink / MiniSEED / FDSN Dataselect / CSV
校正: StationXML（ローカル or FDSN）で counts→加速度[m/s^2]
計算: 気象庁「計測震度の算出方法」
"""

import argparse
import numpy as np
from obspy import read, Stream, Trace, UTCDateTime
from obspy.clients.seedlink import Client as SLClient
from obspy.clients.fdsn import Client as FDSNClient
from obspy.core.inventory import read_inventory
from datetime import datetime

# --------------- ログ出力 -----------------

def info(msg: str):
    try:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[INFO] {ts} {msg}")
    except Exception:
        print(f"[INFO] {msg}")

# ----------------- 気象庁フィルタ -----------------
def jma_frequency_response(f: np.ndarray) -> np.ndarray:
    f = np.asarray(f, dtype=float)
    FL = np.sqrt(1.0 - np.exp(- (np.power(f / 0.5, 3.0))))
    y = f / 10.0
    poly = (1.0 + 0.694*np.power(y, 2) + 0.241*np.power(y, 4) + 0.0557*np.power(y, 6) +
            0.009664*np.power(y, 8) + 0.00134*np.power(y, 10) + 0.000155*np.power(y, 12))
    FH = np.power(poly, -0.5)
    FF = np.zeros_like(f)
    nz = f > 0.0
    FF[nz] = np.power(1.0 / f[nz], 0.5)   # f=0 は 0
    H = FL * FH * FF
    H[~np.isfinite(H)] = 0.0
    return H

def apply_jma_filter_time(acc: np.ndarray, fs: float) -> np.ndarray:
    n = len(acc)
    spec = np.fft.rfft(acc)
    f = np.fft.rfftfreq(n, d=1.0/fs)
    H = jma_frequency_response(f)
    spec_filt = spec * H
    return np.fft.irfft(spec_filt, n=n)

# ----------------- 0.3秒超過レベル a -----------------
def a_threshold_for_03s(vec_abs: np.ndarray, fs: float) -> float:
    k = int(round(0.3 * fs))
    k = max(1, min(k, len(vec_abs)))
    idx = len(vec_abs) - k
    part = np.partition(vec_abs, idx)
    return float(part[idx])

# ----------------- 震度階級換算 -----------------
def jma_scale_from_I(I: float) -> str:
    if I < 0.5: return "0"
    if I < 1.5: return "1"
    if I < 2.5: return "2"
    if I < 3.5: return "3"
    if I < 4.5: return "4"
    if I < 5.0: return "5弱"
    if I < 5.5: return "5強"
    if I < 6.0: return "6弱"
    if I < 6.5: return "6強"
    return "7"

# ----------------- データ取得 -----------------
def read_from_seedlink(server: str, network: str, station: str,
                       channels: str, seconds: float) -> Stream:
    end = UTCDateTime()
    start = end - float(seconds)
    st = Stream()
    host, port = server.split(":")
    cli = SLClient(host, int(port))
    for ch in channels.split(","):
        ch = ch.strip()
        tr = cli.get_waveforms(network, station, "", ch, start, end)
        st += tr
    if len(st) == 0:
        raise RuntimeError("SeedLinkから波形を取得できませんでした。")
    return st

def read_from_mseed(path_glob: str) -> Stream:
    st = read(path_glob)
    if len(st) == 0:
        raise RuntimeError("mseed 読み込み結果が空です。")
    return st

def read_from_fdsn_waveforms(base_url: str, network: str, station: str,
                             channels: str, start_utc: str, end_utc: str) -> Stream:
    cli = FDSNClient(base_url)
    st = Stream()
    t0 = UTCDateTime(start_utc)
    t1 = UTCDateTime(end_utc)
    for ch in channels.split(","):
        ch = ch.strip()
        tr = cli.get_waveforms(network=network, station=station, location="",
                               channel=ch, starttime=t0, endtime=t1)
        st += tr
    if len(st) == 0:
        raise RuntimeError("FDSN(dataselect) から波形を取得できませんでした。")
    return st

# ---- CSV 読み込み（m/s^2 / gal / counts） ----
def parse_time_cell(s):
    s = str(s).strip()
    # UNIX 秒（float）?
    try:
        return UTCDateTime(float(s))
    except:
        pass
    # ISO 8601?
    try:
        return UTCDateTime(datetime.fromisoformat(s.replace("Z","+00:00")))
    except:
        pass
    raise ValueError(f"時刻の解釈に失敗: {s}")

def read_from_csv(path: str, csv_unit: str, fs: float = None,
                  has_time: bool = False, skiprows: int = 0) -> (Stream, float):
    """
    CSV 構成:
      - has_time=False: 3列 [HNN,HNE,HNZ]
      - has_time=True : 4列 [time,HNN,HNE,HNZ] （timeはISO8601 or UNIX秒）
    csv_unit: 'mps2' / 'gal' / 'counts'
    fs: has_time=False のとき必須
    """
    arr = np.loadtxt(path, delimiter=",", dtype=float, skiprows=skiprows)
    if has_time:
        if arr.shape[1] < 4:
            raise ValueError("CSV（時刻付き）は4列（time,HNN,HNE,HNZ）が必要です。")
        tcol = arr[:, 0]
        data = arr[:, 1:4]
        # サンプリング間隔を自動推定
        times = np.array([parse_time_cell(x) for x in tcol], dtype=float)
        dt = np.median(np.diff(times))
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("CSV からサンプリング間隔を推定できません。")
        fs_est = 1.0 / dt
        start = UTCDateTime(times[0])
        fs_use = fs_est
    else:
        if arr.shape[1] < 3:
            raise ValueError("CSV（時刻なし）は3列（HNN,HNE,HNZ）が必要です。")
        data = arr[:, 0:3]
        if fs is None:
            raise ValueError("時刻列がない CSV では --fs を指定してください。")
        fs_use = float(fs)
        start = UTCDateTime()

    # 単位変換
    if csv_unit.lower() == "gal":
        data = data / 100.0  # gal → m/s^2
    elif csv_unit.lower() == "mps2":
        pass
    elif csv_unit.lower() == "counts":
        # counts はここでは変換しない（後段の remove_response で変換）
        pass
    else:
        raise ValueError("--csv-unit は mps2 / gal / counts のいずれかです。")

    # HNN,HNE,HNZ の順で Trace を作成（チャネル名は流用）
    trN = Trace(data=data[:, 0].astype(np.float64))
    trE = Trace(data=data[:, 1].astype(np.float64))
    trZ = Trace(data=data[:, 2].astype(np.float64))
    for tr, ch in zip([trN, trE, trZ], ["HNN", "HNE", "HNZ"]):
        tr.stats.starttime = start
        tr.stats.sampling_rate = fs_use
        tr.stats.channel = ch
        tr.stats.network = ""
        tr.stats.station = ""
        tr.stats.location = ""
    st = Stream([trN, trE, trZ])
    return st, csv_unit.lower()

# ----------------- counts→加速度[m/s²] へ校正 -----------------
def remove_to_acc(stream_counts: Stream, inventory, output="ACC") -> Stream:
    st = stream_counts.copy()
    st.remove_response(inventory=inventory, output=output, water_level=60.0, taper=True)
    return st

def get_inventory(fdsn_url: str = None, stationxml_path: str = None,
                  network: str = None, station: str = None,
                  stream: Stream = None):
    if stationxml_path:
        return read_inventory(stationxml_path)
    if fdsn_url and network and station and stream:
        cli = FDSNClient(fdsn_url)
        t0 = max(tr.stats.starttime for tr in stream) - 3600
        t1 = min(tr.stats.endtime for tr in stream) + 3600
        return cli.get_stations(network=network, station=station, level="response",
                                starttime=t0, endtime=t1)
    raise RuntimeError("StationXML 取得方法が不足しています（--stationxml または --fdsn を指定）。")

# ----------------- 3成分整列 -----------------
def align_three_components(stream_acc: Stream):
    st = stream_acc.copy().detrend("demean").taper(0.02)
    fs = max(tr.stats.sampling_rate for tr in st)
    st.resample(fs)
    t0 = max(tr.stats.starttime for tr in st)
    t1 = min(tr.stats.endtime for tr in st)
    if t1 <= t0:
        raise RuntimeError("3成分の重複時間がありません。")
    st.trim(t0, t1, pad=True, fill_value=0.0)

    def pick(comp_set):
        for tr in st:
            ch = (tr.stats.channel or "")[-1:].upper()
            if ch in comp_set:
                return tr
        return None

    trZ = pick({"Z"})
    trN = pick({"N", "Y", "1"})
    trE = pick({"E", "X", "2"})
    if trZ is None or trN is None or trE is None:
        st_sorted = sorted(st, key=lambda tr: tr.id)
        if len(st_sorted) < 3:
            raise RuntimeError("3成分が取得できませんでした（チャンネル指定/CSV列を確認）。")
        trZ, trN, trE = st_sorted[:3]
    return trZ, trN, trE

# ----------------- メイン計算 -----------------
def compute_jma_intensity_from_acc_stream(stream_acc: Stream):
    info("3成分を整列してフィルタ処理を行います。")
    trZ, trN, trE = align_three_components(stream_acc)
    fs = trZ.stats.sampling_rate
    az = apply_jma_filter_time(trZ.data.astype(np.float64), fs)
    an = apply_jma_filter_time(trN.data.astype(np.float64), fs)
    ae = apply_jma_filter_time(trE.data.astype(np.float64), fs)
    vec = np.sqrt(az**2 + an**2 + ae**2)
    a_mps2 = a_threshold_for_03s(np.abs(vec), fs)
    a_gal = a_mps2 * 100.0
    I_raw = 0.0 if a_gal <= 0 else (2.0 * np.log10(a_gal) + 0.94)
    I_round3 = np.round(I_raw, 3)
    I_final = np.floor(I_round3 * 100.0) / 100.0
    return {
        "I_value": float(I_final),
        "I_raw": float(I_raw),
        "a_gal": float(a_gal),
        "fs": float(fs),
        "window_seconds": len(vec) / fs,
        "scale": jma_scale_from_I(I_final),
    }

# ----------------- CLI -----------------
def main():
    ap = argparse.ArgumentParser(description="Raspberry Shake 4D 計測震度（SeedLink/miniSEED/FDSN/CSV）")
    # 入力
    ap.add_argument("--seedlink", type=str, help="SeedLink host:port (例: rs.local:18000)")
    ap.add_argument("--mseed", type=str, help="mseed のパス（glob 可）")
    ap.add_argument("--fdsn-waveforms", type=str, help="FDSN Dataselect ベースURL（https://…）")
    ap.add_argument("--start", type=str, help="UTC 開始時刻 ISO8601 (例: 2025-10-07T03:05:00)")
    ap.add_argument("--end", type=str, help="UTC 終了時刻 ISO8601 (例: 2025-10-07T03:07:00)")

    # CSV
    ap.add_argument("--csv", type=str, help="CSV ファイルパス")
    ap.add_argument("--csv-unit", type=str, default="mps2", help="mps2 / gal / counts")
    ap.add_argument("--csv-has-time", action="store_true", help="CSVの先頭列が時刻")
    ap.add_argument("--csv-skiprows", type=int, default=0, help="CSV先頭のスキップ行数")
    ap.add_argument("--fs", type=float, help="CSVが時刻列を持たない場合のサンプリング周波数[Hz]")

    # 校正
    ap.add_argument("--stationxml", type=str, help="ローカル StationXML ファイルパス")
    ap.add_argument("--fdsn", type=str, help="FDSN Stations ベースURL（StationXML取得用）")
    ap.add_argument("--network", type=str, default="AM")
    ap.add_argument("--station", type=str, required=True)
    ap.add_argument("--channels", type=str, default="HNN,HNE,HNZ")
    ap.add_argument("--seconds", type=float, default=90.0)

    args = ap.parse_args()

    # 1) データ取得（counts or 加速度）
    info("処理を開始します。入力データを読み込みます。")
    st = None
    csv_unit = None
    if args.mseed:
        info(f"MiniSEED を読み込みます: {args.mseed}")
        st = read_from_mseed(args.mseed)
        csv_unit = "counts"  # MiniSEED は通常 counts
    elif args.seedlink:
        info(f"SeedLink から取得します: server={args.seedlink} net={args.network} sta={args.station} ch={args.channels} seconds={args.seconds}")
        st = read_from_seedlink(args.seedlink, args.network, args.station,
                                args.channels, args.seconds)
        csv_unit = "counts"
    elif args.fdsn_waveforms and args.start and args.end:
        info(f"FDSN Dataselect から取得します: base={args.fdsn_waveforms} net={args.network} sta={args.station} ch={args.channels} start={args.start} end={args.end}")
        st = read_from_fdsn_waveforms(args.fdsn_waveforms, args.network, args.station,
                                      args.channels, args.start, args.end)
        csv_unit = "counts"
    elif args.csv:
        info(f"CSV を読み込みます: path={args.csv} unit={args.csv_unit} has_time={args.csv_has_time} skiprows={args.csv_skiprows} fs={args.fs}")
        st, csv_unit = read_from_csv(args.csv, args.csv_unit, fs=args.fs,
                                     has_time=args.csv_has_time, skiprows=args.csv_skiprows)
    else:
        raise SystemExit("入力を指定してください：--mseed / --seedlink / --fdsn-waveforms+--start+--end / --csv")

    # 取得概要
    try:
        t0 = min(tr.stats.starttime for tr in st)
        t1 = max(tr.stats.endtime for tr in st)
        fs_list = [float(tr.stats.sampling_rate) for tr in st]
        fs_med = float(np.median(fs_list)) if len(fs_list) > 0 else float("nan")
        info(f"入力取得完了: traces={len(st)} 期間={t0}〜{t1} （{float(t1 - t0):.2f} s） fs≈{fs_med:.2f} Hz")
    except Exception:
        info(f"入力取得完了: traces={len(st)}")

    # 2) 単位整備：counts → m/s^2（StationXML必須）
    if csv_unit == "counts":
        info("counts データを ACC[m/s^2] に変換します（応答除去）。")
        if args.stationxml:
            info(f"StationXML をローカルファイルから読み込み: {args.stationxml}")
        elif args.fdsn:
            info(f"FDSN Stations から StationXML を取得: {args.fdsn} net={args.network} sta={args.station}")
        inv = get_inventory(fdsn_url=args.fdsn, stationxml_path=args.stationxml,
                            network=args.network, station=args.station, stream=st)
        st_acc = remove_to_acc(st, inv, output="ACC")
        info("応答除去が完了しました。")
    elif csv_unit == "gal":
        # CSVで gal→m/s^2 に既に変換済み（read_from_csv 内）
        info("入力は gal 単位です。m/s^2 に換算済みです。")
        st_acc = st
    elif csv_unit == "mps2":
        info("入力は m/s^2 単位です。追加の単位変換は不要です。")
        st_acc = st
    else:
        raise SystemExit("内部エラー: 未知の単位")

    # 3) 計測震度
    info("計測震度を計算します（JMA フィルタ適用と 0.3 秒超過レベル）。")
    res = compute_jma_intensity_from_acc_stream(st_acc)
    info("計測震度の計算が完了しました。結果を表示します。")

    print("---- 計測震度（気象庁方式, RS4D 加速度）----")
    print(f"観測区間: {res['window_seconds']:.2f} s, fs={res['fs']:.2f} Hz")
    print(f"a（0.3秒超過）: {res['a_gal']:.2f} gal")
    print(f"計測震度値 I: {res['I_value']:.2f}（raw={res['I_raw']:.3f}）")
    print(f"震度階級: {res['scale']}")

if __name__ == "__main__":
    main()