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
from datetime import datetime, timedelta

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

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
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


# アプリのバージョン。リリース時に git タグ（vX.Y.Z）と揃えて手動更新する。
# WebUI のステータスバーに表示し、デプロイ反映を画面から確認できるようにする。
__version__ = "1.7.0"


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


# ===== バンドパワー履歴（24時間・1分平均）=====
_BP_HISTORY_MINUTES = 1440          # 24時間分
_BP_ACCUM: list[list[float]] = []   # 現在分の集計中サンプル [バンド別パワー値（真値、dBではない）]
_BP_ACCUM_MINUTE: int | None = None # 集計中の分（0-1439相当のエポック分）
_BP_HISTORY_PATH = pathlib.Path(__file__).parent.parent / "data" / "bp_history.jsonl"
_bp_history_lock = threading.Lock()

# 形式: deque of {"t": unix_timestamp_秒, "v": [db0, db1, db2, db3]}（dB, 0.01dB精度）
_bp_history: deque = deque(maxlen=_BP_HISTORY_MINUTES)


def _bp_history_load() -> None:
    """起動時にJSONLから履歴を復元する。24時間以内のエントリのみ読み込む。"""
    if not _BP_HISTORY_PATH.exists():
        return
    cutoff = time.time() - _BP_HISTORY_MINUTES * 60
    try:
        with open(_BP_HISTORY_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("t", 0) >= cutoff:
                        _bp_history.append(entry)
                except Exception:
                    pass
    except Exception:
        pass


def _bp_history_append(entry: dict) -> None:
    """履歴に1分平均エントリを追加し、JSONLに追記する。"""
    with _bp_history_lock:
        _bp_history.append(entry)
        try:
            _BP_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_BP_HISTORY_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def _bp_history_snapshot() -> list[dict]:
    """確定済み履歴＋現在集計中の分（暫定値）を返す。"""
    with _bp_history_lock:
        result = list(_bp_history)
    # 現在集計中の分を暫定エントリとして末尾に追加
    if _BP_ACCUM and _BP_ACCUM_MINUTE is not None:
        n_bands = len(_BP_ACCUM[0])
        avg = []
        for b in range(n_bands):
            # _BP_ACCUMはパワー真値（線形）で蓄積しているため、平均後にdB変換
            vals = [s[b] for s in _BP_ACCUM if s[b] is not None]
            avg.append(round(10.0 * math.log10(sum(vals) / len(vals)), 2) if vals else None)
        result.append({"t": _BP_ACCUM_MINUTE * 60, "v": avg})
    return result


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
        "time": eq.get("time", "")[:19],
        "issue_type": item.get("issue", {}).get("type", ""),
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
            results.append(10.0 * math.log10(power))
        else:
            results.append(None)
    return results


async def broadcast_loop(shared: SharedState):
    """1秒ごとに全WebSocketクライアントへ状態をブロードキャスト。"""
    global _BP_ACCUM, _BP_ACCUM_MINUTE
    _tick = 0
    _cached_band_powers: list | None = None
    while True:
        await asyncio.sleep(1.0)

        snap = shared.snapshot()

        # バンドパワーは10秒ごとに再計算（現象の変化スケールに合わせる）
        if _tick % 10 == 0:
            fs = snap["fs"] or 100.0
            _cached_band_powers = _compute_band_powers(snap["raw_z"], fs)

        # 1分平均を履歴に追記
        if _cached_band_powers and any(v is not None for v in _cached_band_powers):
            now_minute = int(time.time()) // 60
            if _BP_ACCUM_MINUTE != now_minute:
                # 前の分を確定して保存
                if _BP_ACCUM and _BP_ACCUM_MINUTE is not None:
                    n = len(_BP_ACCUM)
                    n_bands = len(_BP_ACCUM[0])
                    avg = []
                    for b in range(n_bands):
                        # _BP_ACCUMはパワー真値（線形）で蓄積しているため、平均後にdB変換
                        vals = [s[b] for s in _BP_ACCUM if s[b] is not None]
                        avg.append(round(10.0 * math.log10(sum(vals) / len(vals)), 2) if vals else None)
                    entry = {"t": _BP_ACCUM_MINUTE * 60, "v": avg}
                    asyncio.get_event_loop().run_in_executor(None, _bp_history_append, entry)
                _BP_ACCUM = []
                _BP_ACCUM_MINUTE = now_minute
            # パワー真値（線形値）でaccumして1分後にdB変換することで丸め誤差を排除
            _BP_ACCUM.append([10 ** (v / 10) if v is not None else None for v in _cached_band_powers])

        _tick += 1
        band_powers = _cached_band_powers

        if not _ws_clients:
            continue

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
            "band_powers_history": _bp_history_snapshot(),
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
    _bp_history_load()

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
        app_version=__version__,
    )
    # CSS/JS はこの HTML にインライン埋め込みのため、ブラウザが HTML をキャッシュすると
    # デプロイ後も古い画面が表示される。HTML は毎回サーバーから取得させる（小さいので負荷は軽微）。
    # GeoJSON 等の不変な静的データ（/api/geojson）はキャッシュさせたいので、ここでは / のみに付与する。
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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


