# rs4d-jma-intensity

Raspberry Shake 4D の UDP ストリームをリアルタイムに受信し、気象庁計測震度を算出・表示するターミナルダッシュボードです。

地震検出時には macOS `say -v Kyoko` による音声アラートを発します（VoiceVox の合成遅延が速報性を損なうため現在は無効化）。

> **注意**: 本ソフトウェアが算出する計測震度は参考値です。公式な震度情報ではありません。

---

## 機能

- Raspberry Shake 4D の DATACAST（UDP）をリアルタイム受信
- 気象庁計測震度アルゴリズム（JMA フィルタ → 0.3 秒閾値 → I = 2log₁₀(a) + 0.94）
- EHZ（速度計）+ ENZ/ENN/ENE（MEMS加速度計）デュアルチャネル方式
  - STA/LTA 検出: EHZ にバンドパスフィルタ（1〜10Hz）を適用して使用（感度向上）
  - I 値計算: ENZ/ENN/ENE 3成分を使用（JMA 定義準拠）
- `rich` ライブラリを使った TUI ダッシュボード（震度バー・波形グラフ・トリガ履歴）
- 震度別音声アラート（macOS `say -v Kyoko` 固定）
- アラート遅延ログ（トリガ検出 → 発話完了までの時間を JSONL に記録）
- P2P地震情報 WebSocket API による最新地震情報・EEW（参考）のリアルタイム表示
- P2P地震情報テーブルの「解析」ボタンによるワンクリック波形解析（analyze_rs.py 連携）
- K-NET / KiK-net 強震波形解析（analyze_knet.py、NIEDの強震記録ASCIIをローカル読み込み）
- マイクロセイズム診断図生成（microseism.py、3成分PSD・H/V比・昼夜比較・帯域パワー時系列を1枚に集約）
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
| `src/microseism.py` | R38DC マイクロセイズム診断図生成スクリプト（3成分PSD・H/V比・帯域パワー時系列・昼夜比較） |
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
pip install numpy scipy obspy rich websocket-client fastapi uvicorn jinja2 matplotlib
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

macOS の `say -v Kyoko` で読み上げます（VoiceVox の合成遅延が速報性を損なうため現在は無効化。再有効化手順は MANUAL.md を参照）。

| 震度 | 警告語 |
|------|--------|
| 1〜2 | 「揺れを検出。」 |
| 3〜4 | 「注意！地震です。」 |
| 5弱・5強 | 「警告！強い地震です。」 |
| 6弱以上 | 「緊急警報！非常に強い地震です。」 |

### K-NET / KiK-net データ謝辞

本ソフトウェアの `src/analyze_knet.py` は、防災科学技術研究所（NIED）が公開する K-NET / KiK-net 強震観測網のデータを利用しています。このデータを利用した成果物（論文・学会発表・報告書等）を公表する場合は、以下の謝辞・DOI を本文の謝辞または Data Availability セクションに明記してください。

**日本語:**
```
本研究では防災科学技術研究所が整備・公開するK-NET・KiK-netの強震観測データを使用した。
防災科学技術研究所 (2019) 防災科研K-NET・KiK-net. doi:10.17598/NIED.0004
```

**English:**
```
Strong-motion data from K-NET and KiK-net were provided by the National Research
Institute for Earth Science and Disaster Resilience (NIED).
National Research Institute for Earth Science and Disaster Resilience (2019)
NIED K-NET, KiK-net. doi:10.17598/NIED.0004
```

- 生成画像のフッターにも DOI を自動記載します。
- データの再配布・販売・改ざんは禁止されています。詳細は [NIED 公開データについて](https://www.kyoshin.bosai.go.jp/ja/about_pubdata/) を参照してください。

---

### 音声クレジット表記

本ソフトウェアの音声アラートは VOICEVOX を利用しています。VoiceVox Engine が起動している環境で本ソフトウェアを利用・配信・配布する場合は、以下のクレジット表記をユーザーが確認可能な場所（README・配信概要欄・アプリ情報画面等）に記載してください。

```
VOICEVOX:青山龍星
```

- 青山龍星はクレジット表記により商用・非商用問わず利用可能です（クレジット非表示の商用利用は別途契約が必要）。
- VoiceVox の他キャラクター（ずんだもん・四国めたん等）に変更する場合は、各キャラクターの利用規約・クレジット表記を必ず確認してください。
- 共通の禁止事項（公序良俗違反、政治・宗教活動、情報商材、フェイク情報、風俗営業、反社会的勢力による利用等）に注意してください。
- 詳細は [VOICEVOX 公式サイト](https://voicevox.hiroshiba.jp/) 内の各キャラクター利用規約を参照してください。

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

Copyright (c) 2026 Masanori Sakai
