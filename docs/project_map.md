# earthQuake Project Map

最終更新: 2026-06-16

## 概要

Raspberry Shake 4D (AM.R38DC, 静岡県三島市) のリアルタイム地震観測システム。
UDP パケット受信 → JMA計測震度算出 → Web ダッシュボード表示 を中核に、
波形解析・月次レポート・マイクロセイズム診断などのサブシステムを持つ。

---

## Git / リポジトリ状態

- リモート: `https://github.com/Masakai/earthQuake.git` (origin/master)
- 現バージョン: v1.5.0
- 未コミット: なし
- 未追跡（新規）: なし

---

## ディレクトリ構成

```
earthQuake/
├── src/
│   ├── jma_intensity_web.py        # メインサーバー（FastAPI + WebSocket）
│   ├── jma_intensity_tui.py        # SharedState・計算ループ・音声アラート
│   ├── jma_intensity_realtime.py   # JMAフィルタ・Ring バッファ・震度換算
│   ├── analyze_rs.py               # MiniSEED 波形解析・PNG出力
│   ├── analyze_knet.py             # K-NET ASCII 強震波形解析
│   ├── microseism.py               # マイクロセイズム診断 HTML レポート
│   ├── monthly_report.py           # 月次地震 HTML レポート生成
│   ├── fetch_p2p_daily.py          # P2P地震情報 日次キャッシュ収集
│   ├── run_monthly_report_if_last_day.py  # 月次レポート launchd ラッパー
│   ├── replay_udp.py               # MiniSEED → UDP リプレイ
│   ├── simulate_udp.py             # 合成波形 → UDP シミュレーション
│   ├── verify_filter.py            # JMAフィルタ・震度計算 pytest 検証
│   ├── download_geojson.py         # 地図 GeoJSON ダウンロード
│   └── templates/
│       └── dashboard.html          # フロントエンド（Jinja2 + WebSocket）
├── scripts/
│   ├── start_web.sh                # メイン起動スクリプト（VoiceVox 対応）
│   └── start_web1.sh / start.sh    # 旧バージョン起動スクリプト
├── data/                           # 波形データ・キャッシュ（.gitignore 対象）
│   ├── p2p_cache/YYYYMM.jsonl      # P2P地震情報キャッシュ
│   ├── monthly_report/             # 月次レポート HTML 出力
│   ├── ne/                         # Natural Earth shapefiles
│   ├── knet/                       # K-NET ASCII 波形（手動配置）
│   └── microseism_cache/           # マイクロセイズム解析キャッシュ
├── logs/
│   ├── trigger_log.jsonl           # トリガ検出履歴 {date, ts, I, scale, ratio}
│   ├── fetch_p2p.log               # P2P収集・月次レポートのログ
│   └── web_server.log              # Web サーバーログ
├── docs/
│   ├── project_map.md              # このファイル
│   ├── MANUAL.md                   # 操作マニュアル
│   └── CHANGELOG.md                # バージョン履歴
├── .env                            # 環境変数（STATION_LAT, STATION_LON 等）
└── .claude/                        # Claude Code 設定
```

---

## センサー構成

| チャンネル | 種別 | 用途 |
|---|---|---|
| EHZ | 高感度速度センサー（垂直） | STA/LTA トリガ検出 |
| ENZ / ENN / ENE | MEMS 加速度センサー（3成分） | 合成加速度・JMA計測震度計算 |

- サンプリングレート: EN* = 100 Hz、EHZ = 100 Hz（高感度）
- 感度: 387,867 count/(m/s²)（EN*）

---

## システムアーキテクチャ

```
Raspberry Shake 4D (UDP DATACAST)
        │
        ▼ UDP port 8888
jma_intensity_web.py  ←→  jma_intensity_tui.py (SharedState)
        │                         │
        │              jma_intensity_realtime.py
        │              （Ring バッファ・JMAフィルタ・STA/LTA）
        │
        ├── WebSocket → dashboard.html（ブラウザ）
        └── HTTP REST → /api/status, /api/config 等
```

---

## ソースファイル詳細

### jma_intensity_web.py（662行）

**役割**: FastAPI + WebSocket サーバー本体。UDP受信・計算・ブラウザ配信を統合。

**主要関数**:
| 関数 | 処理 |
|---|---|
| `recv_and_compute_loop()` | UDP受信 → Ring バッファ蓄積 → STA/LTA・震度計算 → SharedState 更新 |
| `broadcast_loop()` | 1秒ごとに WebSocket で全クライアントへ状態配信 |
| `fetch_p2p_quakes()` | P2P地震情報 API から最新50件取得 |
| `GET /` | dashboard.html を Jinja2 レンダリングして返す |
| `WebSocket /ws` | クライアントとの双方向通信 |
| `POST /api/config` | STA/LTA閾値・窓幅等のパラメータをランタイム変更 |
| `POST /api/analyze` | 指定イベントの波形解析（analyze_rs.py 呼び出し） |