def _run_analyze(job_id: str, start_jst: str, duration: int, out_path: str,
                 eq_name: str | None = None, eq_lat: float | None = None,
                 eq_lon: float | None = None, eq_mag: float | None = None,
                 eq_depth: float | None = None):
    """analyze_rs.py をサブプロセスで実行し、結果をジョブ辞書に記録する。"""
    cwd = pathlib.Path(__file__).parent.parent
    cmd = [
        sys.executable,
        str(pathlib.Path(__file__).parent / "analyze_rs.py"),
        "--start", start_jst,
        "--duration", str(duration),
        "--out", out_path,
    ]
    if eq_lat is not None and eq_lon is not None:
        cmd += ["--eq-lat", str(eq_lat), "--eq-lon", str(eq_lon)]
        if eq_name:
            cmd += ["--eq-name", eq_name]
        if eq_mag is not None:
            cmd += ["--eq-mag", str(eq_mag)]
        if eq_depth is not None:
            cmd += ["--eq-depth", str(eq_depth)]
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
        t_eq = datetime.strptime(start_jst, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return JSONResponse(status_code=422, content={"error": f"invalid time format: {raw_time}"})
    # LTA窓が安定するよう、発生時刻からLTA秒だけ前を解析開始とする
    lta_s = _args.lta if _args is not None else 20.0
    start_jst = (t_eq - timedelta(seconds=lta_s)).strftime("%Y-%m-%d %H:%M:%S")
    duration = int(body.get("duration", 420))
    eq_name  = body.get("eq_name") or None
    eq_lat   = body.get("eq_lat")
    eq_lon   = body.get("eq_lon")
    eq_mag   = body.get("eq_mag")
    eq_depth = body.get("eq_depth")
    job_id = str(uuid.uuid4())[:8]
    out_dir = pathlib.Path(__file__).parent.parent / "data"
    out_path = str(out_dir / f"analyze_{job_id}.png")
    with _analyze_lock:
        _purge_old_analyze_jobs()
        _analyze_jobs[job_id] = {"status": "running", "out_path": None, "error": None}
    t = threading.Thread(
        target=_run_analyze,
        args=(job_id, start_jst, duration, out_path),
        kwargs=dict(eq_name=eq_name, eq_lat=eq_lat, eq_lon=eq_lon, eq_mag=eq_mag, eq_depth=eq_depth),
        daemon=True,
    )
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


# ===== トリガ履歴 読み取り専用 API（統合システム fujimidai-observatory 向け）=====

# 震度（scale）の順序。文字列比較では "5弱" 等を正しく扱えないため、この順序で比較する。
_SCALE_ORDER = ["0", "1", "2", "3", "4", "5弱", "5強", "6弱", "6強", "7"]
_SCALE_RANK = {s: i for i, s in enumerate(_SCALE_ORDER)}

_TRIGGER_LOG_PATH = pathlib.Path.home() / "Dropbox" / "earthQuake" / "logs" / "trigger_log.jsonl"

# limit の上限。全行メモリ展開・ソートのコストを抑えるためのキャップ。
_EVENTS_LIMIT_MAX = 10000


def _read_trigger_events(
    log_path: pathlib.Path,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
    min_scale: str | None = None,
) -> list[dict]:
    """trigger_log.jsonl を読み、フィルタ後に新しい順で返す（読み取り専用）。

    - 各行は json.loads。壊れた行・空行はスキップ。
    - date/from/to は "date" フィールド（YYYY-MM-DD）の文字列比較で範囲判定する。
    - min_scale は _SCALE_ORDER による震度順序で比較（"5弱" 等のため文字列比較は不可）。
      _SCALE_ORDER に無い値を渡すと ValueError（fail-open を防ぐため黙って無視しない）。
    - 返却は date,ts の降順（新しい順）で limit 件まで。既存キー名はそのまま保持。

    I/O エラー・デコードエラーはここでは握りつぶさず呼び出し側へ伝播させる
    （「障害」を「イベント0件」に化けさせない。見逃しの方が深刻という方針に従う）。
    """
    if min_scale is not None and min_scale not in _SCALE_RANK:
        raise ValueError(f"invalid min_scale: {min_scale!r} (expected one of {_SCALE_ORDER})")

    if not log_path.exists():
        return []

    min_rank = _SCALE_RANK.get(min_scale) if min_scale is not None else None

    events: list[dict] = []
    # 書き込み側（jma_intensity_tui.py:add_event）が encoding="utf-8" で "5弱" 等を
    # 生 UTF-8 で書くため、読み取りも明示的に utf-8 を指定する（ロケール依存を排除）。
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                # 破損行のみスキップ（既存ログに空行・破損行が混じりうる）。
                continue
            if not isinstance(ev, dict):
                continue
            d = ev.get("date")
            if date is not None and d != date:
                continue
            if date_from is not None and (d is None or d < date_from):
                continue
            if date_to is not None and (d is None or d > date_to):
                continue
            if min_rank is not None:
                rank = _SCALE_RANK.get(ev.get("scale"))
                if rank is None or rank < min_rank:
                    continue
            events.append(ev)

    # 新しい順（date,ts 降順）。ts が無い行も落ちないよう空文字でソート。
    events.sort(key=lambda e: (e.get("date", ""), e.get("ts", "")), reverse=True)
    if limit is not None and limit >= 0:
        events = events[:limit]
    return events


@app.get("/api/version")
async def api_version():
    """アプリのバージョンを返す（デプロイ反映確認用）。"""
    return {"version": __version__}


@app.get("/api/events")
async def api_events(
    date: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=1000, ge=0, le=_EVENTS_LIMIT_MAX),
    min_scale: str | None = None,
):
    """トリガ履歴（trigger_log.jsonl）を読み取り専用でJSON返却する。

    クエリパラメータ（すべて任意）:
      - date     : YYYY-MM-DD。その日のトリガのみ
      - from / to: YYYY-MM-DD。期間フィルタ（date フィールドで範囲）
      - limit    : 返却件数上限（新しい順、デフォルト 1000、0〜10000）
      - min_scale: 指定があれば震度がそれ以上のみ（震度順序で比較）。
                   不正値は 422（黙って全件返さない）。
    """
    try:
        events = _read_trigger_events(
            _TRIGGER_LOG_PATH,
            date=date,
            date_from=from_,
            date_to=to,
            limit=limit,
            min_scale=min_scale,
        )
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    return {"count": len(events), "events": events}


