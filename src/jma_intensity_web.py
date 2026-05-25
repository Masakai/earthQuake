#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake UDP リアルタイム計測震度 Web ダッシュボード
- FastAPI + uvicorn による HTTP/WebSocket サーバー
- jma_intensity_tui.py から SharedState, recv_loop_fn, compute_loop, AlertSpeaker を import
- jma_intensity_realtime.py から Ring, jma_scale_from_I を import
- P2P地震情報は points/latitude/longitude を含む独自パーサで処理

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
"""

import argparse
import asyncio
import json
import math
import pathlib
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

import numpy as np

try:
    import urllib.request
    import urllib.error
    _urllib_ok = True
except ImportError:
    _urllib_ok = False

try:
    import websocket as _websocket_mod
    _websocket_ok = True
except ImportError:
    _websocket_ok = False

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from jma_intensity_tui import (
    SharedState,
    recv_loop_fn,
    compute_loop,
    AlertSpeaker,
    _p2p_scale_str,
    _parse_eew_item,
)
from jma_intensity_realtime import Ring, jma_scale_from_I


# ===== WebSocket クライアント管理 =====
_ws_clients: set = set()

_analyze_jobs: dict = {}  # job_id -> {status, out_path, error}
_analyze_lock = threading.Lock()


# ===== P2P地震情報（Web版: points/位置情報あり）=====

def _parse_quake_item_web(item: dict) -> dict | None:
    """code=551 の1アイテムを表示用dictに変換（points・緯度経度を含む）。不正な場合は None。"""
    eq = item.get("earthquake", {})
    if not eq:
        return None
    hypo = eq.get("hypocenter", {})
    points = item.get("points", [])
    return {
        "id": item.get("id", ""),
        "time": eq.get("time", "")[:16],
        "name": hypo.get("name", "不明"),
        "magnitude": hypo.get("magnitude", 0.0),
        "depth": hypo.get("depth", 0),
        "latitude": hypo.get("latitude", None),
        "longitude": hypo.get("longitude", None),
        "max_scale": _p2p_scale_str(eq.get("maxScale", -1)),
        "tsunami": eq.get("domesticTsunami", "None"),
        "points": [
            {
                "pref": p.get("pref", ""),
                "addr": p.get("addr", ""),
                "scale": _p2p_scale_str(p.get("scale", -1)),
            }
            for p in points
        ],
    }


def _fetch_p2p_quakes_http_web(limit: int = 5) -> list[dict]:
    """起動時に既存の地震情報をHTTPで初回取得（Web版パーサ使用）。失敗時は空リスト。"""
    if not _urllib_ok:
        return []
    try:
        url = f"https://api.p2pquake.net/v2/history?codes=551&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "rs4d-jma-intensity/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            items = json.loads(r.read())
        result = []
        for item in items:
            parsed = _parse_quake_item_web(item)
            if parsed:
                result.append(parsed)
        return result
    except Exception:
        return []


def p2p_ws_loop_web(shared: SharedState, stop_event: threading.Event):
    """WebSocket でP2P地震情報をリアルタイム受信（Web版: points付き）。自動再接続あり。"""
    WS_URL = "wss://api.p2pquake.net/v2/ws"

    initial = _fetch_p2p_quakes_http_web(20)
    seen_ids: set[str] = {q["id"] for q in initial}
    with shared._lock:
        shared.p2p_quakes = initial
        shared.p2p_seen_ids = seen_ids
        shared._p2p_seen_ids_fifo.extend(seen_ids)

    if not _websocket_ok:
        while not stop_event.is_set():
            quakes = _fetch_p2p_quakes_http_web(20)
            shared.update(p2p_quakes=quakes)
            stop_event.wait(60)
        return

    def on_message(ws, message):
        try:
            item = json.loads(message)
        except Exception:
            return
        code = item.get("code")
        item_id = item.get("id", "")

        with shared._lock:
            if item_id and item_id in shared.p2p_seen_ids:
                return
            if item_id:
                if len(shared._p2p_seen_ids_fifo) == shared._p2p_seen_ids_fifo.maxlen:
                    oldest = shared._p2p_seen_ids_fifo[0]
                    shared.p2p_seen_ids.discard(oldest)
                shared._p2p_seen_ids_fifo.append(item_id)
                shared.p2p_seen_ids.add(item_id)

        if code == 551:
            parsed = _parse_quake_item_web(item)
            if parsed:
                with shared._lock:
                    shared.p2p_quakes = ([parsed] + list(shared.p2p_quakes))[:20]
        elif code == 556:
            parsed_eew = _parse_eew_item(item)
            with shared._lock:
                shared.p2p_eew = parsed_eew
                shared._p2p_eew_received_at = time.time()

    def on_error(ws, error):
        pass

    def on_close(ws, close_status_code, close_msg):
        pass

    while not stop_event.is_set():
        try:
            ws = _websocket_mod.WebSocketApp(
                WS_URL,
                header={"User-Agent": "rs4d-jma-intensity/1.0"},
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        if not stop_event.is_set():
            stop_event.wait(5)


# ===== broadcast_loop =====

async def broadcast_loop(shared: SharedState):
    """1秒ごとに全WebSocketクライアントへ状態をブロードキャスト。"""
    while True:
        await asyncio.sleep(1.0)
        if not _ws_clients:
            continue

        snap = shared.snapshot()
        payload = {
            "fs": snap["fs"],
            "I_final": snap["I_final"],
            "a_gal": snap["a_gal"],
            "scale": snap["scale"],
            "ratio": snap["ratio"],
            "triggered": snap["triggered"],
            "pkt_count": snap["pkt_count"],
            "start_time": snap["start_time"],
            "raw_z": snap["raw_z"].tolist(),
            "raw_n": snap["raw_n"].tolist(),
            "raw_e": snap["raw_e"].tolist(),
            "i_history": snap["i_history"].tolist(),
            "ratio_history": snap["ratio_history"].tolist(),
            "events": list(snap["events"]),
            "p2p_quakes": list(snap["p2p_quakes"]),
            "p2p_eew": snap["p2p_eew"],
            "config": {
                "sta": _args.sta,
                "lta": _args.lta,
                "trig": _args.trig,
                "det_hold": _args.det_hold,
            },
        }
        message = json.dumps(payload)

        dead = set()
        for ws in list(_ws_clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


# ===== HTML =====

def _make_html(station: str, network: str, trig_thr: float,
               sta: float = 1.0, lta: float = 20.0, det_hold: float = 20.0) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RS4D 計測震度ダッシュボード</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
        background: #0d1117;
        color: #e6edf3;
        font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        font-size: 13px;
        min-height: 100vh;
    }}

    /* ===== ステータスバー ===== */
    #statusbar {{
        background: #161b22;
        border-bottom: 1px solid #30363d;
        padding: 6px 16px;
        display: flex;
        align-items: center;
        gap: 20px;
        font-size: 12px;
        color: #8b949e;
    }}
    #statusbar .station {{ color: #e6edf3; font-weight: bold; }}
    #statusbar .sep {{ color: #30363d; }}
    #statusbar .val {{ color: #58a6ff; }}
    #statusbar .uptime {{ margin-left: auto; }}
    #ws-dot {{
        width: 8px; height: 8px; border-radius: 50%;
        background: #8b949e; display: inline-block; margin-right: 4px;
        transition: background 0.3s;
    }}
    #ws-dot.connected {{ background: #3fb950; }}

    /* ===== 情報バナー ===== */
    #info-banner {{
        background: #161b22;
        border-bottom: 1px solid #30363d;
        height: 36px;
        display: flex;
        align-items: center;
        overflow: hidden;
        position: relative;
    }}
    #banner-label {{
        flex: 0 0 auto;
        padding: 0 12px;
        font-size: 13px;
        font-weight: bold;
        white-space: nowrap;
        border-right: 1px solid #30363d;
        color: #8b949e;
    }}
    #banner-label.eew {{
        background: #da3633;
        color: #fff;
        border-right: none;
    }}
    #banner-scroll-wrap {{
        flex: 1;
        overflow: hidden;
        position: relative;
        height: 100%;
    }}
    #banner-ticker {{
        display: inline-flex;
        align-items: center;
        height: 100%;
        white-space: nowrap;
        font-size: 15px;
        gap: 32px;
        animation: ticker-scroll 60s linear infinite;
    }}
    #banner-ticker.eew-mode {{
        animation: none;
        font-weight: bold;
        color: #fff;
        font-size: 15px;
    }}
    #info-banner.eew-active {{
        background: #da3633;
        animation: blink-bg 1s step-end infinite;
    }}
    .ticker-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .ticker-pref {{ color: #8b949e; font-size: 13px; }}
    .ticker-place {{ color: #e6edf3; }}
    .ticker-scale {{ font-weight: bold; margin-left: 2px; }}

    @keyframes ticker-scroll {{
        0%   {{ transform: translateX(100%); }}
        100% {{ transform: translateX(-100%); }}
    }}
    @keyframes blink-bg {{
        0%, 100% {{ background: #da3633; }}
        50%       {{ background: #b91c1c; }}
    }}

    /* ===== メインレイアウト ===== */
    #main {{
        display: grid;
        grid-template-columns: 1fr 1.4fr 1fr;
        gap: 8px;
        padding: 8px;
        height: calc(100vh - 72px);
    }}

    .panel {{
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 12px;
        overflow: hidden;
    }}
    .panel-title {{
        font-size: 11px;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 8px;
        border-bottom: 1px solid #21262d;
        padding-bottom: 6px;
    }}

    /* ===== 左カラム ===== */
    #col-left {{
        grid-column: 1;
        grid-row: 1;
        display: flex;
        flex-direction: column;
        gap: 8px;
    }}

    #intensity-panel {{ flex: 0 0 auto; }}
    #scale-display {{
        text-align: center;
        padding: 12px 0 8px;
    }}
    #scale-num {{
        font-size: 160px;
        font-weight: 900;
        line-height: 1;
        color: #a0d8ef;
        text-shadow: 0 0 40px rgba(160,216,239,0.5);
        transition: color 0.5s, text-shadow 0.5s;
    }}
    #scale-label {{
        font-size: 13px;
        color: #8b949e;
        margin-top: 4px;
    }}
    #intensity-bar-wrap {{ margin: 10px 0 4px; position: relative; }}
    #intensity-bar-bg {{
        height: 12px;
        background: #21262d;
        border-radius: 6px;
        overflow: hidden;
    }}
    #intensity-bar-fill {{
        height: 100%;
        width: 0%;
        background: linear-gradient(90deg, #3fb950, #f97316);
        border-radius: 6px;
        transition: width 0.5s;
    }}
    #intensity-vals {{
        display: flex;
        justify-content: space-between;
        margin-top: 6px;
        font-size: 12px;
    }}
    #i-value {{ color: #e6edf3; font-weight: bold; }}
    #a-value {{ color: #8b949e; }}

    #stalta-panel {{ flex: 0 0 auto; }}
    #stalta-bar-wrap {{ margin: 6px 0 4px; }}
    #stalta-bar-bg {{
        height: 10px;
        background: #21262d;
        border-radius: 5px;
        overflow: visible;
        position: relative;
    }}
    #stalta-bar-fill {{
        height: 100%;
        width: 0%;
        background: #58a6ff;
        border-radius: 5px;
        transition: width 0.4s;
    }}
    #stalta-thr-line {{
        position: absolute;
        top: -3px;
        width: 2px;
        height: 16px;
        background: #f97316;
    }}
    #stalta-vals {{
        display: flex;
        justify-content: space-between;
        margin-top: 4px;
        font-size: 11px;
        color: #8b949e;
    }}

    #trigger-badge {{ text-align: center; margin-top: 6px; }}
    .badge {{
        display: inline-block;
        padding: 3px 12px;
        border-radius: 12px;
        font-size: 11px;
        font-weight: bold;
        letter-spacing: 0.5px;
    }}
    .badge.monitoring {{ background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb; }}
    .badge.triggered  {{
        background: #da363333; color: #f85149; border: 1px solid #da3633;
        animation: blink-border 0.5s step-end infinite;
    }}
    @keyframes blink-border {{
        0%, 100% {{ border-color: #da3633; }}
        50%       {{ border-color: transparent; }}
    }}

    #events-panel {{ flex: 1 1 0; overflow: hidden; display: flex; flex-direction: column; }}
    #events-table-wrap {{ overflow-y: auto; flex: 1; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    thead th {{
        color: #8b949e;
        text-align: left;
        padding: 4px 6px;
        border-bottom: 1px solid #21262d;
        font-weight: normal;
        font-size: 11px;
        position: sticky; top: 0;
        background: #161b22;
    }}
    tbody td {{ padding: 5px 6px; border-bottom: 1px solid #21262d20; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    .trig-row-linked:hover {{ background: #1f2d3d; }}
    .scale-cell {{ font-weight: bold; }}
    .scale-0  {{ color: #a0d8ef; }}
    .scale-1  {{ color: #69b7ea; }}
    .scale-2  {{ color: #2196f3; }}
    .scale-3  {{ color: #4caf50; }}
    .scale-4  {{ color: #ffeb3b; }}
    .scale-5w {{ color: #ff9800; }}
    .scale-5s {{ color: #ff6d00; }}
    .scale-6w {{ color: #e53935; font-weight: 900; }}
    .scale-6s {{ color: #b71c1c; font-weight: 900; }}
    .scale-7  {{ color: #7b1fa2; font-weight: 900; }}
    .scale-suffix {{ font-size: 0.6em; vertical-align: middle; font-weight: normal; }}

    /* ===== 中央カラム ===== */
    #col-center {{
        grid-column: 2;
        grid-row: 1;
        display: flex;
        flex-direction: column;
        gap: 8px;
    }}
    #map-panel {{ flex: 1 1 0; }}
    #map {{
        height: 100%;
        min-height: 200px;
        border-radius: 4px;
        overflow: hidden;
    }}
    .map-legend {{
        background: rgba(22,27,34,0.55);
        border: 1px solid #30363d;
        border-radius: 5px;
        padding: 6px 8px;
        font-size: 11px;
        line-height: 1.6;
        pointer-events: none;
    }}
    .map-legend .leg-row {{
        display: flex;
        align-items: center;
        gap: 5px;
    }}
    .map-legend .leg-swatch {{
        width: 12px;
        height: 12px;
        border-radius: 2px;
        flex-shrink: 0;
    }}
    #charts-row {{ flex: 0 0 120px; }}
    #history-panel {{ overflow: hidden; height: 100%; }}
    .chart-wrap {{ position: relative; height: 88px; }}

    /* ===== 右カラム ===== */
    #col-right {{
        grid-column: 3;
        grid-row: 1;
        display: flex;
        flex-direction: column;
        gap: 8px;
    }}
    #p2p-panel {{ flex: 1 1 0; overflow: hidden; display: flex; flex-direction: column; }}
    #p2p-table-wrap {{ overflow-y: auto; flex: 1; }}
    .tsunami-warn {{ color: #f85149; font-size: 10px; margin-left: 2px; }}

    /* ===== レスポンシブ ===== */
    @media (max-width: 900px) {{
        #main {{ grid-template-columns: 1fr; height: auto; }}
        #col-left, #col-center, #col-right {{ grid-column: 1; grid-row: auto; }}
        #map {{ height: 250px; }}
    }}

    /* ===== 設定ボタン ===== */
    #cfg-open-btn {{
        margin-left: auto;
        background: none;
        border: 1px solid #444;
        color: #c9d1d9;
        cursor: pointer;
        font-size: 16px;
        padding: 1px 7px;
        border-radius: 4px;
        line-height: 1;
    }}
    #cfg-open-btn:hover {{ background: #21262d; }}

    /* ===== 設定パネル ===== */
    #cfg-panel {{
        position: fixed;
        top: 0; right: 0;
        width: 300px; height: 100vh;
        background: #161b22;
        border-left: 1px solid #30363d;
        z-index: 1000;
        transform: translateX(100%);
        transition: transform 0.25s ease;
        display: flex;
        flex-direction: column;
    }}
    #cfg-panel.open {{ transform: translateX(0); }}
    #cfg-panel-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 14px;
        border-bottom: 1px solid #30363d;
        font-size: 13px;
        font-weight: bold;
        color: #c9d1d9;
    }}
    #cfg-panel-header button {{
        background: none; border: none;
        color: #8b949e; cursor: pointer; font-size: 16px;
    }}
    #cfg-panel-header button:hover {{ color: #c9d1d9; }}
    #cfg-panel-body {{
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
    }}
    .cfg-row {{
        display: flex;
        flex-direction: column;
        gap: 4px;
    }}
    .cfg-row label {{
        font-size: 11px;
        color: #8b949e;
    }}
    .cfg-row input[type=range] {{
        width: 100%;
        accent-color: #58a6ff;
    }}
    .cfg-row span {{
        font-size: 13px;
        color: #f97316;
        font-weight: bold;
        text-align: right;
    }}
    #cfg-apply-btn {{
        background: #1f6feb;
        color: #fff;
        border: none;
        border-radius: 4px;
        padding: 8px;
        cursor: pointer;
        font-size: 13px;
        font-weight: bold;
    }}
    #cfg-apply-btn:hover {{ background: #388bfd; }}
    #cfg-status {{
        font-size: 11px;
        color: #3fb950;
        text-align: center;
        min-height: 16px;
    }}
</style>
</head>
<body>

<!-- ステータスバー -->
<div id="statusbar">
    <span id="ws-dot"></span>
    <span class="station">{network}.{station}</span>
    <span class="sep">|</span>
    <span><span class="val" id="fs-val">--</span> Hz</span>
    <span class="sep">|</span>
    <span>pkt: <span class="val" id="pkt-val">--</span></span>
    <span class="sep">|</span>
    <span>RS4D DATACAST</span>
    <span class="uptime">稼働 <span id="uptime-val">--:--:--</span> &nbsp;|&nbsp; <span id="clock">--</span></span>
    <button id="cfg-open-btn" title="パラメータ設定" onclick="document.getElementById('cfg-panel').classList.toggle('open')">⚙</button>
</div>

<!-- 情報バナー -->
<div id="info-banner">
    <div id="banner-label">地震情報なし</div>
    <div id="banner-scroll-wrap">
        <div id="banner-ticker">
            <span style="color:#8b949e; font-size:13px;">EEWなし / 地震情報なし</span>
        </div>
    </div>
</div>

<!-- メインレイアウト -->
<div id="main">

    <!-- 左カラム -->
    <div id="col-left">

        <!-- 震度パネル -->
        <div class="panel" id="intensity-panel">
            <div class="panel-title">現地計測震度 <span style="font-weight:normal; letter-spacing:0; text-transform:none; color:#58a6ff;">{network}.{station}</span></div>
            <div id="scale-display">
                <div id="scale-num">－</div>
                <div id="scale-label">現在地点の計測値</div>
            </div>
            <div id="intensity-bar-wrap">
                <div id="intensity-bar-bg">
                    <div id="intensity-bar-fill"></div>
                </div>
            </div>
            <div id="intensity-vals">
                <span id="i-value">I = --</span>
                <span id="a-value">-- gal</span>
            </div>
            <div id="trigger-badge" style="margin-top:10px;">
                <span class="badge monitoring" id="trig-badge">監視中</span>
            </div>
        </div>

        <!-- STA/LTAパネル -->
        <div class="panel" id="stalta-panel">
            <div class="panel-title">STA/LTA 検出</div>
            <div id="stalta-bar-wrap">
                <div id="stalta-bar-bg">
                    <div id="stalta-bar-fill"></div>
                    <div id="stalta-thr-line"></div>
                </div>
            </div>
            <div id="stalta-vals">
                <span>比: <strong id="ratio-val" style="color:#58a6ff;">--</strong></span>
                <span>閾値: <strong style="color:#f97316;">{trig_thr}</strong></span>
                <span>STA:{sta}s / LTA:{lta}s</span>
            </div>
        </div>

        <!-- トリガ履歴 -->
        <div class="panel" id="events-panel">
            <div class="panel-title">トリガ履歴</div>
            <div id="events-table-wrap">
                <table>
                    <thead>
                        <tr><th>時刻</th><th>I値</th><th>震度</th><th>STA/LTA</th></tr>
                    </thead>
                    <tbody id="events-tbody">
                        <tr><td colspan="3" style="color:#8b949e; text-align:center; padding:12px;">なし</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

    </div><!-- /col-left -->

    <!-- 中央カラム -->
    <div id="col-center">

        <!-- 震源地図 -->
        <div class="panel" id="map-panel">
            <div class="panel-title">震源地図 (P2P)</div>
            <div id="map"></div>
        </div>

        <!-- 推移グラフ -->
        <div id="charts-row">
            <div class="panel" id="history-panel">
                <div class="panel-title">I値・STA/LTA推移 (直近5分)</div>
                <div class="chart-wrap">
                    <canvas id="historyChart"></canvas>
                </div>
            </div>
        </div>

    </div><!-- /col-center -->

    <!-- 右カラム -->
    <div id="col-right">

        <!-- P2P地震情報 -->
        <div class="panel" id="p2p-panel">
            <div class="panel-title">最新地震情報 (P2P)</div>
            <div id="p2p-table-wrap">
                <table>
                    <thead>
                        <tr><th>発生時刻</th><th>震源</th><th>M</th><th>深さ</th><th>距離</th><th>震度</th><th></th></tr>
                    </thead>
                    <tbody id="p2p-tbody">
                        <tr><td colspan="7" style="color:#8b949e; text-align:center; padding:12px;">取得中...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

    </div><!-- /col-right -->

</div><!-- /main -->

<!-- 設定パネル -->
<div id="cfg-panel">
    <div id="cfg-panel-header">
        <span>パラメータ設定</span>
        <button onclick="document.getElementById('cfg-panel').classList.remove('open')">✕</button>
    </div>
    <div id="cfg-panel-body">
        <div class="cfg-row">
            <label>STA 窓長 [秒]</label>
            <input type="range" id="cfg-sta" min="0.2" max="5" step="0.1" value="{sta}"
                oninput="document.getElementById('cfg-sta-val').textContent=this.value">
            <span id="cfg-sta-val">{sta}</span>
        </div>
        <div class="cfg-row">
            <label>LTA 窓長 [秒]</label>
            <input type="range" id="cfg-lta" min="5" max="60" step="1" value="{lta}"
                oninput="document.getElementById('cfg-lta-val').textContent=this.value">
            <span id="cfg-lta-val">{lta}</span>
        </div>
        <div class="cfg-row">
            <label>トリガ閾値 (STA/LTA)</label>
            <input type="range" id="cfg-trig" min="1.0" max="10.0" step="0.1" value="{trig_thr}"
                oninput="document.getElementById('cfg-trig-val').textContent=this.value">
            <span id="cfg-trig-val">{trig_thr}</span>
        </div>
        <div class="cfg-row">
            <label>再検出抑制 [秒]</label>
            <input type="range" id="cfg-det-hold" min="5" max="120" step="5" value="{det_hold}"
                oninput="document.getElementById('cfg-det-hold-val').textContent=this.value">
            <span id="cfg-det-hold-val">{det_hold}</span>
        </div>
        <button id="cfg-apply-btn" onclick="applyConfig()">適用</button>
        <div id="cfg-status"></div>
    </div>
</div>

<script>
let TRIG_THR = {trig_thr};

// ===== スケール定数 =====
// 気象庁震度着色規則に準拠
const SCALE_COLORS = {{
    '0':  '#a0d8ef',  // 水色（感じない）
    '1':  '#69b7ea',  // 淡青
    '2':  '#2196f3',  // 青
    '3':  '#4caf50',  // 緑
    '4':  '#ffeb3b',  // 黄
    '5弱':'#ff9800',  // オレンジ
    '5強':'#ff6d00',  // 濃オレンジ
    '6弱':'#e53935',  // 赤
    '6強':'#b71c1c',  // 深赤
    '7':  '#7b1fa2',  // 紫
}};

function scaleClass(scale) {{
    const map = {{'0':'scale-0','1':'scale-1','2':'scale-2','3':'scale-3','4':'scale-4',
        '5弱':'scale-5w','5強':'scale-5s','6弱':'scale-6w','6強':'scale-6s','7':'scale-7'}};
    return map[scale] || 'scale-0';
}}

// ===== HTMLエスケープ =====
function esc(s) {{
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}}

// ===== 全角変換 =====
function toFullWidth(s) {{
    return String(s).replace(/[0-9]/g, c => String.fromCharCode(c.charCodeAt(0) + 0xFEE0));
}}

function formatScale(scale) {{
    if (!scale || scale === '--') return '－';
    const num = scale.replace(/[弱強]/, '');
    const suf = scale.match(/[弱強]/) ? scale.match(/[弱強]/)[0] : '';
    const fw = toFullWidth(num);
    if (suf) {{
        return fw + '<span class="scale-suffix">' + suf + '</span>';
    }}
    return fw;
}}

// ===== 時計 =====
function updateClock() {{
    const now = new Date();
    const pad = n => String(n).padStart(2,'0');
    document.getElementById('clock').textContent =
        now.getFullYear() + '-' + pad(now.getMonth()+1) + '-' + pad(now.getDate()) + ' ' +
        pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
}}
setInterval(updateClock, 1000);
updateClock();

// ===== 稼働時間 =====
let _startTime = null;
function updateUptime() {{
    if (_startTime === null) return;
    const sec = Math.floor(Date.now() / 1000 - _startTime);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = n => String(n).padStart(2,'0');
    document.getElementById('uptime-val').textContent = pad(h) + ':' + pad(m) + ':' + pad(s);
}}
setInterval(updateUptime, 1000);

// ===== 推移グラフ =====
const N_HIST = 600;
const histLabels = Array.from({{length: N_HIST}}, (_, i) => {{
    const sec = (N_HIST - i) * 1;
    return sec % 60 === 0 ? '-' + Math.floor(sec/60) + 'm' : '';
}});

const histCtx = document.getElementById('historyChart').getContext('2d');
const THR_DATA = Array(N_HIST).fill(TRIG_THR);
const historyChart = new Chart(histCtx, {{
    type: 'line',
    data: {{
        labels: histLabels,
        datasets: [
            {{
                label: 'I値',
                data: Array(N_HIST).fill(0),
                borderColor: '#f97316',
                backgroundColor: 'rgba(249,115,22,0.08)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true,
                tension: 0.4,
                yAxisID: 'yI',
            }},
            {{
                label: 'STA/LTA',
                data: Array(N_HIST).fill(0),
                borderColor: '#58a6ff',
                borderWidth: 1.5,
                pointRadius: 0,
                fill: false,
                tension: 0.4,
                yAxisID: 'yR',
            }},
            {{
                label: 'thr=' + TRIG_THR,
                data: THR_DATA,
                borderColor: '#f97316',
                borderWidth: 1,
                borderDash: [4, 4],
                pointRadius: 0,
                fill: false,
                tension: 0,
                yAxisID: 'yR',
            }},
        ]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {{
            legend: {{
                position: 'top',
                labels: {{ color: '#8b949e', boxWidth: 12, font: {{ size: 11 }} }}
            }},
            tooltip: {{ mode: 'index', intersect: false }},
        }},
        scales: {{
            x: {{
                ticks: {{ color: '#8b949e', font: {{ size: 10 }}, maxRotation: 0 }},
                grid: {{ color: '#21262d' }},
            }},
            yI: {{
                type: 'linear',
                position: 'left',
                min: 0, max: 7,
                ticks: {{ color: '#f97316', font: {{ size: 10 }} }},
                grid: {{ color: '#21262d' }},
                title: {{ display: true, text: 'I値', color: '#f97316', font: {{ size: 10 }} }},
            }},
            yR: {{
                type: 'linear',
                position: 'right',
                min: 0,
                ticks: {{ color: '#58a6ff', font: {{ size: 10 }} }},
                grid: {{ drawOnChartArea: false }},
                title: {{ display: true, text: 'STA/LTA', color: '#58a6ff', font: {{ size: 10 }} }},
            }}
        }}
    }}
}});

// ===== 震源地図 =====
const quakeMap = L.map('map', {{ zoomControl: true, attributionControl: false }}).setView([36.5, 137.5], 5);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    maxZoom: 19,
    subdomains: 'abcd',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
}}).addTo(quakeMap);
let _mapMarkers = [];
let _mapQuakeKey = null;
let _cityLayer = null;

// 震度凡例コントロール
(function() {{
    const legend = L.control({{ position: 'bottomright' }});
    legend.onAdd = () => {{
        const div = L.DomUtil.create('div', 'map-legend');
        const items = [
            ['7',  '#7b1fa2', '震度７'],
            ['6強','#b71c1c', '震度６強'],
            ['6弱','#e53935', '震度６弱'],
            ['5強','#ff6d00', '震度５強'],
            ['5弱','#ff9800', '震度５弱'],
            ['4',  '#ffeb3b', '震度４'],
            ['3',  '#4caf50', '震度３'],
            ['2',  '#2196f3', '震度２'],
            ['1',  '#69b7ea', '震度１'],
            ['0',  '#a0d8ef', '震度０'],
        ];
        div.innerHTML = items.map(([, col, label]) =>
            `<div class="leg-row"><span class="leg-swatch" style="background:${{col}}"></span><span style="color:#e6edf3">${{label}}</span></div>`
        ).join('');
        return div;
    }};
    legend.addTo(quakeMap);
}})();    // 市区町村震度塗りつぶしレイヤー
let _activeQuake = null;  // 震源クリックで選択中の地震
let _latestP2pQuakes = []; // 最新のP2P地震リスト（トリガ履歴クリック照合用）
let _userLat = null, _userLng = null; // ブラウザ位置情報
let _userMarker = null; // 現在地ピン
const _cityGeoCache = {{}}; // 都道府県コード → GeoJSONデータ のキャッシュ

// ===== 位置情報・距離計算 =====
(function initGeolocation() {{
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(pos => {{
        _userLat = pos.coords.latitude;
        _userLng = pos.coords.longitude;

        // 現在地ピン（青い丸）
        const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 20 20'>
            <circle cx='10' cy='10' r='7' fill='#1f6feb' stroke='#fff' stroke-width='2.5'/>
            <circle cx='10' cy='10' r='2.5' fill='#fff'/>
        </svg>`;
        const icon = L.divIcon({{
            html: svg,
            className: '',
            iconSize: [20, 20],
            iconAnchor: [10, 10],
            popupAnchor: [0, -12],
        }});
        if (_userMarker) quakeMap.removeLayer(_userMarker);
        _userMarker = L.marker([_userLat, _userLng], {{ icon, zIndexOffset: 2000 }})
            .bindPopup('現在地', {{closeButton: false}})
            .addTo(quakeMap);

        // 取得完了後、既表示中のリスト・地図を距離付きで再描画
        if (_latestP2pQuakes.length > 0) {{
            updateP2PTable(_latestP2pQuakes);
            _mapQuakeKey = '';  // 強制再描画
            updateMap(_latestP2pQuakes);
        }}
    }}, () => {{}}, {{ enableHighAccuracy: false, timeout: 10000 }});
}})();

function haversineKm(lat1, lng1, lat2, lng2) {{
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat/2)**2 +
              Math.cos(lat1 * Math.PI/180) * Math.cos(lat2 * Math.PI/180) * Math.sin(dLng/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}}

function distLabel(lat, lng) {{
    if (_userLat == null || lat == null || lng == null) return '--';
    const km = haversineKm(_userLat, _userLng, lat, lng);
    return km < 100 ? km.toFixed(0) + 'km' : Math.round(km) + 'km';
}}

const CITY_SHOW_ZOOM = 7;  // このズーム以上で市区町村塗りを表示

// 都道府県名 → 国土数値情報ファイルコード（2桁）
const PREF_CODE = {{
    '北海道':'01','青森県':'02','岩手県':'03','宮城県':'04','秋田県':'05',
    '山形県':'06','福島県':'07','茨城県':'08','栃木県':'09','群馬県':'10',
    '埼玉県':'11','千葉県':'12','東京都':'13','神奈川県':'14','新潟県':'15',
    '富山県':'16','石川県':'17','福井県':'18','山梨県':'19','長野県':'20',
    '岐阜県':'21','静岡県':'22','愛知県':'23','三重県':'24','滋賀県':'25',
    '京都府':'26','大阪府':'27','兵庫県':'28','奈良県':'29','和歌山県':'30',
    '鳥取県':'31','島根県':'32','岡山県':'33','広島県':'34','山口県':'35',
    '徳島県':'36','香川県':'37','愛媛県':'38','高知県':'39','福岡県':'40',
    '佐賀県':'41','長崎県':'42','熊本県':'43','大分県':'44','宮崎県':'45',
    '鹿児島県':'46','沖縄県':'47'
}};
const CITY_BASE = 'https://raw.githubusercontent.com/smartnews-smri/japan-topography/main/data/municipality/geojson/s0010/';

// 震度の数値変換（大小比較用）
const toScaleNum = s => {{
    if (!s) return -1;
    const n = parseFloat(String(s).replace('弱','').replace('強',''));
    if (isNaN(n)) return -1;
    return n + (String(s).includes('強') ? 0.5 : 0);
}};

// ×印SVGアイコン（震源マーカー用）
function crossIcon(color, size) {{
    const s = size || 20;
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='${{s}}' height='${{s}}' viewBox='0 0 20 20'>
        <line x1='2' y1='2' x2='18' y2='18' stroke='${{color}}' stroke-width='3.5' stroke-linecap='round'/>
        <line x1='18' y1='2' x2='2' y2='18' stroke='${{color}}' stroke-width='3.5' stroke-linecap='round'/>
    </svg>`;
    return L.divIcon({{
        html: svg,
        className: '',
        iconSize: [s, s],
        iconAnchor: [s/2, s/2],
        popupAnchor: [0, -s/2],
    }});
}}

// 都道府県コードのGeoJSONを取得（キャッシュあり）
function fetchCityGeoJson(code) {{
    if (_cityGeoCache[code]) return Promise.resolve(_cityGeoCache[code]);
    const url = CITY_BASE + 'N03-21_' + code + '_210101.json';
    return fetch(url)
        .then(r => r.json())
        .then(data => {{ _cityGeoCache[code] = data; return data; }});
}}

// 市区町村震度レイヤーを描画
function drawCityLayer(quake) {{
    if (_cityLayer) {{ quakeMap.removeLayer(_cityLayer); _cityLayer = null; }}
    if (!quake || !quake.points || quake.points.length === 0) return;

    // 市区町村名 → 最大震度 のマップ（pref付きキーで衝突防止）
    const cityScale = {{}};  // "pref::市区町村" → scale
    quake.points.forEach(p => {{
        if (!p.pref || !p.addr) return;
        // addr から市区町村名を抽出（「八戸市湊町」→「八戸市」「湊町」の前半）
        const city = extractCity(p.addr);
        const key = p.pref + '::' + city;
        const cur = cityScale[key];
        if (!cur || toScaleNum(p.scale) > toScaleNum(cur)) {{
            cityScale[key] = p.scale;
        }}
    }});

    // 必要な都道府県コードを特定してGeoJSONを並列取得
    const prefsNeeded = [...new Set(quake.points.map(p => p.pref).filter(Boolean))];
    const fetches = prefsNeeded
        .map(pref => {{ const code = PREF_CODE[pref]; return code ? fetchCityGeoJson(code).then(d => ({{pref, data:d}})) : null; }})
        .filter(Boolean);

    Promise.all(fetches).then(results => {{
        // 全都道府県のfeatureを結合
        const allFeatures = [];
        results.forEach(({{pref, data}}) => {{
            (data.features || []).forEach(f => {{
                f._pref = pref;  // 後でマッチに使う
                allFeatures.push(f);
            }});
        }});

        const merged = {{ type: 'FeatureCollection', features: allFeatures }};

        _cityLayer = L.geoJSON(merged, {{
            style: feature => {{
                const pref = feature._pref || '';
                const cityName = feature.properties.N03_004 || '';
                const key = pref + '::' + cityName;
                const scale = cityScale[key];
                if (!scale) {{
                    return {{ fillColor: 'transparent', fillOpacity: 0, color: '#555', weight: 0.4, opacity: 0.25 }};
                }}
                const col = SCALE_COLORS[scale] || '#8b949e';
                return {{ fillColor: col, fillOpacity: 0.5, color: '#fff', weight: 0.8, opacity: 0.7 }};
            }},
            onEachFeature: (feature, layer) => {{
                const pref = feature._pref || '';
                const cityName = feature.properties.N03_004 || '';
                const key = pref + '::' + cityName;
                const scale = cityScale[key];
                if (scale) {{
                    layer.bindPopup(
                        '<b>' + cityName + '</b>（' + pref + '）<br>震度 ' + scale,
                        {{closeButton: false}}
                    );
                    layer.on('click', e => {{ layer.openPopup(); L.DomEvent.stopPropagation(e); }});
                }}
            }}
        }});

        if (quakeMap.getZoom() >= CITY_SHOW_ZOOM) {{
            _cityLayer.addTo(quakeMap);
        }}
    }}).catch(() => {{}});
}}

// addr文字列から市区町村名を抽出
// 例: 「八戸市湊町」→「八戸市」、「那覇市」→「那覇市」、「本島北部」→「本島北部」
function extractCity(addr) {{
    if (!addr) return addr;
    // 市・区・町・村で終わる最長前方一致を探す
    const m = addr.match(/^(.+?[市区町村郡])/);
    return m ? m[1] : addr;
}}

// ズーム変化に応じてレイヤーの表示/非表示を切替
quakeMap.on('zoomend', () => {{
    if (!_cityLayer) return;
    if (quakeMap.getZoom() >= CITY_SHOW_ZOOM) {{
        if (!quakeMap.hasLayer(_cityLayer)) _cityLayer.addTo(quakeMap);
    }} else {{
        if (quakeMap.hasLayer(_cityLayer)) quakeMap.removeLayer(_cityLayer);
    }}
}});

function updateMap(quakes) {{
    // IDリストが変わっていなければ再描画しない
    const key = (quakes || []).map(q => q.id || q.time || '').join(',');
    if (key === _mapQuakeKey) return;
    _mapQuakeKey = key;

    // 既存マーカーと市区町村レイヤーをクリア
    _mapMarkers.forEach(m => quakeMap.removeLayer(m));
    _mapMarkers = [];
    if (_cityLayer) {{ quakeMap.removeLayer(_cityLayer); _cityLayer = null; }}
    _activeQuake = null;
    // 新しい地震リストが来たらピン固定を解除
    _pinnedQuake = null;
    _bannerKey = null;

    if (!quakes || quakes.length === 0) return;

    quakes.forEach((q, i) => {{
        const lat = q.latitude;
        const lng = q.longitude;
        if (lat == null || lng == null) return;
        const mag = parseFloat(q.magnitude) || 0;
        const iconSize = Math.round(Math.max(8, Math.min(22, mag * 2.8)));
        const marker = L.marker([lat, lng], {{
            icon: crossIcon('#e53935', iconSize),
            zIndexOffset: i === 0 ? 1000 : 0,
        }}).addTo(quakeMap);
        const depth = q.depth != null ? q.depth + 'km' : '不明';
        const dist = distLabel(lat, lng);
        const distStr = dist !== '--' ? '<br>震源まで ' + dist : '';
        marker.bindPopup(
            '<b>' + q.name + '</b><br>' +
            'M' + mag.toFixed(1) + '　深さ ' + depth + '<br>' +
            '最大震度 ' + q.max_scale + distStr
        );
        // クリックで震源周辺にズームし市区町村震度を表示、バナーをこの地震に固定
        marker.on('click', () => {{
            _activeQuake = q;
            _pinnedQuake = q;
            _bannerKey = null;  // 強制再描画のためキーをリセット
            quakeMap.setView([lat, lng], Math.max(quakeMap.getZoom(), CITY_SHOW_ZOOM), {{animate: true}});
            drawCityLayer(q);
            renderBannerForQuake(q, null, true);
        }});
        _mapMarkers.push(marker);
    }});

    // 最新地震のポップアップを開く
    if (_mapMarkers.length > 0) _mapMarkers[0].openPopup();
}}

// ===== バナー =====
let _bannerKey = null;      // 前回描画したコンテンツの識別キー
let _pinnedQuake = null;    // 震源クリックで固定した地震（null=最新地震を自動表示）

function renderBannerForQuake(quake, eew, force) {{
    const banner = document.getElementById('info-banner');
    const label = document.getElementById('banner-label');
    const ticker = document.getElementById('banner-ticker');

    let key, newLabel, newHtml, isEew = false, newClass = '';

    if (eew) {{
        key = 'eew:' + (eew.time || '') + (eew.name || '');
        isEew = true;
        newLabel = '⚡ EEW';
        const mag = eew.magnitude >= 0 ? 'M' + parseFloat(eew.magnitude).toFixed(1) : 'M不明';
        newHtml = eew.name + ' ' + mag + ' 最大予測震度' + eew.max_scale + ' ' + eew.time + ' ※P2P経由・無保証';
        newClass = 'eew-mode';
    }} else if (quake && quake.points && quake.points.length > 0) {{
        const pinned = _pinnedQuake && _pinnedQuake.id === quake.id;
        key = (pinned ? 'pinned:' : 'quake:') + (quake.id || quake.time || '');
        newLabel = (pinned ? '📍 ' : '') + (quake.time || '').replace('T', ' ').slice(5, 16) +
                   ' ' + quake.name + ' M' + parseFloat(quake.magnitude||0).toFixed(1);
        const items = quake.points.map(p => {{
            const col = SCALE_COLORS[p.scale] || '#e6edf3';
            return '<span class="ticker-item">' +
                '<span class="ticker-pref">' + esc(p.pref) + '</span>' +
                '<span class="ticker-place">' + esc(p.addr) + '</span>' +
                '<span class="ticker-scale" style="color:' + col + ';">震度' + esc(p.scale) + '</span>' +
                '</span>';
        }}).join('');
        newHtml = items;
    }} else {{
        key = 'none';
        newLabel = '地震情報なし';
        newHtml = '<span style="color:#8b949e; font-size:13px;">EEWなし / 地震情報なし</span>';
    }}

    if (!force && key === _bannerKey) return;  // 内容が変わっていなければ触らない
    _bannerKey = key;

    // EEWクラス切替
    if (isEew) {{
        banner.classList.add('eew-active');
        label.classList.add('eew');
    }} else {{
        banner.classList.remove('eew-active');
        label.classList.remove('eew');
    }}
    label.textContent = newLabel;

    // アニメーションをリセットしてから更新
    ticker.style.animation = 'none';
    ticker.className = newClass;
    ticker.id = 'banner-ticker';
    if (newClass === 'eew-mode') {{
        ticker.textContent = newHtml;
    }} else {{
        ticker.innerHTML = newHtml;
    }}
    // 次フレームでアニメーション再開（スムーズに右端から始まる）
    requestAnimationFrame(() => {{
        if (newClass !== 'eew-mode') {{
            // テキスト量に応じて速度調整: 1文字あたり0.15秒、最短60秒・最長180秒
            const chars = ticker.textContent.length;
            const dur = Math.min(180, Math.max(60, Math.round(chars * 0.15)));
            ticker.style.animation = 'ticker-scroll ' + dur + 's linear infinite';
        }}
    }});
}}

// 通常のupdateBanner: EEW優先、固定地震があればそれを、なければ最新地震を表示
function updateBanner(quakes, eew) {{
    if (eew) {{
        renderBannerForQuake(null, eew, false);
        return;
    }}
    const quake = _pinnedQuake || (quakes && quakes.length > 0 ? quakes[0] : null);
    renderBannerForQuake(quake, null, false);
}}

// ===== P2Pテーブル =====
function _onP2PRowClick(idx) {{
    const q = _latestP2pQuakes[idx];
    if (!q) return;
    const lat = q.latitude, lng = q.longitude;
    if (lat == null || lng == null) return;
    _activeQuake = q;
    _pinnedQuake = q;
    _bannerKey = null;
    quakeMap.setView([lat, lng], Math.max(quakeMap.getZoom(), CITY_SHOW_ZOOM), {{animate: true}});
    drawCityLayer(q);
    renderBannerForQuake(q, null, true);
}}

function updateP2PTable(quakes) {{
    const tbody = document.getElementById('p2p-tbody');
    if (!quakes || quakes.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e; text-align:center; padding:12px;">取得中...</td></tr>';
        return;
    }}
    tbody.innerHTML = quakes.map((q, idx) => {{
        const sc = q.max_scale || '?';
        const cls = scaleClass(sc);
        const tsunami = (q.tsunami && q.tsunami !== 'None' && q.tsunami !== 'Unknown')
            ? '<span class="tsunami-warn">津波</span>' : '';
        const time = esc((q.time || '').replace('T', ' ').slice(5, 16));
        const dist = esc(distLabel(q.latitude, q.longitude));
        const clickable = q.latitude != null && q.longitude != null;
        const rowAttr = clickable
            ? ' class="trig-row-linked" style="cursor:pointer;" onclick="_onP2PRowClick(' + idx + ')"'
            : '';
        const analyzeBtn = q.time
            ? '<button onclick="event.stopPropagation();_startAnalyze(' + idx + ')" ' +
              'style="font-size:10px;padding:2px 6px;background:#21262d;border:1px solid #30363d;' +
              'color:#58a6ff;border-radius:4px;cursor:pointer;">解析</button>'
            : '';
        return '<tr' + rowAttr + '>' +
            '<td style="color:#8b949e;">' + time + '</td>' +
            '<td>' + esc(q.name || '不明') + '</td>' +
            '<td style="color:#58a6ff;">' + parseFloat(q.magnitude || 0).toFixed(1) + '</td>' +
            '<td style="color:#8b949e;">' + esc(String(q.depth || '--')) + 'km</td>' +
            '<td style="color:#8b949e;">' + dist + '</td>' +
            '<td class="scale-cell ' + cls + '">' + formatScale(sc) + tsunami + '</td>' +
            '<td style="text-align:center;">' + analyzeBtn + '</td>' +
            '</tr>';
    }}).join('');
}}

// ===== P2P行 解析機能 =====
function _startAnalyze(idx) {{
    const q = _latestP2pQuakes[idx];
    if (!q || !q.time) return;
    _showAnalyzeModal('running', null, null);
    fetch('/api/analyze', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{time: q.time, duration: 420}})
    }})
    .then(r => r.json())
    .then(data => {{
        if (!data.job_id) {{
            _showAnalyzeModal('error', null, data.error || '起動失敗');
            return;
        }}
        _pollAnalyze(data.job_id, 0);
    }})
    .catch(e => _showAnalyzeModal('error', null, String(e)));
}}

function _pollAnalyze(jobId, count) {{
    if (count > 120) {{
        _showAnalyzeModal('error', null, 'タイムアウト（120秒）');
        return;
    }}
    fetch('/api/analyze/' + jobId)
    .then(r => r.json())
    .then(data => {{
        if (data.status === 'done') {{
            _showAnalyzeModal('done', '/api/analyze_img/' + jobId, null);
        }} else if (data.status === 'error') {{
            _showAnalyzeModal('error', null, data.error || '解析エラー');
        }} else {{
            setTimeout(() => _pollAnalyze(jobId, count + 1), 1000);
        }}
    }})
    .catch(e => _showAnalyzeModal('error', null, String(e)));
}}

function _showAnalyzeModal(status, imgUrl, errorMsg) {{
    let modal = document.getElementById('analyze-modal');
    if (!modal) {{
        modal = document.createElement('div');
        modal.id = 'analyze-modal';
        modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;' +
            'background:rgba(0,0,0,0.75);z-index:9999;display:flex;align-items:center;' +
            'justify-content:center;';
        modal.onclick = e => {{ if (e.target === modal) modal.remove(); }};
        document.body.appendChild(modal);
    }}
    let inner = '';
    if (status === 'running') {{
        inner = '<div style="color:#e6edf3;font-size:14px;">解析中... (～30秒かかります)</div>';
    }} else if (status === 'done') {{
        inner = '<img src="' + imgUrl + '?t=' + Date.now() + '" style="max-width:95vw;max-height:90vh;border-radius:8px;" />';
    }} else {{
        inner = '<div style="color:#f85149;font-size:13px;">エラー: ' + esc(errorMsg || '不明') + '</div>';
    }}
    modal.innerHTML = '<div class="analyze-modal-wrap" style="background:#161b22;border:1px solid #30363d;border-radius:8px;' +
        'padding:16px;max-width:95vw;max-height:92vh;overflow:auto;position:relative;">' +
        '<button onclick="document.getElementById(&apos;analyze-modal&apos;).remove()" ' +
        'style="position:absolute;top:8px;right:8px;background:transparent;border:none;' +
        'color:#8b949e;font-size:16px;cursor:pointer;">✕</button>' +
        inner + '</div>';
}}

// ===== トリガ履歴テーブル =====

// "HH:MM:SS" または "YYYY-MM-DD HH:MM:SS" を分単位epochに変換
function _trigTsToMin(ts) {{
    if (!ts) return NaN;
    // 日付付き形式（ログ復元時）
    if (ts.length > 8) {{
        const dt = new Date(ts.replace(' ', 'T') + '+09:00');
        return isNaN(dt) ? NaN : dt.getTime() / 60000;
    }}
    // 時刻のみ形式（当日発生分）
    const now = new Date();
    const [h, m] = ts.split(':').map(Number);
    let d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0, 0);
    // 翌0時をまたいだ場合（例：23:59のトリガが0:00以降に表示）
    if (d - now > 12 * 3600 * 1000) d = new Date(d.getTime() - 86400000);
    if (now - d > 12 * 3600 * 1000) d = new Date(d.getTime() + 86400000);
    return d.getTime() / 60000;
}}

// "YYYY-MM-DD HH:MM" を分単位epochに変換
function _quakeTsToMin(t) {{
    if (!t) return NaN;
    const dt = new Date(t.replace(' ', 'T') + ':00+09:00');
    return dt.getTime() / 60000;
}}

// P2P地震リストから最も近い地震を探す（±10分以内）
function _findMatchingQuake(trigTs) {{
    const trigMin = _trigTsToMin(trigTs);
    let best = null, bestDiff = Infinity;
    for (const q of _latestP2pQuakes) {{
        const qMin = _quakeTsToMin(q.time);
        if (isNaN(qMin)) continue;
        const diff = Math.abs(trigMin - qMin);
        if (diff <= 10 && diff < bestDiff) {{
            best = q;
            bestDiff = diff;
        }}
    }}
    return best;
}}

function _onTrigRowClick(trigTs) {{
    const q = _findMatchingQuake(trigTs);
    if (!q) return;
    const lat = q.latitude, lng = q.longitude;
    if (lat == null || lng == null) return;
    _activeQuake = q;
    _pinnedQuake = q;
    _bannerKey = null;
    quakeMap.setView([lat, lng], Math.max(quakeMap.getZoom(), CITY_SHOW_ZOOM), {{animate: true}});
    drawCityLayer(q);
    renderBannerForQuake(q, null, true);
}}

function updateEventsTable(events) {{
    const tbody = document.getElementById('events-tbody');
    if (!events || events.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="4" style="color:#8b949e; text-align:center; padding:12px;">なし</td></tr>';
        return;
    }}
    const rows = [...events].reverse().map(e => {{
        const [ts, I, scale, ratio] = e;
        const cls = scaleClass(scale);
        const dispTs = ts.length > 8 ? ts.slice(0, 10) + ' ' + ts.slice(11, 19) : ts;
        const matched = _findMatchingQuake(ts) !== null;
        const rowStyle = matched
            ? 'cursor:pointer; transition:background 0.15s;'
            : 'color:#555;';
        const tsStyle = matched ? 'color:#8b949e;' : 'color:#555;';
        const iStyle  = matched ? 'color:#58a6ff;' : 'color:#555;';
        const ratioStr = (ratio != null && ratio > 0) ? parseFloat(ratio).toFixed(2) : '--';
        const onClick = matched ? ` onclick="_onTrigRowClick(${{JSON.stringify(ts)}})"`  : '';
        const hoverAttr = matched ? ' class="trig-row-linked"' : '';
        return '<tr' + hoverAttr + ' style="' + rowStyle + '"' + onClick + '>' +
            '<td style="' + tsStyle + '">' + dispTs + (matched ? ' 🔗' : '') + '</td>' +
            '<td style="' + iStyle + '">' + parseFloat(I).toFixed(2) + '</td>' +
            '<td class="scale-cell ' + cls + '">' + formatScale(scale) + '</td>' +
            '<td style="color:#f97316; text-align:right;">' + ratioStr + '</td>' +
            '</tr>';
    }}).join('');
    tbody.innerHTML = rows;
}}

// ===== 震度表示更新 =====
function updateIntensity(data) {{
    const scale = data.scale || '0';
    const I = data.I_final || 0;
    const a = data.a_gal || 0;
    const color = SCALE_COLORS[scale] || '#3fb950';

    const scaleNum = document.getElementById('scale-num');
    scaleNum.innerHTML = formatScale(scale);
    scaleNum.style.color = color;
    scaleNum.style.textShadow = '0 0 40px ' + color + '80';

    document.getElementById('i-value').textContent = 'I = ' + I.toFixed(2);
    document.getElementById('a-value').textContent = a.toFixed(3) + ' gal';

    const barFill = document.getElementById('intensity-bar-fill');
    barFill.style.width = (Math.min(Math.max(I, 0), 7) / 7.0 * 100).toFixed(1) + '%';

    const badge = document.getElementById('trig-badge');
    if (data.triggered) {{
        badge.className = 'badge triggered';
        badge.textContent = '⚠ トリガ検出';
    }} else {{
        badge.className = 'badge monitoring';
        badge.textContent = '監視中';
    }}
}}

// ===== STA/LTA表示更新 =====
function updateSTALTA(data) {{
    const ratio = data.ratio || 0;
    const maxDisp = Math.max(TRIG_THR * 2, 5.0);
    const pct = Math.min(ratio / maxDisp * 100, 100).toFixed(1);
    const thrPct = (TRIG_THR / maxDisp * 100).toFixed(1);

    document.getElementById('stalta-bar-fill').style.width = pct + '%';
    document.getElementById('stalta-bar-fill').style.background = ratio >= TRIG_THR ? '#f85149' : '#58a6ff';
    document.getElementById('stalta-thr-line').style.left = thrPct + '%';
    document.getElementById('ratio-val').textContent = ratio.toFixed(2);
}}

// ===== グラフ更新 =====
let _chartPending = false;
let _chartNextI = null;
let _chartNextR = null;

function updateChart(iHistory, ratioHistory) {{
    _chartNextI = iHistory || [];
    _chartNextR = ratioHistory || [];
    if (_chartPending) return;  // 既にrAFがスケジュール済みならスキップ
    _chartPending = true;
    requestAnimationFrame(() => {{
        const iData = _chartNextI;
        const rData = _chartNextR;
        const padI = Array(Math.max(0, N_HIST - iData.length)).fill(0).concat(iData).slice(-N_HIST);
        const padR = Array(Math.max(0, N_HIST - rData.length)).fill(0).concat(rData).slice(-N_HIST);
        historyChart.data.datasets[0].data = padI;
        historyChart.data.datasets[1].data = padR;
        historyChart.update('none');
        _chartPending = false;
    }});
}}

// ===== 全体更新 =====
let _pendingData = null;
let _dashPending = false;

function updateDashboard(data) {{
    _pendingData = data;
    if (_dashPending) return;  // 既にrAFがスケジュール済み → 最新データで1回だけ描画
    _dashPending = true;
    requestAnimationFrame(() => {{
        const d = _pendingData;
        _dashPending = false;

        if (_startTime === null && d.start_time) {{
            _startTime = d.start_time;
        }}

        document.getElementById('fs-val').textContent = (d.fs || 0).toFixed(1);
        document.getElementById('pkt-val').textContent = (d.pkt_count || 0).toLocaleString();

        // サーバーの実動作パラメータを同期
        if (d.config) {{
            const cfg = d.config;
            if (cfg.trig !== TRIG_THR) {{
                TRIG_THR = cfg.trig;
                // グラフ閾値線
                const thrDataset = historyChart.data.datasets[2];
                thrDataset.data = Array(N_HIST).fill(cfg.trig);
                thrDataset.label = 'thr=' + cfg.trig;
                // STA/LTAバー閾値線
                const maxDisp = Math.max(cfg.trig * 2, 5.0);
                document.getElementById('stalta-thr-line').style.left = (cfg.trig / maxDisp * 100).toFixed(1) + '%';
                // STA/LTAパネルの数値表示
                const thrEl = document.querySelector('#stalta-vals strong[style*="f97316"]');
                if (thrEl) thrEl.textContent = cfg.trig;
            }}
            // 設定パネルが閉じているときだけスライダーを実動作値に同期
            if (!document.getElementById('cfg-panel').classList.contains('open')) {{
                const staEl = document.getElementById('cfg-sta');
                if (staEl) {{ staEl.value = cfg.sta; document.getElementById('cfg-sta-val').textContent = cfg.sta; }}
                const ltaEl = document.getElementById('cfg-lta');
                if (ltaEl) {{ ltaEl.value = cfg.lta; document.getElementById('cfg-lta-val').textContent = cfg.lta; }}
                const trigEl = document.getElementById('cfg-trig');
                if (trigEl) {{ trigEl.value = cfg.trig; document.getElementById('cfg-trig-val').textContent = cfg.trig; }}
                const detEl = document.getElementById('cfg-det-hold');
                if (detEl) {{ detEl.value = cfg.det_hold; document.getElementById('cfg-det-hold-val').textContent = cfg.det_hold; }}
            }}
            const staLtaEl = document.querySelector('#stalta-vals span:last-child');
            if (staLtaEl) staLtaEl.textContent = 'STA:' + cfg.sta + 's / LTA:' + cfg.lta + 's';
        }}

        _latestP2pQuakes = d.p2p_quakes || [];
        updateIntensity(d);
        updateSTALTA(d);
        updateChart(d.i_history, d.ratio_history);
        updateEventsTable(d.events);
        updateP2PTable(d.p2p_quakes);
        updateMap(d.p2p_quakes);
        updateBanner(d.p2p_quakes, d.p2p_eew);
    }});
}}

// ===== WebSocket接続 =====
let _ws = null;
let _reconnectTimer = null;
let _userClosed = false;  // visibilitychange による意図的切断かどうか

function connect() {{
    if (_reconnectTimer) {{ clearTimeout(_reconnectTimer); _reconnectTimer = null; }}
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;

    _userClosed = false;
    const ws = new WebSocket('ws://' + location.host + '/ws');
    _ws = ws;

    ws.onopen = () => {{
        document.getElementById('ws-dot').classList.add('connected');
    }};
    ws.onmessage = (e) => {{
        try {{
            updateDashboard(JSON.parse(e.data));
        }} catch(err) {{
            console.error('parse error', err);
        }}
    }};
    ws.onclose = () => {{
        document.getElementById('ws-dot').classList.remove('connected');
        _ws = null;
        if (!_userClosed) {{
            // サーバー側切断 → 3秒後に再接続
            _reconnectTimer = setTimeout(connect, 3000);
        }}
    }};
    ws.onerror = () => {{ ws.close(); }};
}}

function disconnect() {{
    if (_ws) {{
        _userClosed = true;
        _ws.close();
        _ws = null;
    }}
    if (_reconnectTimer) {{ clearTimeout(_reconnectTimer); _reconnectTimer = null; }}
}}

// ページ表示状態に応じて接続/切断
document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'hidden') {{
        disconnect();
    }} else {{
        connect();
    }}
}});

connect();

// ===== 設定パネル =====
async function applyConfig() {{
    const sta      = parseFloat(document.getElementById('cfg-sta').value);
    const lta      = parseFloat(document.getElementById('cfg-lta').value);
    const trig     = parseFloat(document.getElementById('cfg-trig').value);
    const det_hold = parseFloat(document.getElementById('cfg-det-hold').value);
    const status   = document.getElementById('cfg-status');
    status.textContent = '適用中...';
    status.style.color = '#8b949e';
    try {{
        const res = await fetch('/api/config', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ sta, lta, trig, det_hold }}),
        }});
        if (res.ok) {{
            status.textContent = '✓ 適用しました';
            status.style.color = '#3fb950';
            // STA/LTAパネルの閾値表示を更新
            const thrEl = document.querySelector('#stalta-vals strong[style*="f97316"]');
            if (thrEl) thrEl.textContent = trig;
            const staLtaEl = document.querySelector('#stalta-vals span:last-child');
            if (staLtaEl) staLtaEl.textContent = 'STA:' + sta + 's / LTA:' + lta + 's';
            // グラフ・バーの閾値線を更新
            TRIG_THR = trig;
            const thrDataset = historyChart.data.datasets[2];
            thrDataset.data = Array(N_HIST).fill(trig);
            thrDataset.label = 'thr=' + trig;
            historyChart.update('none');
            // STA/LTAバーの閾値線を即時更新
            const maxDisp = Math.max(trig * 2, 5.0);
            document.getElementById('stalta-thr-line').style.left = (trig / maxDisp * 100).toFixed(1) + '%';
        }} else {{
            status.textContent = '✗ エラー: ' + res.status;
            status.style.color = '#f85149';
        }}
    }} catch(e) {{
        status.textContent = '✗ 通信エラー';
        status.style.color = '#f85149';
    }}
    setTimeout(() => {{ status.textContent = ''; }}, 3000);
}}
</script>
<footer style="text-align:center; padding:6px 0; font-size:11px; color:#484f58; border-top:1px solid #21262d; flex-shrink:0;">
    Copyright &copy; 2026 株式会社リバーランズ・コンサルティング
</footer>
</body>
</html>"""