**入出力**:
- 入力: UDP port 8888（RS DATACAST 形式）
- 出力: WebSocket JSON、HTTP レスポンス
- ログ: `logs/trigger_log.jsonl`（トリガ検出時に追記）
- 設定: `.env`（STATION_LAT, STATION_LON 等）

**CLI引数**: `--station`, `--bind`, `--channels`, `--sta`, `--lta`, `--trig`, `--det-hold`, `--confirm-window`, `--web-port`, `--rt-window`

---

### jma_intensity_tui.py（985行）

**役割**: 共有状態管理・計算ループ・音声アラートを担うコアモジュール。

**主要クラス・関数**:
| 名前 | 役割 |
|---|---|
| `SharedState` | スレッド安全な状態コンテナ（震度・STA/LTA・履歴・設定） |
| `SharedState.add_event()` | トリガ検出イベントを履歴に追加、`trigger_log.jsonl` に追記 |
| `SharedState.load_event_log()` | 起動時に `trigger_log.jsonl` から過去50件読み込み |
| `Ring` | 固定長サンプルバッファ（チャンネル別リングバッファ） |
| `AlertSpeaker` | VoiceVox / macOS say による音声読み上げ |
| `compute_loop()` | STA/LTA 計算・震度計算・閾値判定ループ |
| `recv_loop_fn()` | UDP パケット受信・デコード・Ring バッファ書き込み |

**trigger_log.jsonl フォーマット**:
```json
{"date": "2026-05-24", "ts": "09:32:59", "I": 0.06, "scale": "0", "ratio": 4.12}
```

---

### jma_intensity_realtime.py（353行）

**役割**: JMA計測震度フィルタの実装とリアルタイム計算のコア関数群。

**主要関数**:
| 関数 | 処理 |
|---|---|
| `jma_frequency_response(freqs)` | JMA告示フィルタの周波数応答を返す |
| `apply_jma_filter_time(data, fs)` | 時系列データに JMA フィルタを適用（FFT → 周波数応答乗算 → IFFT） |
| `jma_scale_from_I(I)` | 計測震度値 → 震度階級文字列（"0"〜"7"） |
| `Ring` | 固定長リングバッファクラス |

**JMA震度階マッピング**:
`10→1, 20→2, 30→3, 40→4, 45→5弱, 50→5強, 55→6弱, 60→6強, 70→7`（×10整数コード）

---

### analyze_rs.py（1013行）

**役割**: MiniSEED 波形ファイルを読み込み、4パネル解析グラフ（スペクトログラム・合成加速度・STA/LTA・計測震度）を PNG 出力。

**主要関数**:
| 関数 | 処理 |
|---|---|
| `compute_stalta(vec, fs, sta_s, lta_s)` | STA/LTA 比計算（EHZ 優先、フォールバック EN* 合成） |
| `compute_intensity_timeseries(a_comb, fs)` | JMAフィルタ適用 → 0.3秒窓での計測震度時系列計算 |
| `compute_spectrogram(data, fs)` | スペクトログラム計算（scipy.signal.spectrogram） |
| `plot_map(ax, sta_lat, sta_lon, quake_info)` | 観測点・震源地図描画（Natural Earth shapefiles 使用） |
| `plot_analysis(...)` | 4パネル複合グラフ描画・PNG保存 |
| `main()` | MiniSEED 読込・チャンネル分離・解析実行 |

**STA/LTA チャンネル選択ロジック**:
- EHZ が利用可能 → EHZ の絶対値で STA/LTA 計算
- EHZ なし → EN* 3成分の合成ベクトルで計算

**入出力**:
- 入力: `data/AM.R38DC.00.*.ms` または FDSN Webサービス
- 出力: `data/analyze_*.png`

**CLI引数**: `--files`, `--sta`, `--lta`, `--trig`, `--marker`, `--quake`, `--out`, `--title`

---

### monthly_report.py（736行）

**役割**: P2P地震情報から指定年月の地震データを集計し、震源地図・統計・定型解説・地震一覧を含む HTML レポートを生成。自局検出との照合も実施。

**主要関数**:
| 関数 | 処理 |
|---|---|
| `load_trigger_hhmm_set(year, month)` | `trigger_log.jsonl` から指定月の検出時刻を `"YYYY-MM-DD HH:MM"` セットで返す |
| `_load_from_cache(year, month)` | `data/p2p_cache/YYYYMM.jsonl` からキャッシュ読み込み |
| `_fetch_from_api(year, month)` | P2P API から直接取得（キャッシュなし時フォールバック） |
| `fetch_p2p(year, month)` | キャッシュ優先でデータ取得 |
| `compute_stats(quakes)` | 総件数・最大M・最大震度・地域別集計・群発検出等の統計計算 |
| `make_epicenter_map(quakes, year, month)` | geopandas + Natural Earth で震源分布図生成（base64 PNG） |
| `make_daily_chart()` / `make_mag_chart()` | 日別件数・M分布グラフ生成（base64 PNG） |
| `generate_commentary(quakes, stats, year, month)` | 定型テンプレートによる文章解説生成（全体像・注目イベント・地域別・総括） |
| `make_table(quakes, detected_hhmm)` | 地震一覧テーブル HTML 生成。自局検出列（📡）付き |
| `build_html(...)` | 全セクションを組み合わせて HTML 文字列生成 |
| `main()` | CLI エントリポイント |