# ===== HVSR日次モニタリング 読み取り専用 API =====
#
# 計算・蓄積は src/hvsr_weekly.py（別プロセス・iMac側launchdで毎日実行）が行い、
# ここでは data/hvsr_history.jsonl を読んでJSON化して返すのみ。
# SharedState・broadcast_loop・lifespan内のスレッド起動処理には一切触れない
# （設計書 documents/designs/2026-07-14-hvsr-weekly-monitoring.md の最上位制約）。
#
# 2026-07-15: 週次実行から毎日実行に変更したのに伴い、レコードのキーを
# week_start（週の月曜日）から capture_date（対象日そのもの）に変更した。
# 変更前に記録された古いレコードは week_start フィールドのまま残るため、
# _read_hvsr_history/api_hvsr_history 側で両方を読める後方互換を維持する。

_HVSR_HISTORY_PATH = pathlib.Path(__file__).parent.parent / "data" / "hvsr_history.jsonl"
_HVSR_HISTORY_LIMIT_DEFAULT = 365   # 1年分
_HVSR_HISTORY_LIMIT_MAX = 3650      # 10年分

# mtimeベースの軽量キャッシュ（起動時ロード＋ファイル変化時のみ全量再読み込み）。
# _bp_history のような deque への都度 .append() 更新ではなく全量再読み込み方式にした
# 理由: hvsr_history.jsonl は1日1レコードしか増えないため、都度追記を追跡する仕組みは
# 不要で、ファイル全体を読み直す方が実装が単純。
_hvsr_history_cache: list[dict] = []
_hvsr_history_mtime: float | None = None