# ===== 設定永続化 =====

_CONFIG_PATH = pathlib.Path.home() / ".config" / "jma_intensity" / "config.json"
_CONFIG_KEYS = ("sta", "lta", "trig", "det_hold")


def _load_config(args, cli_specified: set) -> None:
    """config.json を読み込む。コマンドライン引数で明示指定されたキーは上書きしない。"""
    if not _CONFIG_PATH.exists():
        return
    try:
        data = json.loads(_CONFIG_PATH.read_text())
        applied = {}
        for key in _CONFIG_KEYS:
            if key in data and key not in cli_specified:
                setattr(args, key, float(data[key]))
                applied[key] = float(data[key])
        if applied:
            print(f"[INFO] 設定を読み込みました: {applied}")
    except Exception as e:
        print(f"[WARN] 設定ファイルの読み込みに失敗しました: {e}")


def _save_config(args) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {key: getattr(args, key) for key in _CONFIG_KEYS}
        _CONFIG_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[WARN] 設定ファイルの保存に失敗しました: {e}")


# ===== FastAPI アプリ =====

# グローバルに shared と stop_event を保持（lifespan との橋渡し）
_shared: SharedState | None = None
_stop_event: threading.Event | None = None
_args = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shared, _stop_event, _args

    shared = _shared
    stop_event = _stop_event
    args = _args

    comps = [c.strip().upper() for c in args.channels.split(",")]
    host, port_str = args.bind.split(":")
    port = int(port_str)

    log_path = pathlib.Path.home() / "Dropbox" / "earthQuake" / "logs" / "trigger_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shared.load_event_log(log_path, limit=50)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(1.0)

    max_window = int(max(args.rt_window, args.lta) * 2.0)
    rings_counts = {c: Ring(maxlen_samples=max(10_000, max_window * 200)) for c in comps}
    last_t0 = {c: None for c in comps}

    alert = AlertSpeaker()

    recv_th = threading.Thread(
        target=recv_loop_fn,
        args=(sock, rings_counts, comps, shared, last_t0, stop_event),
        daemon=True,
    )
    compute_th = threading.Thread(
        target=compute_loop,
        args=(rings_counts, comps, shared, args, stop_event, alert),
        daemon=True,
    )
    p2p_th = threading.Thread(
        target=p2p_ws_loop_web,
        args=(shared, stop_event),
        daemon=True,
    )

    recv_th.start()
    compute_th.start()
    p2p_th.start()

    task = asyncio.create_task(broadcast_loop(shared))

    yield

    stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    sock.close()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = _make_html(
        station=_args.station,
        network=_args.network,
        trig_thr=_args.trig,
        sta=_args.sta,
        lta=_args.lta,
        det_hold=_args.det_hold,
    )
    return HTMLResponse(content=html)


