#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Shake UDP リアルタイム計測震度 TUI ダッシュボード
- rich.Live を使ったターミナルUI
- 3層構成: recv_loop → compute_loop → 描画(main)

Copyright (c) 2026 Masanori Sakai
"""

import argparse
import io
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import json

import numpy as np
from scipy.signal import butter, sosfilt
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

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ===== アラート音声 =====

def _voicevox_speaker_id(base_url: str, name: str = "ずんだもん", style: str = "ノーマル") -> int | None:
    """VoiceVox Engine から話者IDを取得。見つからなければ None。"""
    try:
        with urllib.request.urlopen(f"{base_url}/speakers", timeout=2) as r:
            import json
            speakers = json.loads(r.read())
        for s in speakers:
            if name in s["name"]:
                for st in s["styles"]:
                    if st["name"] == style:
                        return st["id"]
                return s["styles"][0]["id"]
    except Exception:
        return None


def _voicevox_speak(text: str, base_url: str, speaker_id: int, speed: float = 1.1):
    """VoiceVox で音声合成して afplay で再生（同期・完了まで待機）。失敗は無視。"""
    try:
        import json, urllib.parse
        params = urllib.parse.urlencode({"text": text, "speaker": speaker_id})
        req = urllib.request.Request(
            f"{base_url}/audio_query?{params}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            query_obj = json.loads(r.read())
        query_obj["speedScale"] = speed
        query_bytes = json.dumps(query_obj).encode("utf-8")
        req2 = urllib.request.Request(
            f"{base_url}/synthesis?speaker={speaker_id}",
            data=query_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=10) as r:
            wav = r.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            fname = f.name
        subprocess.run(["afplay", fname], check=False)
    except Exception:
        pass


def _say_popen(text: str, rate: int | None = None) -> subprocess.Popen | None:
    """macOS say コマンドで読み上げを開始し、プロセスハンドルを返す（非ブロッキング）。

    rate を指定すると say -r <words/分> で話速を変える。震度5以上は
    速くして緊迫感を出す用途（Kyoko のデフォルトは約175wpm）。
    呼び出し側が terminate() で中断（言い直し）できるよう Popen を返す。
    """
    cmd = ["say", "-v", "Kyoko"]
    if rate is not None:
        cmd += ["-r", str(rate)]
    cmd.append(text)
    try:
        return subprocess.Popen(cmd)
    except Exception:
        return None


_SCALE_ALERT_PREFIX = {
    "0":   "揺れを検出。",
    "1":   "揺れを検出。",
    "2":   "揺れを検出。",
    "3":   "注意！地震です。",
    "4":   "注意！地震です。",
    "5弱": "警告！強い地震です。",
    "5強": "警告！強い地震です。",
    "6弱": "緊急警報！非常に強い地震です。",
    "6強": "緊急警報！非常に強い地震です。",
    "7":   "緊急警報！非常に強い地震です。",
}

_SCALE_MESSAGES = {
    "0":   "周囲の状況を確認してください。",
    "1":   "周囲の状況を確認してください。",
    "2":   "周囲の状況を確認してください。",
    "3":   "落下物などに気をつけてください。",
    "4":   "不安定な場所から離れてください。",
    "5弱": "今すぐ身を守ってください。",
    "5強": "今すぐ身を守ってください。",
    "6弱": "今すぐ机の下など安全な場所に避難してください。",
    "6強": "今すぐ机の下など安全な場所に避難してください。",
    "7":   "今すぐ安全な場所に避難してください。津波に注意してください。",
}


def _log_alert_latency(
    trig_time: float | None,
    call_time: float,
    start_time: float,
    end_time: float,
    scale: str,
    I_final: float,
    engine: str,
):
    """
    検出→発話のタイムラグをJSONL形式でログに記録。

    フィールド:
      detected_at   : トリガ確定時刻（ISO8601）
      called_at     : speak() 呼び出し時刻
      voice_start_at: 音声スレッド内でエンジン呼び出し開始時刻
      voice_end_at  : 音声再生完了時刻
      lag_trig_to_call_s  : トリガ確定 → speak() 呼び出し [秒]（rt_window待ち含む）
      lag_call_to_voice_s : speak() 呼び出し → 音声開始 [秒]（スレッド起動オーバーヘッド）
      lag_trig_to_voice_s : トリガ確定 → 音声開始 [秒]（合計ラグ）
      voice_duration_s    : 音声再生所要時間 [秒]
    """
    log_dir = pathlib.Path.home() / "Dropbox" / "earthQuake" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "alert_latency.jsonl"

    entry: dict = {
        "detected_at":    datetime.fromtimestamp(trig_time).isoformat() if trig_time else None,
        "called_at":      datetime.fromtimestamp(call_time).isoformat(),
        "voice_start_at": datetime.fromtimestamp(start_time).isoformat(),
        "voice_end_at":   datetime.fromtimestamp(end_time).isoformat(),
        "lag_trig_to_call_s":  round(call_time - trig_time, 3) if trig_time else None,
        "lag_call_to_voice_s": round(start_time - call_time, 3),
        "lag_trig_to_voice_s": round(start_time - trig_time, 3) if trig_time else None,
        "voice_duration_s":    round(end_time - start_time, 3),
        "scale":   scale,
        "I_final": I_final,
        "engine":  engine,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


class AlertSpeaker:
    """
    VoiceVox（青山龍星）優先、使えなければ macOS say にフォールバック。
    起動時に VoiceVox の疎通確認を行う。

    クレジット表記「VOICEVOX:青山龍星」が必要（README/MANUAL 参照）。
    """
    VOICEVOX_URL = "http://localhost:50021"
    SPEAKER_NAME = "青山龍星"
    SPEAKER_STYLE = "ノーマル"

    def __init__(self):
        self._speaker_id: int | None = None
        self._use_voicevox = False
        self._check_voicevox()
        # 言い直し（方式Z）用: 現在再生中の say プロセスと最後に発話した震度を保持。
        # 発話中に震度スケールが上がったら、再生を terminate して新しい値で言い直す。
        self._proc_lock = threading.Lock()
        self._cur_proc: subprocess.Popen | None = None

    def _check_voicevox(self):
        # VoiceVox合成遅延のため現在は無効化（sayにフォールバック固定）
        # 再有効化する場合はコメントアウトを外す
        # sid = _voicevox_speaker_id(self.VOICEVOX_URL, self.SPEAKER_NAME, self.SPEAKER_STYLE)
        # if sid is not None:
        #     self._speaker_id = sid
        #     self._use_voicevox = True
        pass

    def speak(self, scale: str, I_final: float, trig_time: float | None = None):
        """音声読み上げを非同期で実行。trig_time を渡すと発話開始までのラグをログに記録。"""
        i_str = f"{I_final:.2f}".replace(".", "点")
        prefix = _SCALE_ALERT_PREFIX.get(scale, "注意！地震です。")
        caution = _SCALE_MESSAGES.get(scale, "")
        text = f"{prefix}震度{scale}。計測震度{i_str}。{caution}"
        # 震度5以上（5弱/5強/6弱/6強/7）は話速を上げて緊迫感を出す。
        # 震度4以下は None（say のデフォルト話速 約175wpm）。
        say_rate = 240 if scale[:1] in ("5", "6", "7") else None
        call_time = time.time()

        def _run():
            start_time = time.time()
            engine = "voicevox" if self._use_voicevox else "say"
            if self._use_voicevox:
                # VoiceVox は同期再生（言い直し非対応）。現状は say フォールバック固定のため通常ここは通らない。
                _voicevox_speak(text, self.VOICEVOX_URL, self._speaker_id)
            else:
                # 言い直し（方式Z）: 再生中のプロセスがあれば terminate してから新規再生。
                # 呼び出し側（compute_loop）が「震度スケールが上がったとき」だけ speak() を呼ぶので、
                # ここでは無条件に直前の発話を打ち切って最新の震度で読み直す。
                with self._proc_lock:
                    if self._cur_proc is not None and self._cur_proc.poll() is None:
                        self._cur_proc.terminate()
                    proc = _say_popen(text, rate=say_rate)
                    self._cur_proc = proc
                if proc is not None:
                    proc.wait()
            end_time = time.time()
            _log_alert_latency(
                trig_time=trig_time,
                call_time=call_time,
                start_time=start_time,
                end_time=end_time,
                scale=scale,
                I_final=I_final,
                engine=engine,
            )

        threading.Thread(target=_run, daemon=True).start()

    @property
    def engine(self) -> str:
        return f"VoiceVox（{self.SPEAKER_NAME}/{self.SPEAKER_STYLE}）" if self._use_voicevox else "macOS say (Kyoko)"


# ===== P2P地震情報 =====

_P2P_SCALE = {
    10: "1", 20: "2", 30: "3", 40: "4",
    45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
}

def _p2p_scale_str(val: int) -> str:
    return _P2P_SCALE.get(val, "?")


def _parse_quake_item(item: dict) -> dict | None:
    """code=551 の1アイテムを表示用dictに変換。不正な場合は None。"""
    eq = item.get("earthquake", {})
    if not eq:
        return None
    hypo = eq.get("hypocenter", {})
    return {
        "id": item.get("id", ""),
        "time": eq.get("time", "")[:16],
        "name": hypo.get("name", "不明"),
        "magnitude": hypo.get("magnitude", 0.0),
        "depth": hypo.get("depth", 0),
        "max_scale": _p2p_scale_str(eq.get("maxScale", -1)),
        "tsunami": eq.get("domesticTsunami", "None"),
    }


def _fetch_p2p_quakes_http(limit: int = 5) -> list[dict]:
    """起動時に既存の地震情報をHTTPで初回取得。失敗時は空リスト。"""
    if not _urllib_ok:
        return []
    try:
        url = f"https://api.p2pquake.net/v2/history?codes=551&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "rs4d-jma-intensity/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            items = json.loads(r.read())
        result = []
        for item in items:
            parsed = _parse_quake_item(item)
            if parsed:
                result.append(parsed)
        return result
    except Exception:
        return []


def _parse_eew_item(item: dict) -> dict | None:
    """code=556 の1アイテムをEEW表示用dictに変換。不正または取消の場合は None。"""
    if item.get("cancelled", False):
        return None
    eq = item.get("earthquake", {})
    if not eq:
        return None
    hypo = eq.get("hypocenter", {})
    issue = item.get("issue", {})
    areas = item.get("areas", [])
    max_scale = max((a.get("forecastMaxIntensity", -1) for a in areas), default=-1) if areas else -1
    return {
        "id": item.get("id", ""),
        "time": issue.get("time", "")[:16],
        "name": hypo.get("name", "不明"),
        "magnitude": hypo.get("magnitude", -1.0),
        "depth": hypo.get("depth", -1),
        "max_scale": _p2p_scale_str(max_scale) if max_scale >= 0 else "?",
    }


def p2p_ws_loop(shared: "SharedState", stop_event: threading.Event):
    """WebSocket でP2P地震情報をリアルタイム受信。自動再接続あり。"""
    WS_URL = "wss://api.p2pquake.net/v2/ws"

    # 起動時にHTTPで初回データ取得
    initial = _fetch_p2p_quakes_http(5)
    seen_ids: set[str] = {q["id"] for q in initial}
    with shared._lock:
        shared.p2p_quakes = initial
        shared.p2p_seen_ids = seen_ids
        shared._p2p_seen_ids_fifo.extend(seen_ids)

    if not _websocket_ok:
        # websocket-client がなければ60秒ポーリングにフォールバック
        while not stop_event.is_set():
            quakes = _fetch_p2p_quakes_http(5)
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
                # deque(maxlen=1000) が溢れたら古いIDを set からも削除
                if len(shared._p2p_seen_ids_fifo) == shared._p2p_seen_ids_fifo.maxlen:
                    oldest = shared._p2p_seen_ids_fifo[0]
                    shared.p2p_seen_ids.discard(oldest)
                shared._p2p_seen_ids_fifo.append(item_id)
                shared.p2p_seen_ids.add(item_id)

        if code == 551:
            parsed = _parse_quake_item(item)
            if parsed:
                with shared._lock:
                    shared.p2p_quakes = ([parsed] + list(shared.p2p_quakes))[:5]
        elif code == 556:
            parsed_eew = _parse_eew_item(item)
            with shared._lock:
                shared.p2p_eew = parsed_eew  # None = 取消 or 無効
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
            stop_event.wait(5)  # 切断時5秒後に再接続


from jma_intensity_realtime import (
    apply_jma_filter_time,
    a_threshold_for_03s,
    jma_frequency_response,
    jma_scale_from_I,
    parse_udp_packet,
    Ring,
)

# ===== 震度スケールの色 =====
_INTENSITY_COLORS = {
    "0":  "bright_white",
    "1":  "cyan",
    "2":  "green",
    "3":  "yellow",
    "4":  "dark_orange",
    "5弱": "red",
    "5強": "red1",
    "6弱": "bright_red",
    "6強": "bold bright_red",
    "7":  "bold red on white",
}

# ===== スパークライン用ブロック =====
_SPARKS = " ▁▂▃▄▅▆▇█"


def _sparkline(data: np.ndarray, width: int = 50) -> str:
    """DC除去したraw countsをスパークラインに変換"""
    if len(data) == 0:
        return " " * width
    # 直近 width サンプル
    seg = data[-width:] if len(data) >= width else data
    # DC除去
    seg = seg - np.mean(seg)
    amax = np.max(np.abs(seg))
    if amax == 0:
        mid = len(_SPARKS) // 2
        return _SPARKS[mid] * len(seg)
    normed = seg / amax  # -1 .. 1
    # 0..8 にマップ
    idx = np.clip(np.round((normed + 1.0) * 4.0).astype(int), 0, 8)
    return "".join(_SPARKS[i] for i in idx)


def _sparkline_raw(data: np.ndarray, width: int = 60, vmin: float = 0.0, vmax: float = 1.0) -> str:
    """値域[vmin,vmax]固定のスパークライン（推移グラフ用）"""
    if len(data) == 0:
        return " " * width
    seg = data[-width:] if len(data) >= width else data
    span = vmax - vmin if vmax > vmin else 1.0
    normed = np.clip((seg - vmin) / span, 0.0, 1.0)
    idx = np.clip(np.round(normed * 8.0).astype(int), 0, 8)
    return "".join(_SPARKS[i] for i in idx)


def _intensity_bar(I_final: float, width: int = 40) -> Text:
    """計測震度 0–7 を横バーで可視化"""
    ratio = min(max(I_final, 0.0), 7.0) / 7.0
    filled = max(1, int(ratio * width))
    empty = width - filled
    scale = jma_scale_from_I(I_final)
    color = _INTENSITY_COLORS.get(scale, "white")
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="bright_black")
    bar.append(f"  {I_final:.2f} ({scale})", style=color)
    return bar


def _stalta_bar(ratio: float, trig_thr: float, width: int = 30) -> Text:
    """STA/LTA比を横バーで可視化"""
    max_disp = max(trig_thr * 2.0, 5.0)
    filled = min(int((ratio / max_disp) * width), width)
    empty = width - filled
    color = "red" if ratio >= trig_thr else "cyan"
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="bright_black")
    bar.append(f"  {ratio:.2f} (thr={trig_thr:.1f})", style=color)
    return bar


# ===== 共有ステート =====
class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.fs = 0.0
        self.I_final = 0.0
        self.a_gal = 0.0
        self.scale = "0"
        self.ratio = 0.0
        self.triggered = False
        self.pkt_count = 0
        self.start_time = time.time()
        self.last_pkt_wall_time: float = time.time()  # 最終UDP受信時刻（実時計）
        self.pkt_lag: float = 0.0  # 直近パケット: 受信時刻 - パケット先頭時刻 [秒]
        # 直近 n サンプルの raw counts (各成分)
        self.raw_z = np.zeros(0)
        self.raw_n = np.zeros(0)
        self.raw_e = np.zeros(0)
        # イベント履歴（最大50件）
        self.events: deque = deque(maxlen=50)
        self._event_log_path: pathlib.Path | None = None
        # P2P地震情報
        self.p2p_quakes: list = []
        self.p2p_seen_ids: set = set()           # O(1)検索用
        self._p2p_seen_ids_fifo: deque = deque(maxlen=1000)  # FIFO上限管理用
        self.p2p_eew: dict | None = None  # 最新EEW（取消/無効時はNone）
        self._p2p_eew_received_at: float = 0.0  # EEW受信時刻（TTL管理用）
        # I値・STA/LTA推移（直近600点 = 5分@0.5s間隔）
        self.i_history: deque = deque(maxlen=600)
        self.ratio_history: deque = deque(maxlen=600)

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self):
        with self._lock:
            return {
                "fs": self.fs,
                "I_final": self.I_final,
                "a_gal": self.a_gal,
                "scale": self.scale,
                "ratio": self.ratio,
                "triggered": self.triggered,
                "pkt_count": self.pkt_count,
                "pkt_lag": self.pkt_lag,
                "start_time": self.start_time,
                "raw_z": self.raw_z.copy(),
                "raw_n": self.raw_n.copy(),
                "raw_e": self.raw_e.copy(),
                "events": list(self.events),
                "p2p_quakes": list(self.p2p_quakes),
                "p2p_eew": (self.p2p_eew
                            if time.time() - self._p2p_eew_received_at < 600
                            else None),
                "i_history": np.array(self.i_history, dtype=np.float64),
                "ratio_history": np.array(self.ratio_history, dtype=np.float64),
            }

    def add_event(self, ts: str, I: float, scale: str, ratio: float = 0.0):
        with self._lock:
            self.events.append((ts, I, scale, ratio))
        if self._event_log_path is not None:
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                record = json.dumps({"date": date_str, "ts": ts,
                                     "I": I, "scale": scale, "ratio": ratio},
                                    ensure_ascii=False)
                with open(self._event_log_path, "a", encoding="utf-8") as f:
                    f.write(record + "\n")
            except Exception:
                pass

    def load_event_log(self, log_path: pathlib.Path, limit: int = 50) -> None:
        self._event_log_path = log_path
        if not log_path.exists():
            return
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            records = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    date_str = d.get("date", "")
                    ts_full = f"{date_str} {d['ts']}" if date_str else d["ts"]
                    records.append((ts_full, float(d["I"]), d["scale"],
                                    float(d.get("ratio", 0.0))))
                except Exception:
                    continue
            with self._lock:
                for rec in records[-limit:]:
                    self.events.append(rec)
        except Exception:
            pass


# ===== 画面構築 =====
def build_display(state: dict, station: str, network: str, trig_thr: float = 3.5) -> Panel:
    now_utc = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime = int(time.time() - state["start_time"])
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60

    # --- ヘッダー ---
    header_text = (
        f"[bold]{network}.{station}[/bold]  "
        f"RS4D  [dim]{state['fs']:.1f}Hz[/dim]  "
        f"パケット:[cyan]{state['pkt_count']}[/cyan]  "
        f"稼働:[dim]{h:02d}:{m:02d}:{s:02d}[/dim]  "
        f"[dim]{now_utc}[/dim]"
    )

    # --- 計測震度パネル ---
    scale = state["scale"]
    color = _INTENSITY_COLORS.get(scale, "white")
    intensity_table = Table.grid(padding=(0, 1))
    intensity_table.add_row(
        Text(f"震度 {scale}", style=f"bold {color}", justify="center"),
        Text(f"I = {state['I_final']:.2f}", style="bold white"),
        Text(f"  a = {state['a_gal']:.3f} gal", style="dim"),
    )
    intensity_table.add_row(_intensity_bar(state["I_final"], width=50))

    # --- 波形パネル ---
    waveform_table = Table.grid(padding=(0, 1))
    waveform_table.add_column(width=4)
    waveform_table.add_column()
    spark_width = 60
    waveform_table.add_row(
        Text("ENZ", style="bold cyan"),
        Text(_sparkline(state["raw_z"], spark_width), style="cyan"),
    )
    waveform_table.add_row(
        Text("ENN", style="bold green"),
        Text(_sparkline(state["raw_n"], spark_width), style="green"),
    )
    waveform_table.add_row(
        Text("ENE", style="bold yellow"),
        Text(_sparkline(state["raw_e"], spark_width), style="yellow"),
    )

    # --- STA/LTA + 推移グラフパネル ---
    stalta_table = Table.grid(padding=(0, 1))
    stalta_table.add_column(width=9)
    stalta_table.add_column()
    stalta_table.add_row(Text("STA/LTA", style="bold"), _stalta_bar(state["ratio"], 3.5, width=40))

    i_hist = state.get("i_history", np.zeros(0))
    r_hist = state.get("ratio_history", np.zeros(0))
    spark_w = 60
    stalta_table.add_row(
        Text("I値推移", style="dim"),
        Text(_sparkline_raw(i_hist, spark_w, vmin=0.0, vmax=7.0), style="yellow"),
    )
    stalta_table.add_row(
        Text("STA推移", style="dim"),
        Text(_sparkline_raw(r_hist, spark_w, vmin=0.0, vmax=max(float(np.max(r_hist)) if len(r_hist) else 1.0, trig_thr * 2)), style="cyan"),
    )

    # --- トリガ履歴 ---
    event_table = Table("時刻", "I値", "震度", "STA/LTA", box=None, padding=(0, 2), show_header=True)
    event_table.columns[0].style = "dim"
    event_table.columns[1].style = "cyan"
    event_table.columns[2].style = "bold"
    event_table.columns[3].style = "yellow"
    events = state["events"]
    if events:
        for ev in reversed(events):
            ts, I_val, sc = ev[0], ev[1], ev[2]
            ratio_val = ev[3] if len(ev) > 3 else 0.0
            event_color = _INTENSITY_COLORS.get(sc, "white")
            event_table.add_row(ts, f"{I_val:.2f}", Text(f"震度{sc}", style=event_color), f"{ratio_val:.2f}")
    else:
        event_table.add_row("[dim](なし)[/dim]", "", "", "")

    # --- P2P地震情報 ---
    p2p_table = Table("発生時刻", "震源", "M", "深さ", "最大震度", box=None, padding=(0, 1), show_header=True)
    p2p_table.columns[0].style = "dim"
    p2p_table.columns[1].style = "white"
    p2p_table.columns[2].style = "cyan"
    p2p_table.columns[3].style = "dim"
    quakes = state.get("p2p_quakes", [])
    if quakes:
        for q in quakes:
            sc = q["max_scale"]
            color = _INTENSITY_COLORS.get(sc, "white")
            tsunami = " [red]津波[/red]" if q["tsunami"] not in ("None", "Unknown") else ""
            p2p_table.add_row(
                q["time"],
                q["name"],
                f"{q['magnitude']:.1f}",
                f"{q['depth']}km",
                Text(f"震度{sc}{tsunami}", style=color),
            )
    else:
        p2p_table.add_row("[dim]取得中...[/dim]", "", "", "", "")

    # --- EEW行 ---
    eew = state.get("p2p_eew")
    if eew:
        eew_sc = eew.get("max_scale", "?")
        eew_color = _INTENSITY_COLORS.get(eew_sc, "white")
        mag_str = f"M{eew['magnitude']:.1f}" if eew["magnitude"] >= 0 else "M不明"
        eew_text = Text.from_markup(
            f"[bold red] ⚡ EEW (参考)  [/bold red]"
            f"震源:[bold]{eew['name']}[/bold]  "
            f"{mag_str}  "
            f"最大予測震度:[{eew_color}]震度{eew_sc}[/{eew_color}]  "
            f"[dim]{eew['time']}[/dim]"
            f"  [dim]※P2P経由・無保証[/dim]"
        )
        eew_panel = Panel(eew_text, border_style="red", height=3)
    else:
        eew_panel = Panel(
            Text.from_markup("[dim]EEW なし[/dim]"),
            border_style="bright_black",
            height=3,
        )

    # --- レイアウト組み立て ---
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="intensity", size=4),
        Layout(name="waveform", size=5),
        Layout(name="stalta", size=5),
        Layout(name="eew", size=3),
        Layout(name="bottom", size=9),
        Layout(name="footer", size=1),
    )
    layout["bottom"].split_row(
        Layout(name="events", minimum_size=30),
        Layout(name="p2p", minimum_size=50),
    )
    layout["header"].update(Text.from_markup(header_text))
    layout["intensity"].update(Panel(intensity_table, title="[bold]計測震度[/bold]", border_style="bright_white"))
    layout["waveform"].update(Panel(waveform_table, title="[bold]波形 (raw counts, DC除去)[/bold]", border_style="cyan"))
    layout["stalta"].update(Panel(stalta_table, title="[bold]STA/LTA 検出[/bold]", border_style="green"))
    layout["eew"].update(eew_panel)
    layout["events"].update(Panel(event_table, title="[bold]トリガ履歴[/bold]", border_style="yellow"))
    layout["p2p"].update(Panel(p2p_table, title="[bold]最新地震情報 (P2P)[/bold]", border_style="magenta"))
    layout["footer"].update(Text.from_markup(
        "[dim]Copyright (c) 2026 Masanori Sakai[/dim]",
        justify="center",
    ))

    triggered_style = "bold red on white" if state["triggered"] else "bold blue"
    status = "⚠ トリガ" if state["triggered"] else " 監視中"
    return Panel(layout, title=f"[{triggered_style}]{status}[/{triggered_style}]", border_style="bright_white")


# ===== compute_loop =====
def compute_loop(rings_counts, comps, shared: SharedState, args, stop_event, alert: AlertSpeaker):
    last_triggered_time = 0.0
    pending_queue: deque = deque()  # (trigger_time, trigger_ts, trigger_ratio) FIFO

    # ===== 発話用ライブ監視の状態（STA/LTAトリガとは独立）=====
    # トリガ履歴は STA/LTA 比で記録するが、発話はライブ計測震度 I_final が震度1相当（0.5）を
    # 超えたかどうかで駆動する。遠地地震（S-P時間が長くトリガ発火から震度立ち上がりまで数十秒
    # かかる）でも近地地震でも、「計測震度が実際に0.5を超えた時点」で発話できるようにするため。
    #
    # 方式Z（言い直し）: 0.5超過から speak_delay 秒ピークを見て一度発話したあとも監視を続け、
    # 震度スケールが1段階以上上がったら AlertSpeaker.speak() を呼んで言い直す（再生中ならkill）。
    # 同じ震度の中での数値変動では言い直さない（途切れ防止）。
    _SPEAK_THRESHOLD_I = 0.5      # 震度1相当。これを超えたら発話対象
    speak_active = False         # True: 0.5超過中（揺れ継続中）
    speak_pending_since = None   # 0.5を超えてピーク観測を始めた時刻（None=初回発話前でない）
    speak_peak_I = 0.0           # 観測中のピーク I_final
    speak_peak_scale = "0"
    spoken_I = 0.0               # 最後に発話した I 値（震度スケール上昇判定の基準）
    spoken_scale = None          # 最後に発話した震度スケール（None=未発話）

    def sta_lta_ratio(vec, fs, sta_s, lta_s):
        nsta = max(1, int(round(sta_s * fs)))
        nlta = max(nsta + 1, int(round(lta_s * fs)))
        if len(vec) < nlta:
            return 0.0
        s_sta = np.mean(vec[-nsta:] ** 2)
        s_lta = np.mean(vec[-nlta:-nsta] ** 2)
        # LTAが実質ゼロ = データギャップ後のバッファ未充填。誤検出を防ぐため 0 を返す
        if s_lta < 1e-12:
            return 0.0
        return float(s_sta / s_lta)

    def bandpass_ehz(x, fs, f_lo=1.0, f_hi=10.0):
        """EHZ用 1-10Hz バンドパスフィルタ（Butterworth 4次）。"""
        nyq = fs / 2.0
        if f_hi >= nyq:
            f_hi = nyq * 0.95
        sos = butter(4, [f_lo / nyq, f_hi / nyq], btype="band", output="sos")
        return sosfilt(sos, x)

    _UDP_TIMEOUT_S = args.lta  # LTA秒数無音でRingをリセット（LTA充填前の誤検出を防ぐ最小単位）

    while not stop_event.is_set():
        time.sleep(0.5)

        # UDPが停止していればRingをリセット（LTA蓄積前の誤検出を防ぐ）
        with shared._lock:
            silent_s = time.time() - shared.last_pkt_wall_time
        if silent_s > _UDP_TIMEOUT_S and shared.fs > 0.0:
            for ring in rings_counts.values():
                ring.buf.clear()
                ring.nsamp = 0
            with shared._lock:
                shared.fs = 0.0

        fs = shared.fs
        if fs <= 0.0:
            continue
        if any(rings_counts[c].nsamp == 0 for c in comps):
            continue

        need = int(args.rt_window * fs)
        need_stalta = max(need, int((args.sta + args.lta) * fs) + 1)
        segs = {}
        full = {}
        skip = False
        for c in comps:
            arr = rings_counts[c].to_array()
            if len(arr) < need:
                skip = True
                break
            segs[c] = arr[-need:]
            full[c] = arr[-need_stalta:] if len(arr) >= need_stalta else arr
        if skip:
            continue

        cZ, cN, cE = comps[0], comps[1], comps[2]

        # STA/LTA: EHZに1-10Hzバンドパスフィルタを適用して計算
        # EHZが十分蓄積していればEHZを使用、不足時はENZ3成分にフォールバック
        def detrend(x):
            return x - np.mean(x)
        ehz_arr = rings_counts["EHZ"].to_array()
        need_ehz = max(need, need_stalta)
        if len(ehz_arr) >= need_stalta:
            ehz_seg = ehz_arr[-need_stalta:]
            ehz_filtered = bandpass_ehz(detrend(ehz_seg), fs)
            vec_stalta = np.abs(ehz_filtered)
            stalta_src = "EHZ"
        else:
            vec_stalta = np.sqrt(detrend(full[cZ]) ** 2 + detrend(full[cN]) ** 2 + detrend(full[cE]) ** 2)
            stalta_src = "ENZ"
        ratio = sta_lta_ratio(vec_stalta, fs, args.sta, args.lta)

        t_now = time.time()
        triggered = ratio >= args.trig and (t_now - last_triggered_time) > args.det_hold

        # JMA フィルタ → 計測震度（ENZ重力バイアス除去のため mean 除去してからフィルタ）
        acc_z = detrend(segs[cZ]) / args.sensitivity
        acc_n = detrend(segs[cN]) / args.sensitivity
        acc_e = detrend(segs[cE]) / args.sensitivity

        az = apply_jma_filter_time(acc_z, fs)
        an = apply_jma_filter_time(acc_n, fs)
        ae = apply_jma_filter_time(acc_e, fs)
        vec = np.sqrt(az ** 2 + an ** 2 + ae ** 2)
        a_mps2 = a_threshold_for_03s(np.abs(vec), fs)
        a_gal = a_mps2 * 100.0
        I_raw = 0.0 if a_gal <= 0 else (2.0 * np.log10(a_gal) + 0.94)
        I_final = float(np.floor(np.round(I_raw, 3) * 100.0) / 100.0)
        scale = jma_scale_from_I(I_final)

        if triggered:
            last_triggered_time = t_now
            ts = datetime.now().strftime("%H:%M:%S")
            # 各要素は [trig_time, trig_ts, trig_ratio, peak_I, peak_scale]。
            # peak_I は発火時点の I_final で初期化し、confirm_window 経過まで毎ループ最大値を更新する。
            pending_queue.append([t_now, ts, ratio, I_final, scale])

        # 確定待ちの全イベントについて、計測震度のピークを更新する。
        # STA/LTA比のピーク（トリガ発火）から 90秒窓の計測震度Iが立ち上がるまで数十秒のラグがあるため、
        # 発火直後の窓値ではなく confirm_window 期間内の最大I値をそのイベントの震度として記録する。
        for ev in pending_queue:
            if I_final > ev[3]:
                ev[3] = I_final
                ev[4] = scale

        # トリガ後 confirm-window 秒経過したイベントを先頭から順に履歴へ記録（読み上げはしない）
        while pending_queue:
            trig_time, trig_ts, trig_ratio, peak_I, peak_scale = pending_queue[0]
            if t_now - trig_time < args.confirm_window:
                break
            pending_queue.popleft()
            shared.add_event(trig_ts, peak_I, peak_scale, trig_ratio)

        # ===== 発話: ライブ計測震度 I_final の監視（STA/LTAトリガとは独立・方式Z）=====
        # 状態機械:
        #   1) I_final が 0.5 を超えたら → ピーク観測を開始（speak_active=True）
        #   2) 観測開始から speak_delay 秒のあいだ毎ループでピークを更新
        #   3) speak_delay 経過時点のピーク値で初回発話
        #   4) 発話後も監視を続け、震度スケールが上がったら言い直す（再生中ならkillして読み直す）
        #   5) I_final が 0.5 を下回ったら状態をリセット（次の揺れに備える）
        if I_final >= _SPEAK_THRESHOLD_I:
            if not speak_active:
                # 0.5超過の立ち上がり: ピーク観測開始
                speak_active = True
                speak_pending_since = t_now
                speak_peak_I = I_final
                speak_peak_scale = scale
            else:
                # 観測中/発話後: ピーク更新
                if I_final > speak_peak_I:
                    speak_peak_I = I_final
                    speak_peak_scale = scale
                if spoken_scale is None:
                    # 初回発話前: speak_delay 経過でピーク値を発話
                    if t_now - speak_pending_since >= args.speak_delay:
                        alert.speak(speak_peak_scale, speak_peak_I, trig_time=speak_pending_since)
                        spoken_I = speak_peak_I
                        spoken_scale = speak_peak_scale
                else:
                    # 発話済み: 震度スケールが上がったら言い直す。
                    # scale は I の単調増加で決まるため、scale が変化しかつ I が上回れば必ず「上昇」。
                    if speak_peak_scale != spoken_scale and speak_peak_I > spoken_I:
                        alert.speak(speak_peak_scale, speak_peak_I, trig_time=speak_pending_since)
                        spoken_I = speak_peak_I
                        spoken_scale = speak_peak_scale
        else:
            # 0.5未満に戻った: 状態リセット（次の揺れで再発話できるように）
            speak_active = False
            speak_pending_since = None
            speak_peak_I = 0.0
            speak_peak_scale = "0"
            spoken_I = 0.0
            spoken_scale = None

        # 推移履歴に追記
        with shared._lock:
            shared.i_history.append(I_final)
            shared.ratio_history.append(ratio)

        # 波形用 raw counts（直近 4000 サンプル = 40秒 @ 100sps、マイクロセイズム帯0.05Hz観測に必要）
        disp_len = 4000
        shared.update(
            fs=fs,
            I_final=I_final,
            a_gal=a_gal,
            scale=scale,
            ratio=ratio,
            triggered=triggered,
            raw_z=segs[cZ][-disp_len:].copy(),
            raw_n=segs[cN][-disp_len:].copy(),
            raw_e=segs[cE][-disp_len:].copy(),
        )


# ===== recv_loop =====
def recv_loop_fn(sock, rings_counts, comps, shared: SharedState,
                 last_t0, stop_event):
    inferred_fs = None
    _last_diag_time = [0.0]

    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        parsed = parse_udp_packet(data)
        if not parsed:
            continue
        ch, t0, vals = parsed
        ch = ch.upper()
        if ch not in rings_counts:
            continue
        recv_wall = time.time()
        rings_counts[ch].extend(vals)
        # パケット末尾サンプルの推定時刻 = t0 + (サンプル数-1)/fs（fsが未確定なら t0 のみ）
        pkt_lag = recv_wall - t0  # 正 = 受信がパケット先頭時刻より遅れている（伝送+処理遅延）
        with shared._lock:
            shared.pkt_count += 1
            shared.last_pkt_wall_time = recv_wall
            shared.pkt_lag = pkt_lag  # 直近パケットのラグを共有

        # fs 推定
        prev = last_t0.get(ch)
        last_t0[ch] = t0
        if prev is not None:
            dt = t0 - prev
            if dt > 0 and len(vals) > 0:
                # dt が異常に大きい = データギャップ。バッファと fs を汚染しないためリセット
                if dt > 3.0:
                    for ring in rings_counts.values():
                        ring.buf.clear()
                        ring.nsamp = 0
                    with shared._lock:
                        shared.fs = 0.0
                    last_t0[ch] = None
                    continue
                fs_est = len(vals) / dt
                with shared._lock:
                    if shared.fs <= 0.0:
                        shared.fs = fs_est
                    else:
                        shared.fs = 0.8 * shared.fs + 0.2 * fs_est

        # 10秒ごとに各チャネルのバッファ蓄積状況を診断ログ出力
        t_now_diag = time.time()
        if t_now_diag - _last_diag_time[0] >= 10.0:
            _last_diag_time[0] = t_now_diag
            nsamp_info = {c: rings_counts[c].nsamp for c in list(comps) + ["EHZ"]}
            with shared._lock:
                fs_now = shared.fs
                lag_now = shared.pkt_lag
            print(
                f"[DIAG] nsamp={nsamp_info}  fs={fs_now:.1f}  pkt_lag={lag_now:.3f}s",
                flush=True,
            )


# ===== main =====
def main():
    ap = argparse.ArgumentParser(description="Raspberry Shake UDP 計測震度 TUI ダッシュボード")
    ap.add_argument("--bind", type=str, default="0.0.0.0:8888")
    ap.add_argument("--channels", type=str, default="ENZ,ENN,ENE")
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
    ap.add_argument("--sta", type=float, default=1.0)
    ap.add_argument("--lta", type=float, default=20.0)
    ap.add_argument("--trig", type=float, default=3.5)
    ap.add_argument("--det-hold", type=float, default=20.0)
    ap.add_argument("--refresh", type=float, default=1.0,
                    help="TUI 更新間隔（秒）")
    args = ap.parse_args()

    comps = [c.strip().upper() for c in args.channels.split(",")]
    if len(comps) != 3:
        raise SystemExit("--channels に3成分を指定してください（例: ENZ,ENN,ENE）")

    host, port_str = args.bind.split(":")
    port = int(port_str)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(1.0)

    max_window = int(max(args.rt_window, args.lta) * 2.0)
    ring_maxlen = max(10_000, max_window * 200)
    # EHZは comps とは別管理（STA/LTA計算用）
    rings_counts = {c: Ring(maxlen_samples=ring_maxlen) for c in comps}
    rings_counts["EHZ"] = Ring(maxlen_samples=ring_maxlen)
    last_t0 = {c: None for c in comps}
    last_t0["EHZ"] = None

    shared = SharedState()
    stop_event = threading.Event()

    alert = AlertSpeaker()
    console_pre = Console()
    console_pre.print(f"[dim]音声エンジン: {alert.engine}[/dim]")

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
        target=p2p_ws_loop,
        args=(shared, stop_event),
        daemon=True,
    )
    recv_th.start()
    compute_th.start()
    p2p_th.start()

    console = Console()
    try:
        with Live(console=console, refresh_per_second=int(1.0 / args.refresh) or 1,
                  screen=True) as live:
            while True:
                state = shared.snapshot()
                live.update(build_display(state, args.station, args.network, args.trig))
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        sock.close()
        console.print("\n[bold green]終了しました。[/bold green]")


if __name__ == "__main__":
    main()