def _read_hvsr_history(path: pathlib.Path) -> list[dict]:
    """hvsr_history.jsonl を読み、壊れた行をスキップして dict のリストを返す（読み取り専用）。"""
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _hvsr_history_snapshot() -> list[dict]:
    """mtimeが変化していればファイルを全量再読み込みし、日次推移の全件を返す（新しい順ではなく記録順）。

    このハンドラは同期ファイルI/Oを run_in_executor に逃がさず async def 内で直接
    実行している。/api/events の _read_trigger_events と同様、実行中はイベントループ
    全体をブロックする（レビュー指摘: 中重大度）。ここで同期実装のまま許容できると
    判断した根拠は次の2点:
      1. hvsr_history.jsonl は1日1レコードしか増えない。10年運用しても3650行程度に
         収まる。実測（開発機、freq_hz/hv_ratio各81点を含む1レコード約2.3KBの実データ
         から換算）では10年分でも約8MB・全量読み込みは約56ms程度と推定できる規模。
         合計しても1リクエストあたり数十ms程度で、broadcast_loopの1秒tickを
         致命的に圧迫する量ではないと判断した（週次1回運用時の「10ms未満」からは
         上振れするため、ファイルサイズが今後さらに増える場合は判断を見直すこと）。
      2. リクエスト頻度も低い（WebUIが起動時に1回fetchするのみで、bp_historyや
         events のような高頻度ポーリング対象ではない）。
    ファイルサイズ・リクエスト頻度のいずれかが将来変わる場合は、この判断を
    見直し run_in_executor に処理を逃がすことを検討すること。
    """
    global _hvsr_history_cache, _hvsr_history_mtime
    if not _HVSR_HISTORY_PATH.exists():
        _hvsr_history_cache = []
        _hvsr_history_mtime = None
        return _hvsr_history_cache
    mtime = _HVSR_HISTORY_PATH.stat().st_mtime
    if _hvsr_history_mtime is None or mtime != _hvsr_history_mtime:
        _hvsr_history_cache = _read_hvsr_history(_HVSR_HISTORY_PATH)
        _hvsr_history_mtime = mtime
    return _hvsr_history_cache


@app.get("/api/hvsr_history")
async def api_hvsr_history(
    limit: int = Query(default=_HVSR_HISTORY_LIMIT_DEFAULT, ge=0, le=_HVSR_HISTORY_LIMIT_MAX),
):
    """HVSR日次モニタリング履歴（data/hvsr_history.jsonl）を読み取り専用でJSON返却する。

    クエリパラメータ:
      - limit: 返却件数上限（新しい順=直近の日から、デフォルト365日=1年分、最大3650日=10年分）

    無認証・読み取り専用。/api/events と同様、個人情報・機微情報を含まないため
    追加のアクセス制御は不要と判断する（設計書「セキュリティ」節参照）。
    """
    history = _hvsr_history_snapshot()
    # capture_date昇順で記録されている前提だが、念のため明示的にソートしてから
    # 新しい順にlimit件を切り出し、時系列表示用に古い順へ戻す。
    # 2026-07-15以前の週次実行時代のレコードは capture_date を持たないため
    # week_start にフォールバックする（後方互換）。
    sorted_history = sorted(history, key=lambda e: e.get("capture_date") or e.get("week_start", ""))
    if limit is not None and limit >= 0:
        sorted_history = sorted_history[-limit:] if limit > 0 else []
    return {"count": len(sorted_history), "history": sorted_history}


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
                    help="トリガ後に計測震度ピークを追跡して履歴に記録するまでの窓（秒）")
    ap.add_argument("--speak-delay", type=float, default=2.0,
                    help="計測震度が0.5を超えてから初回発話するまでの待機（秒）。短いほど速報的。発話後に震度が上がれば言い直す")
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