_CONFIG_LIMITS = {
    "sta":      (0.1,  30.0),
    "lta":      (1.0, 300.0),
    "trig":     (0.5,  50.0),
    "det_hold": (1.0, 600.0),
}


@app.post("/api/config")
async def api_config(req: Request):
    body = await req.json()
    for key, (lo, hi) in _CONFIG_LIMITS.items():
        if key not in body:
            continue
        try:
            val = float(body[key])
        except (TypeError, ValueError):
            return JSONResponse(status_code=422, content={"error": f"invalid value for {key}"})
        if math.isnan(val) or math.isinf(val) or not (lo <= val <= hi):
            return JSONResponse(status_code=422,
                                content={"error": f"{key} must be in [{lo}, {hi}]"})
        setattr(_args, key, val)
    _save_config(_args)
    return {"ok": True, "sta": _args.sta, "lta": _args.lta,
            "trig": _args.trig, "det_hold": _args.det_hold}


_ANALYZE_JOB_TTL = 3600  # 完了/失敗ジョブを1時間後に削除


def _purge_old_analyze_jobs():
    """完了/失敗から1時間経過したジョブを削除（_analyze_lock 保持下で呼ぶこと）。"""
    cutoff = time.time() - _ANALYZE_JOB_TTL
    stale = [jid for jid, j in _analyze_jobs.items()
             if j.get("completed_at", float("inf")) < cutoff]
    for jid in stale:
        _analyze_jobs.pop(jid, None)