**自局検出照合**:
- `trigger_log.jsonl` の `date + HH:MM` と P2P の `YYYY/MM/DD HH:MM` を HH:MM 単位で照合
- 一致した地震に 📡 マークを表示、サマリーに自局検出件数を表示

**入出力**:
- 入力: `data/p2p_cache/YYYYMM.jsonl`（優先）または P2P API
- 入力: `logs/trigger_log.jsonl`（自局検出照合用）
- 出力: `data/monthly_report/report_YYYYMM.html`

**CLI引数**: `year`（省略時: 当月）, `month`（省略時: 当月）

---

### fetch_p2p_daily.py（160行）

**役割**: P2P地震情報 API から当日分のデータを取得し `data/p2p_cache/YYYYMM.jsonl` に追記。launchd から毎日 03:00 に実行。

**キャッシュ JSONL フォーマット**:
```json
{"id": "...", "time": "2026/05/24 09:32", "year": 2026, "month": 5, "day": 24,
 "name": "千葉県北東部", "lat": 35.7, "lon": 140.8, "mag": 3.9, "depth": 30, "scale": 30}
```

---

### run_monthly_report_if_last_day.py（61行）

**役割**: 毎月 1 日のみ、前月分の `monthly_report.py` を実行する launchd ラッパー。

**実行フロー**: `今日が1日？ → NO: exit / YES: 前月(year, month)を計算 → monthly_report.py を subprocess 実行`

---

### microseism.py（1554行）

**役割**: EN* 3成分からマイクロセイズム（1〜9秒周期の海洋性地震動）を診断する HTML レポート生成ツール。ObsPy で計器応答除去。

**主な出力パネル**: スペクトログラム・平均PSD・帯域エネルギー重心・H/V比・帯域パワー時系列・昼夜比較

**入出力**:
- 入力: RS FDSN API（ObsPy 経由）またはキャッシュ `data/microseism_cache/`
- 出力: `data/` に複数 PNG + HTML レポート

---

### analyze_knet.py（608行）

**役割**: NIED K-NET/KiK-net 強震波形（ASCII）を解析し、analyze_rs.py と同形式の解析グラフを生成。

**入出力**:
- 入力: `data/knet/{station}{YYMMDDHHmm}.{NS|EW|UD}`（手動配置）
- 出力: `data/knet/analysis_knet_{station}_{event}.png`

---

### replay_udp.py / simulate_udp.py

**役割**: テスト・デバッグ用 UDP 送出ツール。

| スクリプト | 入力 | 出力 |
|---|---|---|
| `replay_udp.py` | MiniSEED ファイル | UDP パケット（実波形リプレイ） |
| `simulate_udp.py` | 目標震度値 | UDP パケット（合成正弦波） |

---

### verify_filter.py（157行）

**役割**: `pytest` による JMA フィルタ・計測震度計算の単体テスト。

**実行**: `source .venv/bin/activate && pytest src/verify_filter.py -v`

---

## launchd 自動実行スケジュール

| plist ファイル | 実行時刻 | スクリプト | 処理 |
|---|---|---|---|
| `net.r38dc.fetch_p2p_daily.plist` | 毎日 03:00 | `fetch_p2p_daily.py` | 当月 P2P データをキャッシュに追記 |
| `net.r38dc.monthly_report.plist` | 毎日 05:00 | `run_monthly_report_if_last_day.py` | 毎月1日のみ前月分レポート生成 |

plist 保存場所: `~/Library/LaunchAgents/`

---

## データフロー

```
P2P地震情報 API
    │
    ├─ fetch_p2p_daily.py (毎日03:00)
    │       └─ data/p2p_cache/YYYYMM.jsonl
    │
    └─ monthly_report.py (毎月1日05:00)
            ├─ キャッシュ読み込み
            ├─ logs/trigger_log.jsonl（自局検出照合）
            └─ data/monthly_report/report_YYYYMM.html

Raspberry Shake UDP
    └─ jma_intensity_web.py
            ├─ logs/trigger_log.jsonl（トリガ記録）
            └─ WebSocket → dashboard.html（ブラウザ）

MiniSEED ファイル (data/)
    ├─ analyze_rs.py → data/analyze_*.png
    └─ replay_udp.py → UDP（テスト用）
```

---

## 主要な設定値・定数

| 項目 | デフォルト値 | 場所 |
|---|---|---|
| STA 窓 | 1.0 秒 | `--sta` CLI引数 |
| LTA 窓 | 10.0 秒 | `--lta` CLI引数 |
| トリガ閾値 | 3.5 | `--trig` CLI引数（UI で可変） |
| 確認窓 | 2.0 秒 | `--confirm-window` |
| Web ポート | 8080 | `--web-port` |
| UDP ポート | 8888 | `--bind` |
| P2P 取得件数 | 50件 | `jma_intensity_web.py` |
| トリガ履歴保持 | 50件 | `load_event_log(limit=50)` |
