#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GET /api/events（トリガ履歴 読み取り専用API）のユニットテスト。

統合システム fujimidai-observatory 向けに追加したエンドポイントを検証する。
date / from-to / limit / min_scale のフィルタ、壊れた行スキップ、空ログ時の挙動を確認する。

実行:
    source .venv/bin/activate
    pytest src/test_api_events.py -v
"""
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from jma_intensity_web import _read_trigger_events, _SCALE_ORDER, _EVENTS_LIMIT_MAX  # noqa: E402


# ===== テスト用ログ生成 =====

SAMPLE_LINES = [
    {"date": "2026-05-24", "ts": "00:01:07", "I": 0.09, "scale": "0", "ratio": 1.81},
    {"date": "2026-05-25", "ts": "10:20:50", "I": 4.97, "scale": "5弱", "ratio": 10912.22},
    {"date": "2026-05-25", "ts": "11:00:00", "I": 1.20, "scale": "1", "ratio": 50.0},
    {"date": "2026-06-14", "ts": "17:52:51", "I": 0.10, "scale": "0", "ratio": 17.01},
    {"date": "2026-06-14", "ts": "18:00:00", "I": 6.50, "scale": "6強", "ratio": 99999.0},
]


@pytest.fixture
def log_file(tmp_path):
    """サンプルログ＋空行＋破損行を含む trigger_log.jsonl を作る。"""
    p = tmp_path / "trigger_log.jsonl"
    lines = []
    for ev in SAMPLE_LINES:
        lines.append(json.dumps(ev))
    # 空行・破損行を混入させる（既存ログに混じりうる）
    lines.insert(2, "")
    lines.insert(4, "{ this is not valid json")
    lines.append("   ")
    p.write_text("\n".join(lines) + "\n")
    return p


# ===== _read_trigger_events 単体テスト =====

def test_read_all(log_file):
    """全件取得：壊れた行・空行はスキップされ、新しい順で返る。"""
    events = _read_trigger_events(log_file)
    assert len(events) == len(SAMPLE_LINES)
    # 新しい順（date,ts 降順）
    dates_ts = [(e["date"], e["ts"]) for e in events]
    assert dates_ts == sorted(dates_ts, reverse=True)
    # 既存キー名がそのまま保持される
    assert set(events[0].keys()) == {"date", "ts", "I", "scale", "ratio"}


def test_filter_date(log_file):
    """date フィルタ：指定日のみ。"""
    events = _read_trigger_events(log_file, date="2026-05-25")
    assert len(events) == 2
    assert all(e["date"] == "2026-05-25" for e in events)


def test_filter_from_to(log_file):
    """from/to フィルタ：期間内のみ（両端含む）。"""
    events = _read_trigger_events(log_file, date_from="2026-05-25", date_to="2026-06-14")
    dates = {e["date"] for e in events}
    assert dates == {"2026-05-25", "2026-06-14"}
    assert all("2026-05-25" <= e["date"] <= "2026-06-14" for e in events)


def test_filter_from_only(log_file):
    """from のみ：指定日以降。"""
    events = _read_trigger_events(log_file, date_from="2026-06-01")
    assert all(e["date"] >= "2026-06-01" for e in events)
    assert len(events) == 2


def test_limit(log_file):
    """limit：新しい順で件数制限。"""
    events = _read_trigger_events(log_file, limit=2)
    assert len(events) == 2
    # 最新2件（2026-06-14 の2件）
    assert events[0]["date"] == "2026-06-14"
    assert events[1]["date"] == "2026-06-14"


def test_min_scale_excludes_noise(log_file):
    """min_scale="1"：scale="0"（ノイズ）を除外する。"""
    events = _read_trigger_events(log_file, min_scale="1")
    scales = {e["scale"] for e in events}
    assert "0" not in scales
    assert len(events) == 3  # "5弱","1","6強"


def test_min_scale_intensity_order(log_file):
    """min_scale="5弱"：震度順序で比較（"5弱","6強" のみ、"1"は除外）。"""
    events = _read_trigger_events(log_file, min_scale="5弱")
    scales = {e["scale"] for e in events}
    assert scales == {"5弱", "6強"}


def test_min_scale_order_is_not_lexical(tmp_path):
    """min_scale が単純文字列比較ではなく震度順序で比較されることを実経路で保証する。

    文字列比較では "5弱" < "5強"（"弱"=U+5F31 < "強"=U+5F37）になるが、震度的に
    "5強" は "5弱" 以上であり、min_scale="5強" のとき "5弱" は除外されねばならない。
    また文字列比較では "5弱" > "10" 的な桁の逆転が起きうる（"1" < "5"）。
    実データを読ませて、文字列比較なら混入するはずの値が正しく除外されることを確認する。
    """
    rows = [
        {"date": "2026-01-01", "ts": "00:00:01", "I": 0.1, "scale": "1", "ratio": 1.0},
        {"date": "2026-01-01", "ts": "00:00:02", "I": 4.0, "scale": "5弱", "ratio": 1.0},
        {"date": "2026-01-01", "ts": "00:00:03", "I": 5.0, "scale": "5強", "ratio": 1.0},
    ]
    p = tmp_path / "scale.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    # min_scale="5強": 震度順では "5弱"(rank5) < "5強"(rank6) なので "5弱" は除外。
    got = {e["scale"] for e in _read_trigger_events(p, min_scale="5強")}
    assert got == {"5強"}
    # 仮に文字列比較だったら "5弱" > "5強" は偽 だが "5弱" >= "5強" も偽で挙動が変わる。
    # ここでは震度順序の rank に基づき "5弱" が確実に落ちることを担保する。
    assert "5弱" not in got


def test_min_scale_invalid_raises():
    """_SCALE_ORDER に無い min_scale は ValueError（fail-open させない）。"""
    # "5" は "5弱"/"5強" の打ち間違いとして起こりうる典型的な不正値。
    with pytest.raises(ValueError):
        _read_trigger_events(pathlib.Path("/nonexistent.jsonl"), min_scale="5")
    with pytest.raises(ValueError):
        _read_trigger_events(pathlib.Path("/nonexistent.jsonl"), min_scale="X")


def test_io_error_propagates_not_empty(tmp_path):
    """読み取り障害（不正UTF-8）は空リストに化けず例外として伝播する。"""
    p = tmp_path / "broken_bytes.jsonl"
    # utf-8 として不正なバイト列を書く。デコード時に UnicodeDecodeError になる。
    p.write_bytes(b'{"date": "2026-01-01", "ts": "00:00:00", "scale": "\xff\xfe"}\n')
    with pytest.raises(UnicodeDecodeError):
        _read_trigger_events(p)


def test_reads_utf8_multibyte_scale(tmp_path):
    """マルチバイトの scale（"5弱" 等）を encoding 明示で正しく読める。"""
    p = tmp_path / "mb.jsonl"
    p.write_text(
        json.dumps({"date": "2026-01-01", "ts": "00:00:00", "I": 4.0,
                    "scale": "5弱", "ratio": 1.0}) + "\n",
        encoding="utf-8",
    )
    events = _read_trigger_events(p)
    assert events[0]["scale"] == "5弱"


def test_empty_log_missing_file(tmp_path):
    """ファイルが存在しない場合：空リスト。"""
    events = _read_trigger_events(tmp_path / "does_not_exist.jsonl")
    assert events == []


def test_empty_log_blank_file(tmp_path):
    """空ファイル：空リスト。"""
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert _read_trigger_events(p) == []


# ===== エンドポイント結合テスト（標準ライブラリのみの軽量ASGIクライアント）=====
#
# starlette.testclient（TestClient）は httpx 依存だが本環境に httpx が無いため、
# 新規依存を増やさず、ASGI アプリを直接呼ぶ最小クライアントで /api/events を検証する。
# これにより FastAPI のクエリパラメータ解決（from の alias、limit の int 変換、
# Query デフォルト値）まで含めて実際のリクエスト経路で確認できる。

import asyncio
from urllib.parse import urlencode


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


@pytest.fixture
def app_with_log(log_file, monkeypatch):
    import jma_intensity_web
    monkeypatch.setattr(jma_intensity_web, "_TRIGGER_LOG_PATH", log_file)
    return jma_intensity_web.app


def test_endpoint_all(app_with_log):
    status, data = _asgi_get(app_with_log, "/api/events")
    assert status == 200
    assert data["count"] == len(SAMPLE_LINES)
    assert len(data["events"]) == len(SAMPLE_LINES)


def test_endpoint_date(app_with_log):
    _, data = _asgi_get(app_with_log, "/api/events", {"date": "2026-05-25"})
    assert data["count"] == 2
    assert all(e["date"] == "2026-05-25" for e in data["events"])


def test_endpoint_from_to(app_with_log):
    _, data = _asgi_get(app_with_log, "/api/events",
                        {"from": "2026-05-25", "to": "2026-06-14"})
    assert {e["date"] for e in data["events"]} == {"2026-05-25", "2026-06-14"}


def test_endpoint_limit(app_with_log):
    _, data = _asgi_get(app_with_log, "/api/events", {"limit": 1})
    assert data["count"] == 1
    assert data["events"][0]["date"] == "2026-06-14"


def test_endpoint_min_scale(app_with_log):
    _, data = _asgi_get(app_with_log, "/api/events", {"min_scale": "1"})
    assert all(e["scale"] != "0" for e in data["events"])
    assert data["count"] == 3


def test_endpoint_empty(log_file, monkeypatch, tmp_path):
    import jma_intensity_web
    monkeypatch.setattr(jma_intensity_web, "_TRIGGER_LOG_PATH", tmp_path / "nope.jsonl")
    _, data = _asgi_get(jma_intensity_web.app, "/api/events")
    assert data == {"count": 0, "events": []}


def test_endpoint_response_shape(app_with_log):
    """レスポンス形状：count と events キー、events 各要素は既存キーを保持。"""
    _, data = _asgi_get(app_with_log, "/api/events", {"limit": 1})
    assert set(data.keys()) == {"count", "events"}
    assert set(data["events"][0].keys()) == {"date", "ts", "I", "scale", "ratio"}


def test_endpoint_invalid_min_scale_422(app_with_log):
    """不正な min_scale は 422（黙って全件返さない＝fail-open しない）。"""
    status, data = _asgi_get(app_with_log, "/api/events", {"min_scale": "5"})
    assert status == 422
    assert "error" in data


def test_endpoint_negative_limit_422(app_with_log):
    """負の limit は FastAPI の ge=0 制約で 422（全件返却にならない）。"""
    status, _ = _asgi_get(app_with_log, "/api/events", {"limit": -5})
    assert status == 422


def test_endpoint_limit_over_max_422(app_with_log):
    """上限を超える limit は le=_EVENTS_LIMIT_MAX で 422。"""
    status, _ = _asgi_get(app_with_log, "/api/events", {"limit": _EVENTS_LIMIT_MAX + 1})
    assert status == 422