def _run_analyze(job_id: str, start_jst: str, duration: int, out_path: str):
    """analyze_rs.py をサブプロセスで実行し、結果をジョブ辞書に記録する。"""
    cwd = pathlib.Path(__file__).parent.parent
    cmd = [
        sys.executable,
        str(pathlib.Path(__file__).parent / "analyze_rs.py"),
        "--start", start_jst,
        "--duration", str(duration),
        "--out", out_path,
    ]
    now = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), timeout=300)
        with _analyze_lock:
            if result.returncode == 0:
                _analyze_jobs[job_id] = {"status": "done", "out_path": out_path,
                                         "error": None, "completed_at": now}
            else:
                _analyze_jobs[job_id] = {
                    "status": "error",
                    "out_path": None,
                    "error": result.stderr[-500:] if result.stderr else "unknown error",
                    "completed_at": now,
                }
    except subprocess.TimeoutExpired:
        with _analyze_lock:
            _analyze_jobs[job_id] = {"status": "error", "out_path": None,
                                     "error": "timeout", "completed_at": now}
    except Exception as e:
        with _analyze_lock:
            _analyze_jobs[job_id] = {"status": "error", "out_path": None,
                                     "error": str(e), "completed_at": now}


@app.post("/api/analyze")
async def api_analyze_start(req: Request):
    body = await req.json()
    raw_time = (body.get("time") or "").strip()
    if not raw_time:
        return JSONResponse(status_code=422, content={"error": "time is required"})
    # "YYYY/MM/DD HH:MM", "YYYY-MM-DDTHH:MM" など → "YYYY-MM-DD HH:MM:00"
    start_jst = raw_time.replace("/", "-").replace("T", " ")
    if len(start_jst) == 16:
        start_jst += ":00"
    try:
        datetime.strptime(start_jst, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return JSONResponse(status_code=422, content={"error": f"invalid time format: {raw_time}"})
    duration = int(body.get("duration", 420))
    job_id = str(uuid.uuid4())[:8]
    out_dir = pathlib.Path(__file__).parent.parent / "data"
    out_path = str(out_dir / f"analyze_{job_id}.png")
    with _analyze_lock:
        _purge_old_analyze_jobs()
        _analyze_jobs[job_id] = {"status": "running", "out_path": None, "error": None}
    t = threading.Thread(target=_run_analyze, args=(job_id, start_jst, duration, out_path), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/analyze/{job_id}")
async def api_analyze_status(job_id: str):
    with _analyze_lock:
        job = _analyze_jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "job not found"})
    return job


