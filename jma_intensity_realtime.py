#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake UDP（DATACAST）を受信してリアルタイムに地震検出→計測震度(JMA)を出力
- UDP: 既定ポート8888, フォーマット: {'CH', epoch, count, ...} （1パケット=1ch）
- 3成分（HNN/HNE/HNZ）をリングバッファで保持、counts→ACC[m/s^2]へ StationXML で校正
- 検出: STA/LTA、トリガ時に直近 rt-window 秒の計測震度を算出して表示

参考: Raspberry Shake マニュアル「UDP Port Output / DATACAST」（出力形式とポート）,
      rsudp (公式UDPクライアントの実装例)
"""

import argparse
import socket
import threading
import time
from collections import deque, defaultdict

import numpy as np
from obspy import Stream, Trace, UTCDateTime
from obspy.clients.fdsn import Client as FDSNClient
from obspy.core.inventory import read_inventory


# ===== JMA フィルタ =====
def jma_frequency_response(f):
    f = np.asarray(f, dtype=float)
    FL = np.sqrt(1.0 - np.exp(- (np.power(f / 0.5, 3.0))))
    y = f / 10.0
    poly = (1.0 + 0.694 * np.power(y, 2) + 0.241 * np.power(y, 4) + 0.0557 * np.power(y, 6) +
            0.009664 * np.power(y, 8) + 0.00134 * np.power(y, 10) + 0.000155 * np.power(y, 12))
    FH = np.power(poly, -0.5)
    FF = np.zeros_like(f)
    nz = f > 0.0
    FF[nz] = np.power(1.0 / f[nz], 0.5)  # f=0 -> 0
    H = FL * FH * FF
    H[~np.isfinite(H)] = 0.0
    return H


def apply_jma_filter_time(acc, fs):
    n = len(acc)
    spec = np.fft.rfft(acc)
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    H = jma_frequency_response(f)
    return np.fft.irfft(spec * H, n=n)


def a_threshold_for_03s(vec_abs, fs):
    k = int(round(0.3 * fs))
    k = max(1, min(k, len(vec_abs)))
    idx = len(vec_abs) - k
    part = np.partition(vec_abs, idx)
    return float(part[idx])


def jma_scale_from_I(I):
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


# ===== StationXML 読み込み =====
def get_inventory(fdsn_url=None, stationxml_path=None, network=None, station=None, t0=None, t1=None):
    if stationxml_path:
        return read_inventory(stationxml_path)
    elif fdsn_url and network and station:
        cli = FDSNClient(fdsn_url)
        # 時間が不明な場合に備えて余裕を持って取得
        start = t0 - 3600 if t0 else None
        end = t1 + 3600 if t1 else None
        return cli.get_stations(network=network, station=station, level="response",
                                starttime=start, endtime=end)
    else:
        raise RuntimeError("StationXML の取得方法が不足（--stationxml または --fdsn を指定）。")


# ===== UDP パケットのパース =====
def parse_udp_packet(payload: bytes):
    """
    例: b"{'HNZ', 1700000000.123, 123,456,789,...}"
    戻り値: (channel:str, t0_epoch:float, np.ndarray[int32])
    """
    s = payload.decode('ascii', errors='ignore').strip()
    # { 'CH', ts, v1, v2, ... }
    if not (s.startswith("{") and s.endswith("}")):
        return None
    s = s[1:-1].strip()
    # 先頭の 'CH' を取り出す
    if not s.startswith("'"):
        return None
    p = s.find("',")
    if p < 0:
        return None
    ch = s[1:p]
    rest = s[p + 2:].strip()
    # 残りをカンマ分割（数が多いので高速パース）
    parts = rest.split(',')
    if len(parts) < 2:
        return None
    try:
        t0 = float(parts[0])
        vals = np.fromiter((int(x) for x in parts[1:]), dtype=np.int32, count=len(parts) - 1)
        return ch, t0, vals
    except Exception:
        return None


# ===== リングバッファ（各成分）=====
class Ring:
    def __init__(self, maxlen_samples):
        self.buf = deque(maxlen=maxlen_samples)
        self.nsamp = 0

    def extend(self, arr):
        for v in arr:
            self.buf.append(float(v))
        self.nsamp = len(self.buf)

    def to_array(self):
        if self.nsamp == 0:
            return np.zeros(0)
        return np.array(self.buf, dtype=np.float64)


# ===== メイン（UDP受信→検出→震度）=====
def main():
    ap = argparse.ArgumentParser(description="Raspberry Shake UDP リアルタイム計測震度")
    ap.add_argument("--bind", type=str, default="0.0.0.0:8888", help="受信アドレス:ポート（例: 0.0.0.0:8888）")
    ap.add_argument("--channels", type=str, default="ENZ,ENN,ENE", help="3成分（カンマ区切り）RS4D加速度計: ENZ,ENN,ENE")
    ap.add_argument("--network", type=str, default="AM")
    ap.add_argument("--station", type=str, required=True)
    ap.add_argument("--stationxml", type=str, help="ローカル StationXML")
    ap.add_argument("--fdsn", type=str, help="FDSN Stations ベースURL（例: http://rs.local:16023）")
    ap.add_argument("--sensitivity", type=float, default=387867.0,
                    help="counts/(m/s²) 感度値。StationXML不要の場合に使用。R38DC実測値: 387867 (公式V6: 384500)")

    # リアルタイム設定
    ap.add_argument("--rt-window", type=float, default=90.0, help="震度計算の窓長[秒]")
    ap.add_argument("--sta", type=float, default=1.0, help="STA 窓長[秒]")
    ap.add_argument("--lta", type=float, default=20.0, help="LTA 窓長[秒]")
    ap.add_argument("--trig", type=float, default=3.5, help="STA/LTA しきい値")
    ap.add_argument("--det-hold", type=float, default=20.0, help="検出後の再検出抑制[秒]")

    args = ap.parse_args()

    # UDP ソケット
    host, port = args.bind.split(":")
    port = int(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.setblocking(True)
    print(f"[INFO] UDP受信待機: {host}:{port}  （RS側DATACAST設定が必要）")

    # 3成分
    comps = [c.strip().upper() for c in args.channels.split(",")]
    if len(comps) != 3:
        raise SystemExit("--channels は3成分を指定してください（例: HNN,HNE,HNZ）")

    # FS 推定（パケットの到着間隔・長さから推定、初期は100Hz仮定）
    inferred_fs = None
    pkt_len = None
    last_t0 = {c: None for c in comps}

    # リングバッファ長（最大でも rt-window の2倍程度を持たせる）
    max_window = int(max(args.rt_window, args.lta) * 2.0)
    rings_counts = {c: Ring(maxlen_samples=10_000 * max_window) for c in comps}

    # 検出状態
    last_trigger_time = 0.0
    last_periodic_output = 0.0

    # StationXML は初回に揃えておく（stream を仮作成して取得に使う）
    inv_cache = None

    def compute_fs_if_possible(ch, t0_epoch, arr_len):
        nonlocal inferred_fs, pkt_len
        # 単純に「同一chの連続パケットの t0 差分 / サンプル数」で推定
        prev = last_t0.get(ch)
        last_t0[ch] = t0_epoch
        if prev is None:
            return
        dt = t0_epoch - prev
        if dt > 0 and arr_len > 0:
            fs_est = arr_len / dt
            # 100 Hz 近辺に落ち着くはず（RS4DのHNは100Hzが多い）
            if inferred_fs is None:
                inferred_fs = fs_est
            else:
                # 簡易平滑
                inferred_fs = 0.8 * inferred_fs + 0.2 * fs_est
        pkt_len = arr_len

    # STA/LTA 計算（逐次・簡易）
    def sta_lta_ratio(vec, fs, sta_s, lta_s):
        nsta = max(1, int(round(sta_s * fs)))
        nlta = max(nsta + 1, int(round(lta_s * fs)))
        if len(vec) < nlta:
            return 0.0
        s_sta = np.mean(vec[-nsta:] ** 2) + 1e-18
        s_lta = np.mean(vec[-nlta:-nsta] ** 2) + 1e-18
        return s_sta / s_lta

    # 受信スレッド
    stop = False

    def recv_loop():
        nonlocal inferred_fs
        while not stop:
            data, _ = sock.recvfrom(65536)
            parsed = parse_udp_packet(data)
            if not parsed:
                continue
            ch, t0, vals = parsed
            ch = ch.upper()
            if ch not in rings_counts:
                # 他チャンネルは無視
                continue
            rings_counts[ch].extend(vals)
            compute_fs_if_possible(ch, t0, len(vals))

    th = threading.Thread(target=recv_loop, daemon=True)
    th.start()

    print("[INFO] 受信開始。最初の数秒は fs（サンプリング周波数）を推定します…")

    try:
        while True:
            time.sleep(0.5)
            if inferred_fs is None:
                continue

            fs = float(inferred_fs)

            # 3成分が揃っているか確認
            if any(rings_counts[c].nsamp == 0 for c in comps):
                continue

            # counts → m/s^2 に変換するため、一時的に ObsPy Stream を作る
            # （連続時間情報は近似。UTCNow を終端として作る）
            now = UTCDateTime()
            # 窓長ぶん取り出し
            need = int(args.rt_window * fs)
            traces = []
            for c in comps:
                arr = rings_counts[c].to_array()
                if len(arr) < need:
                    # 充分なデータが溜まってから
                    break
                seg = arr[-need:]
                tr = Trace(data=seg.astype(np.float64))
                tr.stats.sampling_rate = fs
                # チャンネル名（countsのHN?を仮設定）
                tr.stats.channel = c
                tr.stats.network = args.network
                tr.stats.station = args.station
                tr.stats.starttime = now - (len(seg) / fs)
                traces.append(tr)
            if len(traces) != 3:
                continue

            st_counts = Stream(traces)

            # counts -> ACC
            st_acc = st_counts.copy()
            if args.stationxml or args.fdsn:
                # StationXML（キャッシュ）取得
                if inv_cache is None:
                    try:
                        inv_cache = get_inventory(
                            fdsn_url=args.fdsn,
                            stationxml_path=args.stationxml,
                            network=args.network, station=args.station,
                            t0=st_counts[0].stats.starttime, t1=st_counts[0].stats.endtime
                        )
                        print("[INFO] StationXML をロードしました。counts→ACC 変換を開始。")
                    except Exception as e:
                        print(f"[WARN] StationXML 取得失敗: {e}")
                        continue
                try:
                    st_acc.remove_response(inventory=inv_cache, output="ACC", water_level=60.0, taper=True)
                except Exception as e:
                    print(f"[WARN] remove_response 失敗: {e}")
                    continue
            else:
                # 感度値で直接 counts → m/s² に変換（概算）
                for tr in st_acc:
                    tr.data = tr.data / args.sensitivity

            # 3成分取得
            # Z/N/E がそろわない場合はスキップ
            def pick(st, endch):
                for tr in st:
                    if (tr.stats.channel or "").upper().endswith(endch):
                        return tr
                return None

            trZ = pick(st_acc, "Z")
            trN = pick(st_acc, "N")
            trE = pick(st_acc, "E")
            if not (trZ and trN and trE):
                continue

            # 検出用ベクトル（未フィルタ）で STA/LTA
            vec_raw = np.sqrt(trZ.data ** 2 + trN.data ** 2 + trE.data ** 2)
            ratio = sta_lta_ratio(vec_raw, fs, args.sta, args.lta)

            t_now = time.time()
            triggered = False
            if ratio >= args.trig and (t_now - last_trigger_time) > args.det_hold:
                triggered = True
                last_trigger_time = t_now

            # トリガ時 or 5秒ごとに現在の震度を出す
            periodic = (t_now - last_periodic_output) >= 5.0
            if triggered or periodic:
                # JMA フィルタ → 0.3s 閾値 → 震度
                az = apply_jma_filter_time(trZ.data.astype(np.float64), fs)
                an = apply_jma_filter_time(trN.data.astype(np.float64), fs)
                ae = apply_jma_filter_time(trE.data.astype(np.float64), fs)
                vec = np.sqrt(az ** 2 + an ** 2 + ae ** 2)
                a_mps2 = a_threshold_for_03s(np.abs(vec), fs)
                a_gal = a_mps2 * 100.0
                I_raw = 0.0 if a_gal <= 0 else (2.0 * np.log10(a_gal) + 0.94)
                I_final = np.floor(np.round(I_raw, 3) * 100.0) / 100.0
                scale = jma_scale_from_I(I_final)

                if periodic:
                    last_periodic_output = t_now
                lab = "TRIGGER" if triggered else "STATUS"
                print(f"[{lab}] fs={fs:.1f}Hz ratio={ratio:.2f}  a={a_gal:.2f}gal  I={I_final:.2f}  震度:{scale}")

    except KeyboardInterrupt:
        pass
    finally:
        stop = True
        try:
            sock.close()
        except:
            pass
        print("\n[INFO] 終了しました。")


if __name__ == "__main__":
    main()