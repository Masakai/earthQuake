#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
実波形リプレイシミュレーター: MiniSEEDファイルをRS DATACAST UDP形式で再送出する

使い方:
    .venv/bin/python3 src/replay_udp.py --files data/AM.R38DC.00.EN?.D.2026.144.ms

    # 早送り（2倍速）
    .venv/bin/python3 src/replay_udp.py --files data/AM.R38DC.00.EN?.D.2026.144.ms --speed 2.0

    # 別ポートに送出
    .venv/bin/python3 src/replay_udp.py --files data/AM.R38DC.00.EN?.D.2026.144.ms --dest 127.0.0.1:8888

Copyright (c) 2026 Masanori Sakai
"""

import argparse
import glob
import socket
import time

import numpy as np
from obspy import read, Stream, UTCDateTime


CHANNEL_MAP = {
    "ENZ": "ENZ",
    "ENN": "ENN",
    "ENE": "ENE",
}


def make_packet(channel: str, t0: float, samples: np.ndarray) -> bytes:
    """RS DATACAST形式のUDPパケットを生成"""
    vals = ",".join(str(int(round(v))) for v in samples)
    s = f"{{'{channel}', {t0:.6f}, {vals}}}"
    return s.encode("ascii")


def main():
    ap = argparse.ArgumentParser(description="MiniSEED実波形リプレイシミュレーター")
    ap.add_argument("--files", nargs="+", required=True,
                    help="MiniSEEDファイル（globパターン可、ENZ/ENN/ENEを含む3成分）")
    ap.add_argument("--dest", type=str, default="127.0.0.1:8888",
                    help="送出先アドレス:ポート (デフォルト: 127.0.0.1:8888)")
    ap.add_argument("--pkt-samples", type=int, default=25,
                    help="1パケットのサンプル数 (デフォルト: 25)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="再生速度倍率 (1.0=等速、2.0=2倍速、0=最速)")
    ap.add_argument("--channels", type=str, default="ENZ,ENN,ENE",
                    help="送出するチャンネル名（カンマ区切り）")
    args = ap.parse_args()

    host, port_str = args.dest.split(":")
    port = int(port_str)
    ch_list = [c.strip() for c in args.channels.split(",")]

    # glob展開
    paths = []
    for pat in args.files:
        expanded = glob.glob(pat)
        paths.extend(expanded if expanded else [pat])

    if not paths:
        raise SystemExit("MiniSEEDファイルが見つかりません")

    # 読み込みとチャンネル別トレース取得
    st = Stream()
    for p in paths:
        st += read(p)
    st.merge(method=1, fill_value="interpolate")
    st.sort()

    traces = {}
    for ch in ch_list:
        sel = st.select(channel=ch)
        if not sel:
            print(f"[WARN] {ch} が見つかりません、スキップします")
            continue
        tr = sel[0]
        traces[ch] = tr
        print(f"[INFO] {ch}: {tr.stats.starttime} → {tr.stats.endtime}  "
              f"npts={tr.stats.npts}  fs={tr.stats.sampling_rate}Hz")

    if not traces:
        raise SystemExit("有効なチャンネルがありません")

    # 共通の開始・終了時刻（最も遅い開始、最も早い終了）
    t_start = max(tr.stats.starttime for tr in traces.values())
    t_end   = min(tr.stats.endtime   for tr in traces.values())
    fs = list(traces.values())[0].stats.sampling_rate
    n_samples = int((t_end - t_start) * fs)

    print(f"[INFO] 再生区間: {t_start} → {t_end}  ({n_samples/fs:.1f}秒)")
    print(f"[INFO] 送出先: {host}:{port}  速度: {args.speed}x  pkt={args.pkt_samples}samples")
    print("[INFO] Ctrl+C で停止")

    # 各チャンネルのデータをt_start起点でスライス
    ch_data = {}
    for ch, tr in traces.items():
        i_start = int((t_start - tr.stats.starttime) * fs)
        ch_data[ch] = tr.data[i_start:i_start + n_samples]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt_interval = args.pkt_samples / fs  # 実時間での1パケット間隔（秒）
    wall_start = time.time()
    sample_idx = 0

    try:
        while sample_idx + args.pkt_samples <= n_samples:
            # このパケットの波形上のタイムスタンプ
            pkt_t0 = float(t_start) + sample_idx / fs

            for ch in ch_list:
                if ch not in ch_data:
                    continue
                samples = ch_data[ch][sample_idx:sample_idx + args.pkt_samples]
                pkt = make_packet(ch, pkt_t0, samples)
                sock.sendto(pkt, (host, port))

            sample_idx += args.pkt_samples

            # 次パケットの壁時計上の送出タイミング
            if args.speed > 0:
                elapsed_wave = sample_idx / fs          # 波形上の経過秒
                next_wall = wall_start + elapsed_wave / args.speed
                sleep_sec = next_wall - time.time()
                if sleep_sec > 0:
                    time.sleep(sleep_sec)

            # 進捗表示（5秒ごと）
            wave_elapsed = sample_idx / fs
            if sample_idx % int(fs * 5) < args.pkt_samples:
                print(f"\r[INFO] {wave_elapsed:.0f}/{n_samples/fs:.0f}秒 送出中...", end="", flush=True)

    except KeyboardInterrupt:
        print("\n[INFO] 中断しました。")
    finally:
        print(f"\n[INFO] 完了。{sample_idx/fs:.1f}秒分を送出しました。")
        sock.close()


if __name__ == "__main__":
    main()
