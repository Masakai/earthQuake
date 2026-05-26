# rs4d-jma-intensity

Raspberry Shake 4D の UDP ストリームをリアルタイムに受信し、気象庁計測震度を算出・表示するターミナルダッシュボードです。

地震検出時には VoiceVox（または macOS say）による音声アラートを発します。

> **注意**: 本ソフトウェアが算出する計測震度は参考値です。公式な震度情報ではありません。

---

## 機能

- Raspberry Shake 4D の DATACAST（UDP）をリアルタイム受信
- 気象庁計測震度アルゴリズム（JMA フィルタ → 0.3 秒閾値 → I = 2log₁₀(a) + 0.94）
- STA/LTA による地震検出
- `rich` ライブラリを使った TUI ダッシュボード（震度バー・波形グラフ・トリガ履歴）
- 震度別音声アラート（VoiceVox 優先、未起動時は macOS say にフォールバック）
- P2P地震情報 WebSocket API による最新地震情報・EEW（参考）のリアルタイム表示
- P2P地震情報テーブルの「解析」ボタンによるワンクリック波形解析（analyze_rs.py 連携）
- K-NET / KiK-net 強震波形解析（analyze_knet.py、NIEDの強震記録ASCIIをローカル読み込み）
- UDP シミュレーター（実機なしでのテスト用）
- JMA フィルタ検証スクリプト

---

## ファイル構成

| ファイル | 説明 |
|---------|------|
| `src/jma_intensity_tui.py` | メイン TUI ダッシュボード |
| `src/jma_intensity_web.py` | Web ダッシュボード（FastAPI + Jinja2） |
| `src/jma_intensity_realtime.py` | JMA 計測震度コアライブラリ |
| `src/analyze_rs.py` | P2P地震情報・波形解析スクリプト（FDSN波形取得・スペクトログラム） |
| `src/analyze_knet.py` | K-NET / KiK-net 強震波形解析スクリプト（NIED ASCIIをローカル読み込み・計測震度算出） |
| `src/simulate_udp.py` | 任意震度の合成波形を UDP 送出するシミュレーター |
| `src/verify_filter.py` | JMA フィルタ特性の検証 pytest スイート（41テスト） |
| `src/templates/dashboard.html` | Web ダッシュボード Jinja2 テンプレート |
| `src/download_geojson.py` | 国土数値情報から市区町村GeoJSONをダウンロード・変換するスクリプト |
| `data/R38DC.xml` | StationXML（R38DC 感度情報） |
| `data/geojson/` | 市区町村別GeoJSONファイル（下記参照） |

---

## 市区町村 GeoJSON データについて

`data/geojson/` 以下に格納されている市区町村の境界ポリゴンデータは、国土交通省が公開する **国土数値情報（行政区域データ N03）** を加工して作成しています。

**出典:** 国土交通省国土数値情報ダウンロードサイト  
https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-2024.html

**ライセンス:** 公共データ利用規約（PDL1.0）— 出典記載のうえ商用・非商用を問わず利用可

**加工内容:**  
元データ（都道府県単位の GeoJSON）を市区町村コード（`N03_007`）ごとに分割し、1 市区町村 = 1 ファイル（`{都道府県コード2桁}/{市区町村コード5桁}.json`）として保存しています。座標・属性値（`N03_001` 都道府県名、`N03_004` 市区町村名等）は原データのままで変更していません。

分割作業は `src/download_geojson.py` で自動化されており、同スクリプトを実行すると全 47 都道府県・1905 市区町村のファイルが再生成されます。

---

## 必要環境

- macOS（音声アラートに `afplay` / `say` を使用）
- Python 3.10 以上
- VoiceVox Engine（任意、未起動時は macOS say にフォールバック）

---

## インストール

```bash
git clone <repository-url>
cd earthQuake
python3 -m venv .venv
source .venv/bin/activate
pip install numpy obspy rich websocket-client fastapi uvicorn jinja2
```

---

## クイックスタート

### 1. Raspberry Shake 側の設定

Shake の Web 設定画面で UDP/DATACAST を有効化し、送信先 IP とポート（既定: 8888）を設定します。

### 2. TUI 起動

```bash
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC
```

### 3. シミュレーターでテスト（実機なし）

ターミナル 1 でTUI を起動：
```bash
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC --rt-window 5 --lta 10 --bind 127.0.0.1:9999
```

ターミナル 2 でシミュレーターを起動：
```bash
.venv/bin/python3 src/simulate_udp.py --intensity 3.0 --duration 60 --quiet-sec 20 --dest 127.0.0.1:9999
```

---

## 音声アラート

VoiceVox Engine（`http://localhost:50021`）が起動していれば自動検出し、No.7（アナウンス）で読み上げます。未起動の場合は macOS の `say -v Kyoko` にフォールバックします。

| 震度 | 警告語 |
|------|--------|
| 1〜2 | 「揺れを検出。」 |
| 3〜4 | 「注意！地震です。」 |
| 5弱・5強 | 「警告！強い地震です。」 |
| 6弱以上 | 「緊急警報！非常に強い地震です。」 |

---

## 計測震度アルゴリズムの正当性検証

本ソフトウェアの計測震度算出ロジックを、防災科研（NIED）が公開する K-NET 強震記録の公式統計値と照合しました。

**検証イベント:** 2026年5月20日 11:46（震源 27.500°N, 128.600°E、深さ 50 km、M5.9）

| 観測点 | 震央距離 | 公式値 (NIED) | 解析値 (本ソフト) | 一致度 |
|---|---|---|---|---|
| KGS035（与論） | 53 km | 312.3 gal / I=5.0 | 293.9 gal / I=5.08 | 震度階級完全一致 |
| KGS034（知名） | 19 km | 150.0 gal / I=4.2 | 147.4 gal / I=4.29 | 震度階級完全一致 |

加速度の差（−6%、−2%）は、公式値が成分別最大、本ソフトが3成分ベクトル合成最大という算出手法の違いによります。計測震度値は両観測点とも公式値と +0.08 〜 +0.09 の範囲で一致しており、JMA 計測震度の階級（震度3〜5強）と完全に一致しました。

**検証された範囲:**
- `src/jma_intensity_realtime.py` の JMA フィルタ（`apply_jma_filter_time`）と震度計算（`jma_scale_from_I`）
- `src/analyze_rs.py` の `compute_intensity_timeseries`（0.3秒持続値抽出）
- `src/analyze_knet.py` の単位変換・3成分ベクトル合成・震源距離計算

**検証範囲外（参考値として扱うべき機能）:**
- リアルタイム UDP 受信（TUI/Web）、STA/LTA 検出、P2P 地震情報表示、UDP シミュレーター
- 上記検証範囲外の震度（震度6弱以上・震度2以下）、震央距離100km超

検証手順の詳細は [MANUAL.md](docs/MANUAL.md) を参照してください。

---

## 詳細

詳しい使い方・オプション・アルゴリズム解説は [MANUAL.md](docs/MANUAL.md) を参照してください。

変更履歴は [CHANGELOG.md](docs/CHANGELOG.md) を参照してください。

---

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