@app.get("/api/analyze_img/{job_id}")
async def api_analyze_img(job_id: str):
    with _analyze_lock:
        job = _analyze_jobs.get(job_id)
    if job is None or job.get("status") != "done":
        return JSONResponse(status_code=404, content={"error": "not ready"})
    out_path = pathlib.Path(job["out_path"])
    if not out_path.exists():
        return JSONResponse(status_code=404, content={"error": "file not found"})
    from fastapi.responses import FileResponse
    return FileResponse(str(out_path), media_type="image/png")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


# ===== main =====

def main():
    global _shared, _stop_event, _args

    ap = argparse.ArgumentParser(description="Raspberry Shake UDP 計測震度 Web ダッシュボード")
    ap.add_argument("--bind", type=str, default="0.0.0.0:8888",
                    help="UDP受信アドレス:ポート（例: 0.0.0.0:8888）")
    ap.add_argument("--channels", type=str, default="ENZ,ENN,ENE",
                    help="3成分（カンマ区切り）RS4D加速度計: ENZ,ENN,ENE")
    ap.add_argument("--network", type=str, default="AM")
    ap.add_argument("--station", type=str, required=True)
    ap.add_argument("--sensitivity", type=float, default=387867.0,
                    help="counts/(m/s²)  R38DC実測値: 387867 (公式V6: 384500)")
    ap.add_argument("--rt-window", type=float, default=90.0,
                    help="震度計算の窓長[秒]")
    ap.add_argument("--sta", type=float, default=1.0, help="STA 窓長[秒]")
    ap.add_argument("--lta", type=float, default=20.0, help="LTA 窓長[秒]")
    ap.add_argument("--trig", type=float, default=3.5, help="STA/LTA しきい値")
    ap.add_argument("--det-hold", type=float, default=20.0,
                    help="検出後の再検出抑制[秒]")
    ap.add_argument("--web-port", type=int, default=8080,
                    help="HTTP サーバーポート（デフォルト: 8080）")
    ap.add_argument("--web-bind", type=str, default="127.0.0.1",
                    help="HTTP サーバーバインドアドレス（デフォルト: 127.0.0.1）")
    args = ap.parse_args()
    # コマンドラインで明示指定されたキーを収集（config.json より優先させるため）
    import sys
    cli_specified = set()
    for token in sys.argv[1:]:
        if token.startswith("--"):
            key = token.lstrip("-").split("=")[0].replace("-", "_")
            cli_specified.add(key)
    _load_config(args, cli_specified)

    comps = [c.strip().upper() for c in args.channels.split(",")]
    if len(comps) != 3:
        raise SystemExit("--channels に3成分を指定してください（例: ENZ,ENN,ENE）")

    _shared = SharedState()
    _stop_event = threading.Event()
    _args = args

    import uvicorn
    uvicorn.run(app, host=args.web_bind, port=args.web_port, log_level="info")


if __name__ == "__main__":
    main()
