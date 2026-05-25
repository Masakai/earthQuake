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
| `src/simulate_udp.py` | 任意震度の合成波形を UDP 送出するシミュレーター |
| `src/verify_filter.py` | JMA フィルタ特性の検証 pytest スイート（41テスト） |
| `src/templates/dashboard.html` | Web ダッシュボード Jinja2 テンプレート |
| `data/R38DC.xml` | StationXML（R38DC 感度情報） |

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

## 詳細

詳しい使い方・オプション・アルゴリズム解説は [MANUAL.md](MANUAL.md) を参照してください。

変更履歴は [CHANGELOG.md](CHANGELOG.md) を参照してください。

---

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
