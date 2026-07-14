#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GET /api/hvsr_history（HVSR日次モニタリング履歴 読み取り専用API）のユニットテスト。

test_api_events.py のASGI直接呼び出しパターンを踏襲する。

実行:
    source .venv/bin/activate
    pytest src/test_api_hvsr_history.py -v
"""
import asyncio
import json
import os
import sys
from urllib.parse import urlencode

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import jma_intensity_web  # noqa: E402


SAMPLE_ENTRIES = [
    {
        "capture_date": "2026-07-12", "computed_at": "2026-07-12T05:33:00+09:00",
        "station": "R38DC", "status": "ok",
        "n_windows_total": 539, "n_windows_used": 103, "reject_ratio": 0.809,
        "window_length_s": 40.0, "window_overlap": 0.5,
        "peak_frequency_hz": 0.91, "peak_amplitude": 3.42,
        "freq_hz": [0.2, 0.5, 20.0], "hv_ratio": [1.05, 2.0, 0.34],
        "smoothing": {"method": "konno_ohmachi", "b": 40},
        "sesame_criteria": {"window_length_ok": True, "amplitude_ok": True,
                            "stability_ok": False, "peak_freq_std_hz": 0.12},
        "weather_note": "",
    },
    {
        "capture_date": "2026-07-13", "computed_at": "2026-07-13T05:33:00+09:00",
        "station": "R38DC", "status": "insufficient_data",
        "n_windows_total": 539, "n_windows_used": 12, "reject_ratio": 0.978,
        "window_length_s": 40.0, "window_overlap": 0.5,
        "peak_frequency_hz": 0.95, "peak_amplitude": 1.8,
        "freq_hz": [0.2, 0.5, 20.0], "hv_ratio": [0.9, 1.8, 0.2],
        "smoothing": {"method": "konno_ohmachi", "b": 40},
        "sesame_criteria": {"window_length_ok": True, "amplitude_ok": False,
                            "stability_ok": False, "peak_freq_std_hz": 0.3},
        "weather_note": "大雨",
    },
    {
        "capture_date": "2026-07-14", "computed_at": "2026-07-14T05:33:00+09:00",
        "station": "R38DC", "status": "failed",
        "n_windows_total": 539, "n_windows_used": 0, "reject_ratio": 1.0,
        "window_length_s": 40.0, "window_overlap": 0.5,
        "peak_frequency_hz": None, "peak_amplitude": None,
        "freq_hz": None, "hv_ratio": None,
        "smoothing": {"method": "konno_ohmachi", "b": 40},
        "sesame_criteria": None,
        "weather_note": "",
    },
]


@pytest.fixture
def history_file(tmp_path):
    """サンプル履歴＋空行＋破損行を含む hvsr_history.jsonl を作る。"""
    p = tmp_path / "hvsr_history.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in SAMPLE_ENTRIES]
    lines.insert(1, "")
    lines.insert(2, "{ this is not valid json")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def app_with_history(history_file, monkeypatch):
    monkeypatch.setattr(jma_intensity_web, "_HVSR_HISTORY_PATH", history_file)
    # モジュールレベルキャッシュをテスト間で共有させない
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_cache", [])
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_mtime", None)
    return jma_intensity_web.app


def _asgi_get(app, path: str, params: dict | None = None) -> tuple[int, dict]:
    """ASGI アプリに GET リクエストを1本投げ、(status, json_body) を返す。"""
    query = urlencode(params or {}).encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }

    body_chunks: list[bytes] = []
    status_holder: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    body = b"".join(body_chunks)
    return status_holder["status"], json.loads(body.decode("utf-8"))


# ===== _read_hvsr_history 単体テスト =====

def test_read_all_skips_broken_lines(history_file):
    entries = jma_intensity_web._read_hvsr_history(history_file)
    assert len(entries) == len(SAMPLE_ENTRIES)
    dates = [e["capture_date"] for e in entries]
    assert dates == ["2026-07-12", "2026-07-13", "2026-07-14"]


def test_read_missing_file_returns_empty(tmp_path):
    entries = jma_intensity_web._read_hvsr_history(tmp_path / "does_not_exist.jsonl")
    assert entries == []


def test_read_empty_file_returns_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert jma_intensity_web._read_hvsr_history(p) == []


def test_sesame_criteria_field_round_trips(history_file):
    """sesame_criteriaフィールドを含むレコードが正しく往復すること。"""
    entries = jma_intensity_web._read_hvsr_history(history_file)
    ok_entry = next(e for e in entries if e["status"] == "ok")
    assert ok_entry["sesame_criteria"] == {
        "window_length_ok": True, "amplitude_ok": True,
        "stability_ok": False, "peak_freq_std_hz": 0.12,
    }
    failed_entry = next(e for e in entries if e["status"] == "failed")
    assert failed_entry["sesame_criteria"] is None


# ===== エンドポイント結合テスト =====

def test_endpoint_all(app_with_history):
    status, data = _asgi_get(app_with_history, "/api/hvsr_history")
    assert status == 200
    assert data["count"] == 3
    assert [e["capture_date"] for e in data["history"]] == [
        "2026-07-12", "2026-07-13", "2026-07-14",
    ]


def test_endpoint_limit(app_with_history):
    _, data = _asgi_get(app_with_history, "/api/hvsr_history", {"limit": 2})
    assert data["count"] == 2
    # limitは直近の日（新しい順で切り出した後、古い順に戻す）
    assert [e["capture_date"] for e in data["history"]] == ["2026-07-13", "2026-07-14"]


def test_endpoint_limit_zero(app_with_history):
    _, data = _asgi_get(app_with_history, "/api/hvsr_history", {"limit": 0})
    assert data == {"count": 0, "history": []}


def test_endpoint_limit_over_max_422(app_with_history):
    status, _ = _asgi_get(app_with_history, "/api/hvsr_history",
                          {"limit": jma_intensity_web._HVSR_HISTORY_LIMIT_MAX + 1})
    assert status == 422


def test_endpoint_negative_limit_422(app_with_history):
    status, _ = _asgi_get(app_with_history, "/api/hvsr_history", {"limit": -1})
    assert status == 422


def test_endpoint_empty_file(monkeypatch, tmp_path):
    monkeypatch.setattr(jma_intensity_web, "_HVSR_HISTORY_PATH", tmp_path / "nope.jsonl")
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_cache", [])
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_mtime", None)
    _, data = _asgi_get(jma_intensity_web.app, "/api/hvsr_history")
    assert data == {"count": 0, "history": []}


def test_endpoint_response_shape(app_with_history):
    _, data = _asgi_get(app_with_history, "/api/hvsr_history", {"limit": 1})
    assert set(data.keys()) == {"count", "history"}
    entry = data["history"][0]
    assert "sesame_criteria" in entry
    assert "weather_note" in entry


def test_endpoint_reflects_file_change_via_mtime(app_with_history, history_file):
    """mtimeが変化した場合、キャッシュが再読み込みされて新規エントリが反映されること。"""
    status, data = _asgi_get(app_with_history, "/api/hvsr_history")
    assert data["count"] == 3

    new_entry = dict(SAMPLE_ENTRIES[-1])
    new_entry["capture_date"] = "2026-07-15"
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(new_entry, ensure_ascii=False) + "\n")

    _, data2 = _asgi_get(app_with_history, "/api/hvsr_history")
    assert data2["count"] == 4
    assert data2["history"][-1]["capture_date"] == "2026-07-15"


def test_endpoint_sorts_legacy_week_start_records_before_capture_date(monkeypatch, tmp_path):
    """capture_date移行前（週次実行時代）のweek_startのみのレコードも、
    capture_dateフィールドを持つ新しいレコードとの混在時に日付順で正しく並ぶこと
    （後方互換フォールバック: capture_date優先、なければweek_start）。"""
    legacy_entry = dict(SAMPLE_ENTRIES[0])
    del legacy_entry["capture_date"]
    legacy_entry["week_start"] = "2026-07-06"

    p = tmp_path / "hvsr_history.jsonl"
    lines = [json.dumps(legacy_entry, ensure_ascii=False)] + \
        [json.dumps(e, ensure_ascii=False) for e in SAMPLE_ENTRIES[1:]]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr(jma_intensity_web, "_HVSR_HISTORY_PATH", p)
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_cache", [])
    monkeypatch.setattr(jma_intensity_web, "_hvsr_history_mtime", None)

    _, data = _asgi_get(jma_intensity_web.app, "/api/hvsr_history")
    assert data["count"] == 3
    # 2026-07-06 (legacy week_start) -> 2026-07-13 -> 2026-07-14 の順
    assert data["history"][0]["week_start"] == "2026-07-06"
    assert data["history"][1]["capture_date"] == "2026-07-13"
    assert data["history"][2]["capture_date"] == "2026-07-14"
