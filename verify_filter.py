#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JMAフィルタ特性と計測震度計算の合成信号検証

検証項目:
1. フィルタ振幅特性 - 各周波数での理論値との一致
2. f=0 の扱い - DC成分が正しく0になるか
3. 計測震度の逆算 - 既知のI値から逆算した振幅で計算が一致するか
4. 0.3s閾値の動作 - 短いスパイクと定常波で挙動確認
"""

import numpy as np
from jma_intensity_realtime import (
    apply_jma_filter_time,
    jma_frequency_response,
    a_threshold_for_03s,
    jma_scale_from_I,
)

PASS = "  PASS"
FAIL = "  FAIL"

def check(label, ok, detail=""):
    mark = PASS if ok else FAIL
    line = f"{mark}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok

# ===== 1. フィルタ振幅特性 =====
print("=" * 60)
print("1. フィルタ振幅特性（単一周波数正弦波）")
print("=" * 60)

fs = 100.0
duration = 60.0  # 秒（FFT精度のため長め）
t = np.arange(0, duration, 1.0 / fs)
n = len(t)

# 理論応答 H(f) との比較
test_freqs = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0]
all_pass = True
print(f"  {'周波数':>6}  {'理論H(f)':>10}  {'実測比':>10}  {'誤差%':>8}")
print(f"  {'-'*6}  {'-'*10}  {'-'*10}  {'-'*8}")
for f0 in test_freqs:
    amp_in = 1.0
    sig = amp_in * np.sin(2.0 * np.pi * f0 * t)
    filtered = apply_jma_filter_time(sig, fs)
    # 定常部分（端効果除去: 前後10秒除く）
    trim = int(10.0 * fs)
    amp_out = np.max(np.abs(filtered[trim:-trim]))
    H_theory = float(jma_frequency_response(np.array([f0]))[0])
    ratio = amp_out / amp_in if amp_in > 0 else 0.0
    err_pct = abs(ratio - H_theory) / (H_theory + 1e-30) * 100.0
    ok = err_pct < 5.0  # 5%以内を合格
    all_pass = all_pass and ok
    mark = "" if ok else " NG"
    print(f"  {f0:>5.1f}Hz  {H_theory:>10.4f}  {ratio:>10.4f}  {err_pct:>7.2f}%{mark}")

check("フィルタ振幅特性（全周波数5%以内）", all_pass)

# ===== 2. DC成分(f=0)が0になるか =====
print()
print("=" * 60)
print("2. DC成分の扱い（f=0 → H=0）")
print("=" * 60)

dc_signal = np.ones(n) * 1000.0  # 大きなDCオフセット
filtered_dc = apply_jma_filter_time(dc_signal, fs)
trim = int(10.0 * fs)
dc_out = np.max(np.abs(filtered_dc[trim:-trim]))
ok_dc = dc_out < 1e-6
check("DC成分がゼロになる", ok_dc, f"出力最大値={dc_out:.2e}")

# ===== 3. 計測震度の逆算検証 =====
print()
print("=" * 60)
print("3. 計測震度の逆算検証（目標I値から振幅設計→計算値と一致確認）")
print("=" * 60)
# I = 2*log10(a_gal) + 0.94
# → a_gal = 10^((I - 0.94) / 2)
# フィルタ応答H(f)のピーク付近(f=1Hzが応答大きい)の正弦波を使う
# フィルタ後の振幅 = amp * H(1Hz) なので
# amp = a_mps2 / H(1Hz) として入力する

f0 = 1.0  # Hz（フィルタ応答が大きい周波数）
H1 = float(jma_frequency_response(np.array([f0]))[0])
target_Is = [1.0, 2.0, 3.0, 4.0, 5.0]

print(f"  {'目標I':>6}  {'目標a_gal':>10}  {'計算I':>8}  {'差':>8}")
print(f"  {'-'*6}  {'-'*10}  {'-'*8}  {'-'*8}")
all_pass3 = True
for I_target in target_Is:
    a_gal_target = 10.0 ** ((I_target - 0.94) / 2.0)
    a_mps2_target = a_gal_target / 100.0
    # フィルタ後に a_mps2_target が得られるよう逆算
    amp_in = a_mps2_target / H1
    sig = amp_in * np.sin(2.0 * np.pi * f0 * t)
    # 3成分合成（Z=sig, N=0, E=0 で合成ベクトル=sigそのもの）
    az = apply_jma_filter_time(sig, fs)
    an = apply_jma_filter_time(np.zeros_like(sig), fs)
    ae = apply_jma_filter_time(np.zeros_like(sig), fs)
    vec = np.sqrt(az ** 2 + an ** 2 + ae ** 2)
    a_mps2 = a_threshold_for_03s(np.abs(vec), fs)
    a_gal = a_mps2 * 100.0
    I_raw = 2.0 * np.log10(a_gal) + 0.94
    I_calc = float(np.floor(np.round(I_raw, 3) * 100.0) / 100.0)
    diff = abs(I_calc - I_target)
    ok = diff < 0.1
    all_pass3 = all_pass3 and ok
    mark = "" if ok else " NG"
    print(f"  {I_target:>6.1f}  {a_gal_target:>10.3f}  {I_calc:>8.2f}  {diff:>8.3f}{mark}")

check("逆算I値と計算I値が0.1以内で一致", all_pass3)

# ===== 4. 0.3s閾値の動作確認 =====
print()
print("=" * 60)
print("4. 0.3s閾値（a_threshold_for_03s）の動作確認")
print("=" * 60)

# ケース A: 定常正弦波 → 最大振幅に近い値が返るはず
t30 = np.arange(0, 30.0, 1.0 / fs)
steady = 1.0 * np.abs(np.sin(2.0 * np.pi * 1.0 * t30))
val_steady = a_threshold_for_03s(steady, fs)
ok_steady = abs(val_steady - 1.0) < 0.01
check("定常波: 最大振幅近傍の値", ok_steady, f"val={val_steady:.4f} (期待≈1.0)")

# ケース B: 短いスパイク（0.1秒だけ大きい）→ 0.3s閾値を下回るはず
spike = np.zeros(n)
spike_len = int(0.1 * fs)  # 0.1秒分
spike[-spike_len:] = 100.0  # 末尾0.1秒に大きな値
val_spike = a_threshold_for_03s(np.abs(spike), fs)
ok_spike = val_spike < 100.0
check("短スパイク(0.1s): 100.0より小さい", ok_spike, f"val={val_spike:.4f}")

# ケース C: 長いスパイク（0.5秒）→ 0.3s閾値を超えるはず
long_spike = np.zeros(n)
long_spike_len = int(0.5 * fs)
long_spike[-long_spike_len:] = 100.0
val_long = a_threshold_for_03s(np.abs(long_spike), fs)
ok_long = val_long >= 100.0
check("長スパイク(0.5s): 100.0以上", ok_long, f"val={val_long:.4f}")

# ===== 5. jma_scale_from_I の境界値 =====
print()
print("=" * 60)
print("5. jma_scale_from_I 境界値確認")
print("=" * 60)

boundaries = [
    (-1.0,  "0"),
    ( 0.0,  "0"),
    ( 0.49, "0"),
    ( 0.5,  "1"),
    ( 1.49, "1"),
    ( 1.5,  "2"),
    ( 2.49, "2"),
    ( 2.5,  "3"),
    ( 3.49, "3"),
    ( 3.5,  "4"),
    ( 4.49, "4"),
    ( 4.5,  "5弱"),
    ( 4.99, "5弱"),
    ( 5.0,  "5強"),
    ( 5.49, "5強"),
    ( 5.5,  "6弱"),
    ( 5.99, "6弱"),
    ( 6.0,  "6強"),
    ( 6.49, "6強"),
    ( 6.5,  "7"),
    ( 7.0,  "7"),
]
all_pass5 = True
for I_val, expected in boundaries:
    got = jma_scale_from_I(I_val)
    ok = got == expected
    all_pass5 = all_pass5 and ok
    if not ok:
        print(f"  NG  I={I_val:+.2f}  期待={expected}  実際={got}")
check("jma_scale_from_I 全境界値", all_pass5)

# ===== サマリー =====
print()
print("=" * 60)
