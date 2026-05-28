#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake UDP リアルタイム計測震度 Web ダッシュボード
- FastAPI + uvicorn による HTTP/WebSocket サーバー
- jma_intensity_tui.py から SharedState, recv_loop_fn, compute_loop, AlertSpeaker を import
- jma_intensity_realtime.py から Ring, jma_scale_from_I を import
- P2P地震情報は points/latitude/longitude を含む独自パーサで処理

Copyright (c) 2026 Masanori Sakai
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

import jinja2

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from jma_intensity_tui import (
    SharedState,
    recv_loop_fn,
    compute_loop,
    AlertSpeaker,
    _p2p_scale_str,
    _parse_eew_item,
)
from jma_intensity_realtime import Ring, jma_scale_from_I


# ===== .env から観測点座標を読み込む =====
_ENV_PATH = pathlib.Path(__file__).parent.parent / '.env'
_station_lat: float | None = None
_station_lon: float | None = None
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith('#') or '=' not in _line:
            continue
        _k, _v = _line.split('=', 1)
        _k = _k.strip()
        _v = _v.strip().strip('"\'')
        if _k == 'STATION_LAT':
            try:
                _station_lat = float(_v)
            except ValueError:
                pass
        elif _k == 'STATION_LON':
            try:
                _station_lon = float(_v)
            except ValueError:
                pass


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

# 帯域パワー計算用: (低Hz, 高Hz, ラベル, 色)
_BAND_DEFS = [
    (0.05, 0.30, "うねり",   "#58a6ff"),
    (1.0,  5.0,  "地震/河川", "#f97316"),
    (5.0,  15.0, "交通",     "#3fb950"),
    (15.0, 44.0, "豪雨/風",  "#da3633"),
]


def _compute_band_powers(raw_z: np.ndarray, fs: float) -> list[float]:
    """垂直成分からWelch法でバンドパワー[dB ref 1 count²/Hz]を計算する。"""
    n = len(raw_z)
    if n < 64 or fs <= 0:
        return [None] * len(_BAND_DEFS)

    # Welch: nperseg は40秒分（fs*40）、最大4000サンプル
    # マイクロセイズム帯（0.05-0.30Hz）の観測に必要な周波数分解能 = fs/nperseg ≒ 0.025Hz
    nperseg = min(4000, max(64, int(fs * 40)))
    # ハニング窓
    step = nperseg // 2
    noverlap = nperseg - step
    win = np.hanning(nperseg)
    win_norm = np.sum(win ** 2)

    segments = []
    for start in range(0, n - nperseg + 1, step):
        seg = raw_z[start:start + nperseg] * win
        segments.append(seg)
    if not segments:
        return [None] * len(_BAND_DEFS)

    # 各セグメントのFFT → パワースペクトル
    specs = [np.abs(np.fft.rfft(s)) ** 2 / (fs * win_norm) for s in segments]
    psd = np.mean(specs, axis=0)
    freqs = np.fft.rfftfreq(nperseg, d=1.0 / fs)

    results = []
    for f_lo, f_hi, _, _ in _BAND_DEFS:
        idx = np.where((freqs >= f_lo) & (freqs < f_hi))[0]
        if len(idx) == 0:
            results.append(None)
            continue
        power = float(np.mean(psd[idx]))
        if power > 0:
            results.append(round(10.0 * math.log10(power), 1))
        else:
            results.append(None)
    return results


