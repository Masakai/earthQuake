#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JMAフィルタ特性と計測震度計算の合成信号検証

pytest src/verify_filter.py -v で実行する。

検証項目:
1. フィルタ振幅特性 - 各周波数での理論値との一致
2. f=0 の扱い - DC成分が正しく0になるか
3. 計測震度の逆算 - 既知のI値から逆算した振幅で計算が一致するか
4. 0.3s閾値の動作 - 短いスパイクと定常波で挙動確認
5. jma_scale_from_I の境界値
6. compute_intensity_timeseries が realtime と同じ値を返すか

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
"""

import sys
import os
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from jma_intensity_realtime import (
    apply_jma_filter_time,
    jma_frequency_response,
    a_threshold_for_03s,
    jma_scale_from_I,
)
from analyze_rs import compute_intensity_timeseries


FS = 100.0
DURATION = 60.0
T = np.arange(0, DURATION, 1.0 / FS)


# ===== 1. フィルタ振幅特性 =====

@pytest.mark.parametrize("f0", [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0])
def test_filter_amplitude(f0):
    sig = np.sin(2.0 * np.pi * f0 * T)
    filtered = apply_jma_filter_time(sig, FS)
    trim = int(10.0 * FS)
    amp_out = np.max(np.abs(filtered[trim:-trim]))
    H_theory = float(jma_frequency_response(np.array([f0]))[0])
    err_pct = abs(amp_out - H_theory) / (H_theory + 1e-30) * 100.0
    assert err_pct < 5.0, f"f={f0}Hz: 誤差{err_pct:.2f}% > 5%  (理論={H_theory:.4f}, 実測={amp_out:.4f})"


# ===== 2. DC成分(f=0)が0になるか =====

def test_filter_dc_zero():
    n = len(T)
    dc_signal = np.ones(n) * 1000.0
    filtered_dc = apply_jma_filter_time(dc_signal, FS)
    trim = int(10.0 * FS)
    dc_out = np.max(np.abs(filtered_dc[trim:-trim]))
    assert dc_out < 1e-6, f"DC出力が残っている: {dc_out:.2e}"


# ===== 3. 計測震度の逆算検証 =====

@pytest.mark.parametrize("I_target", [1.0, 2.0, 3.0, 4.0, 5.0])
def test_intensity_roundtrip(I_target):
    f0 = 1.0
    H1 = float(jma_frequency_response(np.array([f0]))[0])
    a_gal_target = 10.0 ** ((I_target - 0.94) / 2.0)
    a_mps2_target = a_gal_target / 100.0
    amp_in = a_mps2_target / H1
    sig = amp_in * np.sin(2.0 * np.pi * f0 * T)
    az = apply_jma_filter_time(sig, FS)
    an = apply_jma_filter_time(np.zeros_like(sig), FS)
    ae = apply_jma_filter_time(np.zeros_like(sig), FS)
    vec = np.sqrt(az ** 2 + an ** 2 + ae ** 2)
    a_mps2 = a_threshold_for_03s(np.abs(vec), FS)
    a_gal = a_mps2 * 100.0
    I_raw = 2.0 * np.log10(a_gal) + 0.94
    I_calc = float(np.floor(np.round(I_raw, 3) * 100.0) / 100.0)
    diff = abs(I_calc - I_target)
    assert diff < 0.1, f"I_target={I_target}: 計算値={I_calc:.3f}, 差={diff:.4f}"


# ===== 4. 0.3s閾値の動作確認 =====

def test_threshold_steady():
    n = len(T)
    t30 = np.arange(0, 30.0, 1.0 / FS)
    steady = 1.0 * np.abs(np.sin(2.0 * np.pi * 1.0 * t30))
    val = a_threshold_for_03s(steady, FS)
    assert abs(val - 1.0) < 0.01, f"定常波: val={val:.4f} (期待≈1.0)"


def test_threshold_short_spike():
    n = len(T)
    spike = np.zeros(n)
    spike_len = int(0.1 * FS)  # 0.1秒
    spike[-spike_len:] = 100.0
    val = a_threshold_for_03s(np.abs(spike), FS)
    assert val < 100.0, f"0.1sスパイク: val={val:.4f} (100.0未満であるべき)"


def test_threshold_long_spike():
    n = len(T)
    long_spike = np.zeros(n)
    long_spike_len = int(0.5 * FS)  # 0.5秒
    long_spike[-long_spike_len:] = 100.0
    val = a_threshold_for_03s(np.abs(long_spike), FS)
    assert val >= 100.0, f"0.5sスパイク: val={val:.4f} (100.0以上であるべき)"


# ===== 5. jma_scale_from_I の境界値 =====

@pytest.mark.parametrize("I_val,expected", [
    (-1.0, "0"), ( 0.0, "0"), ( 0.49, "0"),
    ( 0.5, "1"), ( 1.49, "1"),
    ( 1.5, "2"), ( 2.49, "2"),
    ( 2.5, "3"), ( 3.49, "3"),
    ( 3.5, "4"), ( 4.49, "4"),
    ( 4.5, "5弱"), ( 4.99, "5弱"),
    ( 5.0, "5強"), ( 5.49, "5強"),
    ( 5.5, "6弱"), ( 5.99, "6弱"),
    ( 6.0, "6強"), ( 6.49, "6強"),
    ( 6.5, "7"), ( 7.0, "7"),
])
def test_jma_scale_boundaries(I_val, expected):
    got = jma_scale_from_I(I_val)
    assert got == expected, f"I={I_val}: 期待={expected}, 実際={got}"


# ===== 6. compute_intensity_timeseries が realtime と同じ値を返すか =====

@pytest.mark.parametrize("I_target", [2.0, 3.0, 4.0])
def test_compute_intensity_timeseries_matches_realtime(I_target):
    f0 = 1.0
    H1 = float(jma_frequency_response(np.array([f0]))[0])
    a_gal_target = 10.0 ** ((I_target - 0.94) / 2.0)
    amp_in = (a_gal_target / 100.0) / H1
    sig = amp_in * np.sin(2.0 * np.pi * f0 * T)

    az = apply_jma_filter_time(sig, FS)
    vec = np.abs(az)

    # realtime 方式
    a_mps2_rt = a_threshold_for_03s(vec, FS)
    I_rt = 2.0 * np.log10(a_mps2_rt * 100.0) + 0.94

    # analyze_rs 方式（gal 単位で渡す）
    I_arr = compute_intensity_timeseries(az * 100.0, FS, window_s=90.0)
    I_analyze = float(I_arr[-1])

    diff = abs(I_analyze - I_rt)
    assert diff < 0.05, (
        f"I_target={I_target}: realtime={I_rt:.4f}, analyze_rs={I_analyze:.4f}, 差={diff:.4f}"
    )
