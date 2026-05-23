#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
震度シミュレーター: 合成波形をRaspberry Shake UDPパケット形式で送出する

使い方:
    .venv/bin/python3 simulate_udp.py --intensity 3.0 --duration 60

別ターミナルでTUIを起動しておく:
    .venv/bin/python3 jma_intensity_tui.py --station R38DC --rt-window 30 --lta 10

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
"""

import argparse
import math
import socket
import time

import numpy as np

from jma_intensity_realtime import jma_frequency_response


def design_signal(I_target: float, fs: float, sensitivity: float) -> float:
    """
    目標震度I_targetになるよう1Hz正弦波の振幅を設計して返す（単位: counts）
    Z成分のみに信号を入れる（N/E=0）ことで合成ベクトル=Z成分となり計算が単純になる
    """
    # I = 2*log10(a_gal) + 0.94  ->  a_gal = 10^((I-0.94)/2)
    a_gal_target = 10.0 ** ((I_target - 0.94) / 2.0)
    a_mps2_target = a_gal_target / 100.0

    # JMAフィルタH(1Hz)で割り戻して入力振幅を求める（Z成分のみ使用）
    f0 = 1.0
    H1 = float(jma_frequency_response(np.array([f0]))[0])
    amp_mps2 = a_mps2_target / H1
    amp_counts = amp_mps2 * sensitivity
    return amp_counts


def make_packet(channel: str, t0: float, samples: np.ndarray) -> bytes:
    """RS DATACAST形式のUDPパケットを生成"""
    vals = ",".join(str(int(round(v))) for v in samples)
    s = f"{{'{channel}', {t0:.6f}, {vals}}}"
    return s.encode("ascii")


def main():
    ap = argparse.ArgumentParser(description="計測震度シミュレーター（UDP送出）")
    ap.add_argument("--intensity", type=float, default=3.0, help="目標計測震度")
    ap.add_argument("--duration", type=float, default=60.0, help="送出時間[秒]")
    ap.add_argument("--fs", type=float, default=100.0, help="サンプリング周波数[Hz]")
    ap.add_argument("--pkt-samples", type=int, default=25, help="1パケットのサンプル数")
    ap.add_argument("--sensitivity", type=float, default=387867.0)
    ap.add_argument("--dest", type=str, default="127.0.0.1:8888", help="送出先アドレス:ポート")
    ap.add_argument("--f0", type=float, default=1.0, help="正弦波の周波数[Hz]")
    ap.add_argument("--noise-ratio", type=float, default=0.05,
                    help="信号振幅に対するノイズ比率 (0.05=5%%)")
    ap.add_argument("--quiet-sec", type=float, default=0.0,
                    help="地震前の静穏期間[秒]（ノイズのみ送出）")
    args = ap.parse_args()

    host, port_str = args.dest.split(":")
    port = int(port_str)

    amp_counts = design_signal(args.intensity, args.fs, args.sensitivity)
    pkt_interval = args.pkt_samples / args.fs  # 秒/パケット
    total_sec = args.quiet_sec + args.duration

    print(f"[INFO] 目標震度: I={args.intensity:.1f}")
    print(f"[INFO] 信号周波数: {args.f0:.1f}Hz  振幅: {amp_counts:.1f} counts")
    print(f"[INFO] 送出先: {host}:{port}")
    if args.quiet_sec > 0:
        print(f"[INFO] 静穏期: {args.quiet_sec:.0f}秒 → 地震: {args.duration:.0f}秒")
    print(f"[INFO] パケット間隔: {pkt_interval*1000:.1f}ms  ({args.pkt_samples}samples/{args.fs:.0f}Hz)")
    print("[INFO] Ctrl+C で停止")

    channels = ["ENZ", "ENN", "ENE"]
    # 静穏期のノイズ振幅: 信号振幅の1%（実機の静穏時に相当）
    noise_amp_quiet = amp_counts * 0.01

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    t_start = time.time()
    sample_idx = 0
    rng = np.random.default_rng(42)

    try:
        while True:
            t_now = time.time()
            elapsed = t_now - t_start
            if elapsed >= total_sec:
                print(f"\n[INFO] 完了。終了します。")
                break

            in_quiet = elapsed < args.quiet_sec
            if in_quiet and int(elapsed) != int(elapsed - 0.5):
                remaining = args.quiet_sec - elapsed
                print(f"\r[INFO] 静穏期... あと{remaining:.0f}秒で地震開始", end="", flush=True)

            pkt_t0 = t_start + sample_idx / args.fs
            t_samples = pkt_t0 + np.arange(args.pkt_samples) / args.fs

            for ch in channels:
                if in_quiet:
                    # 静穏期: 全成分ノイズのみ（微小振動）
                    signal = np.zeros(args.pkt_samples)
                    noise = rng.normal(0.0, noise_amp_quiet, size=args.pkt_samples)
                else:
                    # 地震期: Z成分に信号、N/Eはノイズのみ
                    noise = rng.normal(0.0, amp_counts * args.noise_ratio, size=args.pkt_samples)
                    if ch == "ENZ":
                        signal = amp_counts * np.sin(2.0 * math.pi * args.f0 * t_samples)
                    else:
                        signal = np.zeros(args.pkt_samples)

                # 重力バイアス（ENZのみ）
                bias = 3_803_600.0 if ch == "ENZ" else 0.0
                samples = signal + noise + bias

                pkt = make_packet(ch, pkt_t0, samples)
                sock.sendto(pkt, (host, port))

            sample_idx += args.pkt_samples

            # 次のパケット送出タイミングまで待機
            next_time = t_start + sample_idx / args.fs
            sleep_sec = next_time - time.time()
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    except KeyboardInterrupt:
        print("\n[INFO] 中断しました。")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