async def broadcast_loop(shared: SharedState):
    """1秒ごとに全WebSocketクライアントへ状態をブロードキャスト。"""
    _tick = 0
    _cached_band_powers: list | None = None
    while True:
        await asyncio.sleep(1.0)
        if not _ws_clients:
            continue

        snap = shared.snapshot()

        # バンドパワーは10秒ごとに再計算（現象の変化スケールに合わせる）
        if _tick % 10 == 0:
            fs = snap["fs"] or 100.0
            _cached_band_powers = _compute_band_powers(snap["raw_z"], fs)
        _tick += 1
        band_powers = _cached_band_powers

        payload = {
            "fs": snap["fs"],
            "I_final": snap["I_final"],
            "a_gal": snap["a_gal"],
            "scale": snap["scale"],
            "ratio": snap["ratio"],
            "triggered": snap["triggered"],
            "pkt_count": snap["pkt_count"],
            "pkt_lag": snap["pkt_lag"],
            "start_time": snap["start_time"],
            "i_history": snap["i_history"].tolist(),
            "ratio_history": snap["ratio_history"].tolist(),
            "events": list(snap["events"]),
            "p2p_quakes": list(snap["p2p_quakes"]),
            "p2p_eew": snap["p2p_eew"],
            "band_powers": band_powers,
            "config": {
                "sta": _args.sta,
                "lta": _args.lta,
                "trig": _args.trig,
                "det_hold": _args.det_hold,
                "confirm_window": _args.confirm_window,
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



# ===== 設定永続化 =====

_CONFIG_PATH = pathlib.Path.home() / ".config" / "jma_intensity" / "config.json"
_CONFIG_KEYS = ("sta", "lta", "trig", "det_hold", "confirm_window")


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
    ring_maxlen = max(10_000, max_window * 200)
    rings_counts = {c: Ring(maxlen_samples=ring_maxlen) for c in comps}
    rings_counts["EHZ"] = Ring(maxlen_samples=ring_maxlen)
    last_t0 = {c: None for c in comps}
    last_t0["EHZ"] = None

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

_TEMPLATE_PATH = pathlib.Path(__file__).parent / "templates" / "dashboard.html"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_PATH.parent)),
    autoescape=False,
)


@app.get("/", response_class=HTMLResponse)
async def index():
    tmpl = _jinja_env.get_template(_TEMPLATE_PATH.name)
    html = tmpl.render(
        station=_args.station,
        network=_args.network,
        trig_thr=_args.trig,
        sta=_args.sta,
        lta=_args.lta,
        det_hold=_args.det_hold,
        confirm_window=_args.confirm_window,
        station_lat=_station_lat,
        station_lon=_station_lon,
    )
    return HTMLResponse(content=html)


_CONFIG_LIMITS = {
    "sta":            (0.1,  30.0),
    "lta":            (1.0, 300.0),
    "trig":           (0.5,  50.0),
    "det_hold":       (1.0, 600.0),
    "confirm_window": (1.0,  60.0),
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


_GEOJSON_DIR = pathlib.Path(__file__).parent.parent / "data" / "geojson"
_STATIONS_PATH = pathlib.Path(__file__).parent.parent / "data" / "jma_stations.json"
_stations_cache_bytes: bytes | None = None

@app.get("/api/stations")
async def get_stations():
    """気象庁震度観測点一覧（name→{lat,lon,pref}）を返す。"""
    global _stations_cache_bytes
    if _stations_cache_bytes is None:
        if not _STATIONS_PATH.exists():
            return JSONResponse(status_code=404, content={"error": "stations data not found"})
        _stations_cache_bytes = _STATIONS_PATH.read_bytes()
    return Response(content=_stations_cache_bytes, media_type="application/json")


@app.get("/api/geojson/{pref_code}")
async def get_geojson_pref_index(pref_code: str):
    """都道府県内の市区町村コード一覧を返す。"""
    pref_dir = _GEOJSON_DIR / pref_code
    if not pref_dir.exists():
        return JSONResponse(status_code=404, content={"error": "not found"})
    codes = [p.stem for p in sorted(pref_dir.glob("*.json"))]
    return JSONResponse(content={"codes": codes})

@app.get("/api/geojson/{pref_code}/{city_code}")
async def get_geojson_city(pref_code: str, city_code: str):
    path = _GEOJSON_DIR / pref_code / f"{city_code}.json"
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "not found"})
    from fastapi.responses import FileResponse
    return FileResponse(str(path), media_type="application/json")


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
                    help="I値計算に使う波形窓長（秒）")
    ap.add_argument("--confirm-window", type=float, default=10.0,
                    help="トリガ後に揺れ継続を確認する窓（秒）。発話までのラグに直結する")
    ap.add_argument("--sta", type=float, default=1.0, help="STA 窓長[秒]")
    ap.add_argument("--lta", type=float, default=20.0, help="LTA 窓長[秒]")
    ap.add_argument("--trig", type=float, default=3.5, help="STA/LTA しきい値")
    ap.add_argument("--det-hold", type=float, default=20.0,
                    help="検出後の再検出抑制[秒]")
    ap.add_argument("--web-port", type=int, default=8080,
                    help="HTTP サーバーポート（デフォルト: 8080）")
    ap.add_argument("--web-bind", type=str, default="0.0.0.0",
                    help="HTTP サーバーバインドアドレス（デフォルト: 0.0.0.0）")
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
