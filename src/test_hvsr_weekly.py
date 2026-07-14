#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HVSR週次モニタリング（src/hvsr_weekly.py）のユニットテスト・統合テスト。

実行:
    source .venv/bin/activate
    pytest src/test_hvsr_weekly.py -v
"""
import os
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

import hvsr_weekly as hw  # noqa: E402


FS = 100.0
_DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


def _make_synthetic(duration_s: float, peak_freq_hz: float, seed: int = 42,
                     noise_h: float = 0.3, noise_z: float = 1.0):
    """常時微動を模擬した3成分合成波形を作る。

    ENZは広帯域ノイズのみ、ENN/ENEは指定周波数の正弦波+ノイズとすることで、
    H/V比が peak_freq_hz にピークを持つ合成データになる。
    """
    n = int(duration_s * FS)
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed)
    peak_signal = np.sin(2 * np.pi * peak_freq_hz * t) * 0.3
    enz = rng.normal(0, noise_z, n)
    enn = rng.normal(0, noise_h, n) + peak_signal
    ene = rng.normal(0, noise_h, n) + peak_signal
    return enz, enn, ene


# ===== コア関数の単体テスト =====

def test_split_windows_count_and_overlap():
    """3時間・40秒窓・50%オーバーラップで539窓に分割されること（設計書の算出根拠と一致）。"""
    n = int(3 * 3600 * FS)
    windows = hw.split_windows(n, FS)
    assert len(windows) == 539
    # 各窓の長さは40秒(=4000サンプル)固定
    assert all(e - s == 4000 for s, e in windows)
    # オーバーラップ50%: 次の窓の開始は前の窓の半分だけ進む
    assert windows[1][0] - windows[0][0] == 2000


def test_apply_cosine_taper_edges_near_zero():
    """コサインテーパー適用後、両端の値がほぼ0になること。"""
    vec = np.ones(4000)
    tapered = hw.apply_cosine_taper(vec, fraction=0.05)
    assert tapered[0] < 0.01
    assert tapered[-1] < 0.01
    # 中央部はテーパーの影響を受けない
    assert tapered[2000] == pytest.approx(1.0)


@pytest.mark.parametrize("peak_freq_hz", [0.5, 1.0, 2.0, 5.0])
def test_known_synthetic_peak_frequency(peak_freq_hz):
    """既知の周波数の正弦波+ノイズで、期待通りのピーク周波数がHVSR計算から得られること。"""
    enz, enn, ene = _make_synthetic(duration_s=3 * 3600, peak_freq_hz=peak_freq_hz)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert result["status"] in ("ok", "insufficient_data")
    assert result["peak_frequency_hz"] == pytest.approx(peak_freq_hz, rel=0.1)
    # 平滑化・出力周波数軸は0.2〜20Hzの対数等間隔81点（FREQ_N_POINTS）固定であること
    assert len(result["freq_hz"]) == hw.FREQ_N_POINTS
    assert len(result["hv_ratio"]) == hw.FREQ_N_POINTS
    assert result["freq_hz"][0] == pytest.approx(hw.FREQ_MIN_HZ, rel=0.01)
    assert result["freq_hz"][-1] == pytest.approx(hw.FREQ_MAX_HZ, rel=0.01)


def test_stack_log_average_is_geometric_mean():
    """対数平均（幾何平均）でスタッキングされること。"""
    curves = [np.array([1.0, 4.0]), np.array([1.0, 1.0])]
    stacked = hw.stack_log_average(curves)
    # 幾何平均: sqrt(1*1)=1, sqrt(4*1)=2
    assert stacked[0] == pytest.approx(1.0)
    assert stacked[1] == pytest.approx(2.0)


# ===== アンチトリガ（棄却ロジック）のテスト =====

def test_is_window_valid_accepts_steady_ratio():
    """STA/LTA比が[0.5, 2.0]内に収まる窓は有効と判定されること。"""
    ratio = np.full(4000, 1.0)
    assert hw.is_window_valid(ratio, 0, 4000) is True


def test_is_window_valid_rejects_spike():
    """窓内にSTA/LTA比が2.0を超える時刻が1つでもあれば棄却されること（地震様の非定常波形）。"""
    ratio = np.full(4000, 1.0)
    ratio[2000] = 3.5  # 地震様のスパイク
    assert hw.is_window_valid(ratio, 0, 4000) is False


def test_is_window_valid_rejects_low_ratio():
    """STA/LTA比が0.5を下回る（極端に静穏・不自然な）時刻を含む窓も棄却されること。"""
    ratio = np.full(4000, 1.0)
    ratio[100] = 0.1
    assert hw.is_window_valid(ratio, 0, 4000) is False


def test_antitrigger_rejects_synthetic_earthquake_like_segment():
    """人工的に地震様の非定常波形を混ぜた合成データで、該当窓が棄却されること。"""
    enz, enn, ene = _make_synthetic(duration_s=3 * 3600, peak_freq_hz=1.0)
    n = len(enz)
    # 中央付近に大振幅・短時間のパルス（地震のP波到達を模擬）を注入
    quake_start = n // 2
    quake_len = int(5 * FS)
    enz[quake_start:quake_start + quake_len] += 50.0
    enn[quake_start:quake_start + quake_len] += 50.0
    ene[quake_start:quake_start + quake_len] += 50.0

    windows = hw.split_windows(n, FS)
    stalta_z = hw.compute_stalta(enz, FS, hw.STA_S, hw.LTA_S)

    # パルスを含む窓が棄却されることを確認
    rejected_found = False
    for start, end in windows:
        if start <= quake_start < end:
            if not hw.is_window_valid(stalta_z, start, end):
                rejected_found = True
    assert rejected_found


# ===== status付与ロジックのテスト =====

def test_status_ok_when_enough_windows():
    """有効窓数がTARGET_N_WINDOWS以上ならstatus="ok"。"""
    enz, enn, ene = _make_synthetic(duration_s=3 * 3600, peak_freq_hz=1.0)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert result["n_windows_used"] >= hw.TARGET_N_WINDOWS
    assert result["status"] == "ok"


def test_status_insufficient_data_when_few_windows():
    """有効窓数が1件以上・目標未満ならstatus="insufficient_data"。"""
    # 短時間データにして有効窓数を目標(45)未満に抑える
    enz, enn, ene = _make_synthetic(duration_s=15 * 60, peak_freq_hz=1.0)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert 0 < result["n_windows_used"] < hw.TARGET_N_WINDOWS
    assert result["status"] == "insufficient_data"


def _inject_periodic_spikes(vec: np.ndarray, fs: float, interval_s: float = 20.0,
                             spike_len_samples: int = 50, amplitude: float = 100.0) -> np.ndarray:
    """40秒窓のどこにも必ず1つ入る間隔（interval_s < window_length_s/2）でスパイクを注入する。

    定常的な正弦波を全区間に足すだけでは「定常」とみなされ棄却されないため
    （STA/LTA比は非定常性を検出するものであり、一定振幅の連続信号には反応しない）、
    テストで「全窓棄却」を再現するには周期的な非定常スパイクが必要となる。
    """
    n = len(vec)
    step = int(interval_s * fs)
    out = vec.copy()
    for i in range(0, n, step):
        out[i:i + spike_len_samples] += amplitude
    return out


def test_status_failed_when_zero_valid_windows():
    """有効窓が0件の場合、status="failed"かつHVSR値がnullになること。"""
    n = int(3 * 3600 * FS)
    rng = np.random.default_rng(7)
    # 全窓に必ず地震様の非定常スパイクが入るようにして、すべての窓を棄却させる
    enz = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    enn = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    ene = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert result["status"] == "failed"
    assert result["n_windows_used"] == 0
    assert result["peak_frequency_hz"] is None
    assert result["peak_amplitude"] is None
    assert result["freq_hz"] is None
    assert result["hv_ratio"] is None
    assert result["sesame_criteria"] is None


def test_status_failed_when_zero_total_windows():
    """総窓数が0件（データが窓長未満）の場合もstatus="failed"。"""
    n = int(10 * FS)  # 10秒分のみ（40秒窓に満たない）
    enz = np.random.default_rng(1).normal(0, 1.0, n)
    enn = np.random.default_rng(2).normal(0, 1.0, n)
    ene = np.random.default_rng(3).normal(0, 1.0, n)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert result["status"] == "failed"
    assert result["n_windows_total"] == 0


# ===== SESAME簡易クライテリアのテスト =====

def test_window_length_ok_boundary():
    """window_length_ok: peak_frequency_hz が 0.25Hz(=10/40)を跨ぐ境界で真偽が切り替わること。"""
    below = hw.sesame_criteria_ok(peak_frequency_hz=0.24, peak_amplitude=3.0, peak_freq_std_hz=0.01)
    at = hw.sesame_criteria_ok(peak_frequency_hz=0.25, peak_amplitude=3.0, peak_freq_std_hz=0.01)
    above = hw.sesame_criteria_ok(peak_frequency_hz=0.26, peak_amplitude=3.0, peak_freq_std_hz=0.01)
    assert below["window_length_ok"] is False
    assert at["window_length_ok"] is False  # 厳密な超過条件（>）であり、境界値自体は満たさない
    assert above["window_length_ok"] is True


@pytest.mark.parametrize("amplitude,expected", [(1.9, False), (2.0, True), (2.1, True)])
def test_amplitude_ok_boundary_uses_gte(amplitude, expected):
    """amplitude_ok: >=2.0（>2ではない）であることを境界値1.9/2.0/2.1で確認する。"""
    result = hw.sesame_criteria_ok(peak_frequency_hz=1.0, peak_amplitude=amplitude, peak_freq_std_hz=0.01)
    assert result["amplitude_ok"] is expected


@pytest.mark.parametrize("peak_freq_hz,std,expected", [
    # 帯域 0.2-0.5Hz: epsilon = 0.20*f0
    (0.3, 0.20 * 0.3 * 0.5, True),   # 十分小さいばらつき
    (0.3, 0.20 * 0.3 * 2.0, False),  # 閾値超過
    # 帯域 0.5-1.0Hz: epsilon = 0.15*f0
    (0.8, 0.15 * 0.8 * 0.5, True),
    (0.8, 0.15 * 0.8 * 2.0, False),
    # 帯域 1.0-2.0Hz: epsilon = 0.10*f0
    (1.5, 0.10 * 1.5 * 0.5, True),
    (1.5, 0.10 * 1.5 * 2.0, False),
    # 帯域 >2.0Hz: epsilon = 0.05*f0
    (5.0, 0.05 * 5.0 * 0.5, True),
    (5.0, 0.05 * 5.0 * 2.0, False),
])
def test_stability_ok_band_boundaries(peak_freq_hz, std, expected):
    """stability_ok: Table 3の帯域別epsilon係数が正しく選択され、境界を跨ぐケースで真偽が一致すること。"""
    result = hw.sesame_criteria_ok(peak_frequency_hz=peak_freq_hz, peak_amplitude=3.0,
                                    peak_freq_std_hz=std)
    assert result["stability_ok"] is expected


def test_sesame_criteria_absent_when_status_failed():
    """status="failed"のとき、sesame_criteria全体がNoneになること（compute_hvsr_from_traces経由）。"""
    n = int(3 * 3600 * FS)
    rng = np.random.default_rng(7)
    enz = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    enn = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    ene = _inject_periodic_spikes(rng.normal(0, 1.0, n), FS)
    result = hw.compute_hvsr_from_traces(enz, enn, ene, FS)
    assert result["status"] == "failed"
    assert result["sesame_criteria"] is None


# ===== 統合テスト: 既存MiniSEEDキャッシュを使ったend-to-end動作確認 =====

_CACHE_FILES_EXIST = all(
    (_DATA_DIR / f"AM.R38DC.00.{ch}.20260626_222700_420s.ms").exists()
    for ch in ("ENZ", "ENN", "ENE")
)


@pytest.mark.skipif(not _CACHE_FILES_EXIST, reason="既存MiniSEEDキャッシュがdata/に見つからない")
def test_integration_with_cached_miniseed():
    """data/内の既存MiniSEEDキャッシュを使い、ダウンロード以降のロジックのみを
    キャッシュ済みデータでend-to-end動作確認する（analyze_rs.py --no-download と同じ発想）。

    このキャッシュは420秒（7分）分のみで45窓のスタッキング目標には遠く及ばないため
    status="insufficient_data"または"failed"になる想定だが、例外を送出せず
    正しい形式のレコードが返ることを確認する。
    """
    from obspy import read as obspy_read

    traces = {}
    fs = None
    for ch in ("ENZ", "ENN", "ENE"):
        path = _DATA_DIR / f"AM.R38DC.00.{ch}.20260626_222700_420s.ms"
        st = obspy_read(str(path))
        tr = st[0]
        traces[ch] = tr.data.astype(np.float64)
        fs = tr.stats.sampling_rate

    result = hw.compute_hvsr_from_traces(traces["ENZ"], traces["ENN"], traces["ENE"], fs)
    assert result["status"] in ("ok", "insufficient_data", "failed")
    assert "n_windows_total" in result
    assert "sesame_criteria" in result


# ===== capture_window_for_date のテスト =====

def test_capture_window_for_date_is_02_to_05_jst():
    import datetime
    d = datetime.date(2026, 7, 13)
    start_utc, end_utc = hw.capture_window_for_date(d)
    start_jst = start_utc.astimezone(hw.JST)
    end_jst = end_utc.astimezone(hw.JST)
    assert start_jst.hour == 2
    assert end_jst.hour == 5
    assert (end_jst - start_jst).total_seconds() == 3 * 3600


# ===== run() の失敗パスのテスト =====
#
# ダウンロード失敗（チャンネル取得0バイト等）を「イベント0件」「データなし」に
# 化けさせず、status="failed"のレコードとしてhistoryに明示的に残すことを確認する
# （_read_trigger_eventsのコメントにある「障害を『イベント0件』に化けさせない」
# という本プロジェクトの一貫した方針に従う、設計書「地震区間の除外」節参照）。

def test_run_writes_failed_entry_when_download_fails(tmp_path, monkeypatch):
    """download_channel()が全チャンネルで失敗（ファイル未生成）した場合、
    run()がstatus="failed"のレコードをhistoryに書き込み、例外を送出しないこと。"""
    history_path = tmp_path / "hvsr_history.jsonl"
    log_path = tmp_path / "hvsr_weekly.log"
    monkeypatch.setattr(hw, "_HISTORY_PATH", history_path)
    monkeypatch.setattr(hw, "_LOG_PATH", log_path)
    monkeypatch.setattr(hw, "_CACHE_DIR", tmp_path)

    def _fake_download_channel(station, channel, t_start, t_end, out_path):
        # 何も書き込まない = 公式FDSN・自局SeedLinkともに失敗した状態を模擬
        pass

    monkeypatch.setattr(hw, "download_channel", _fake_download_channel)

    import datetime
    result = hw.run(target_date_jst=datetime.date(2026, 7, 13))

    assert result["status"] == "failed"
    assert result["n_windows_total"] == 0
    assert result["n_windows_used"] == 0
    assert result["peak_frequency_hz"] is None
    assert result["sesame_criteria"] is None

    # historyファイルに実際に1行追記されていること（例外を送出せず記録されること）
    assert history_path.exists()
    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    written = json.loads(lines[0])
    assert written["status"] == "failed"
    assert written["capture_date"] == "2026-07-13"


def test_run_writes_failed_entry_when_one_channel_missing(tmp_path, monkeypatch):
    """3成分中1チャンネルでも取得失敗すればHVSR計算自体を行わずstatus="failed"にすること
    （3成分すべて揃わないとH/V比が計算できないため、部分的な成功は許容しない）。"""
    history_path = tmp_path / "hvsr_history.jsonl"
    log_path = tmp_path / "hvsr_weekly.log"
    monkeypatch.setattr(hw, "_HISTORY_PATH", history_path)
    monkeypatch.setattr(hw, "_LOG_PATH", log_path)
    monkeypatch.setattr(hw, "_CACHE_DIR", tmp_path)

    def _fake_download_channel(station, channel, t_start, t_end, out_path):
        if channel != "ENZ":
            out_path.write_bytes(b"dummy-not-empty")
        # ENZのみ失敗（ファイル未生成）

    monkeypatch.setattr(hw, "download_channel", _fake_download_channel)

    import datetime
    result = hw.run(target_date_jst=datetime.date(2026, 7, 13))

    assert result["status"] == "failed"
    assert history_path.exists()


def test_run_dry_run_does_not_write_history(tmp_path, monkeypatch):
    """--dry-run指定時はhistoryファイルへの追記を行わないこと。"""
    history_path = tmp_path / "hvsr_history.jsonl"
    log_path = tmp_path / "hvsr_weekly.log"
    monkeypatch.setattr(hw, "_HISTORY_PATH", history_path)
    monkeypatch.setattr(hw, "_LOG_PATH", log_path)
    monkeypatch.setattr(hw, "_CACHE_DIR", tmp_path)

    calls = []

    def _fake_download_channel(station, channel, t_start, t_end, out_path):
        calls.append(channel)

    monkeypatch.setattr(hw, "download_channel", _fake_download_channel)

    import datetime
    result = hw.run(target_date_jst=datetime.date(2026, 7, 13), dry_run=True)

    assert result["status"] == "dry_run"
    assert len(calls) == 3  # ENZ/ENN/ENEの3成分をダウンロード試行
    assert not history_path.exists()
