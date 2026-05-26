# 使用マニュアル

## 目次

1. [インストール](#1-インストール)
2. [ファイル構成](#2-ファイル構成)
3. [Raspberry Shake の設定](#3-raspberry-shake-の設定)
4. [起動スクリプト](#4-起動スクリプト)
5. [TUI ダッシュボード](#5-tui-ダッシュボード)
6. [Web ダッシュボード](#6-web-ダッシュボード)
7. [シミュレーター](#7-シミュレーター)
8. [音声アラート](#8-音声アラート)
9. [P2P地震情報・EEW 表示](#9-p2p地震情報eew-表示)
10. [フィルタ検証](#10-フィルタ検証)
11. [アルゴリズム解説](#11-アルゴリズム解説)
12. [計測震度算出ロジックの正当性検証](#12-計測震度算出ロジックの正当性検証)
13. [K-NET / KiK-net 強震波形解析](#13-k-net--kik-net-強震波形解析)
14. [トラブルシューティング](#14-トラブルシューティング)

---

## 1. インストール

### 必要環境

- macOS（afplay / say コマンドを使用）
- Python 3.10 以上
- VoiceVox Engine（任意）

### 手順

```bash
git clone https://github.com/Masakai/earthQuake.git
cd earthQuake
python3 -m venv .venv
source .venv/bin/activate

# TUI 版のみ使う場合
pip install numpy scipy obspy rich websocket-client jinja2 matplotlib

# Web 版も使う場合
pip install numpy scipy obspy rich websocket-client fastapi uvicorn jinja2 matplotlib
```

---

## 2. ファイル構成

```
earthQuake/
├── README.md
├── data/
│   └── R38DC.xml              # StationXML（R38DC 感度情報）
├── src/                       # Python ソース
│   ├── jma_intensity_tui.py   # TUI ダッシュボード
│   ├── jma_intensity_web.py   # Web ダッシュボード
│   ├── jma_intensity_realtime.py  # JMA 計測震度コアライブラリ
│   ├── simulate_udp.py        # UDP シミュレーター
│   ├── analyze_rs.py          # 波形後処理解析・グラフ生成（MiniSEED ダウンロード）
│   ├── analyze_knet.py        # K-NET / KiK-net 強震波形解析（NIED ASCIIをローカル読み込み）
│   ├── microseism.py          # マイクロセイズム診断図生成（3成分PSD・H/V比・昼夜比較）
│   ├── verify_filter.py       # JMA フィルタ検証（pytest スイート、41テスト）
│   └── templates/             # Jinja2 HTML テンプレート
│       └── dashboard.html     # Web ダッシュボード HTML（Jinja2）
├── scripts/                   # 起動スクリプト
│   ├── start.sh               # TUI 起動（VoiceVox 自動起動付き）
│   └── start_web.sh           # Web 起動（VoiceVox 自動起動付き）
└── docs/                      # ドキュメント・GitHub Pages
    ├── index.html
    ├── guide.html
    ├── stalta_explainer.html
    ├── MANUAL.md
    └── CHANGELOG.md
```

---

## 3. Raspberry Shake の設定

Shake の Web 設定画面（通常 `http://<shake-ip>:7000`）で以下を設定します。

1. **Station View → Settings → Datacast** を開く
2. **UDP DATACAST** を有効化
3. 送信先 IP に本スクリプトを実行するマシンの IP を入力
4. ポートを `8888`（既定）に設定
5. 設定を保存・再起動

送信フォーマット例：
```
{'ENZ', 1700000000.123, 3803600,3803601,...}
```

---

## 4. 起動スクリプト

VoiceVox の自動起動と依存パッケージ確認をまとめた便利スクリプトです。

### TUI 版

```bash
bash scripts/start.sh --station R38DC
```

### Web 版

```bash
bash scripts/start_web.sh --station R38DC
```

スクリプトは `--station` 以外の引数もそのまま各プログラムへ渡します。

```bash
# 例: Web 版をポート 8081 で起動
bash scripts/start_web.sh --station R38DC --web-port 8081
```

---

## 5. TUI ダッシュボード

### 基本起動

```bash
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC
```

`--station` は必須です。RS4D のステーションコードを指定してください。

### 全オプション

| オプション | 既定値 | 説明 |
|-----------|--------|------|
| `--station` | （必須） | ステーションコード（例: R38DC） |
| `--bind` | `0.0.0.0:8888` | 受信アドレス:ポート |
| `--channels` | `ENZ,ENN,ENE` | 3 成分チャンネル（カンマ区切り） |
| `--network` | `AM` | ネットワークコード |
| `--sensitivity` | `387867.0` | 感度値 counts/(m/s²) |
| `--rt-window` | `90.0` | 計測震度の計算窓長 [秒] |
| `--sta` | `1.0` | STA 窓長 [秒] |
| `--lta` | `20.0` | LTA 窓長 [秒] |
| `--trig` | `3.5` | STA/LTA トリガしきい値 |
| `--det-hold` | `20.0` | トリガ後の再検出抑制時間 [秒] |
| `--refresh` | `1.0` | TUI 更新間隔 [秒] |

### 実行例

LTA 窓を長くして誤検知を減らす：
```bash
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC --lta 30 --trig 4.0
```

別ポートで受信（シミュレーター用）：
```bash
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC --rt-window 5 --lta 10 --bind 127.0.0.1:9999
```

### 感度値について

R38DC の感度値（実測）: **387,867 counts/(m/s²)**  
公式 V6 仕様値: 384,500 counts/(m/s²)

実機の感度値が異なる場合は `--sensitivity` で指定してください。

---

## 6. Web ダッシュボード

ブラウザから確認できる HTTP ダッシュボードです。Leaflet 地図・Chart.js グラフ・WebSocket リアルタイム更新に対応しています。

### 基本起動

```bash
.venv/bin/python3 src/jma_intensity_web.py --station R38DC
```

起動後、ブラウザで `http://localhost:8080` を開きます。

### 全オプション

| オプション | 既定値 | 説明 |
|-----------|--------|------|
| `--station` | （必須） | ステーションコード（例: R38DC） |
| `--bind` | `0.0.0.0:8888` | RS4D UDP 受信アドレス:ポート |
| `--channels` | `ENZ,ENN,ENE` | 3 成分チャンネル（カンマ区切り） |
| `--network` | `AM` | ネットワークコード |
| `--sensitivity` | `387867.0` | 感度値 counts/(m/s²) |
| `--rt-window` | `90.0` | 計測震度の計算窓長 [秒] |
| `--sta` | `1.0` | STA 窓長 [秒] |
| `--lta` | `20.0` | LTA 窓長 [秒] |
| `--trig` | `3.5` | STA/LTA トリガしきい値 |
| `--det-hold` | `20.0` | トリガ後の再検出抑制時間 [秒] |
| `--web-port` | `8080` | Web サーバーのポート番号 |
| `--web-bind` | `127.0.0.1` | Web サーバーのバインドアドレス |

### 実行例

外部からアクセスできるようにバインド：
```bash
.venv/bin/python3 src/jma_intensity_web.py --station R38DC --web-bind 0.0.0.0
```

ポート変更：
```bash
.venv/bin/python3 src/jma_intensity_web.py --station R38DC --web-port 8081
```

### TUI と Web の同時起動について

同一マシンで TUI と Web を同時に起動する場合、UDP ポートが競合します。Raspberry Shake の DATACAST 送信先を 2 エントリ登録し、各プロセスで別ポートを指定してください。

```bash
# TUI: ポート 8889 で受信
.venv/bin/python3 src/jma_intensity_tui.py --station R38DC --bind 0.0.0.0:8889

# Web: ポート 8888 で受信（別ターミナル）
.venv/bin/python3 src/jma_intensity_web.py --station R38DC --bind 0.0.0.0:8888
```

---

## 7. シミュレーター

実機なしで任意の震度を再現した UDP パケットを送出します。TUI・Web の動作確認に使用します。

### 全オプション

| オプション | 既定値 | 説明 |
|-----------|--------|------|
| `--intensity` | `3.0` | 目標計測震度 |
| `--duration` | `60.0` | 地震信号の送出時間 [秒] |
| `--quiet-sec` | `0.0` | 地震前の静穏期間 [秒]（LTA 安定化に使用） |
| `--dest` | `127.0.0.1:8888` | 送出先アドレス:ポート |
| `--f0` | `1.0` | 正弦波の周波数 [Hz] |
| `--fs` | `100.0` | サンプリング周波数 [Hz] |
| `--pkt-samples` | `25` | 1 パケットのサンプル数 |
| `--noise-ratio` | `0.05` | 信号振幅に対するノイズ比率 |
| `--sensitivity` | `387867.0` | 感度値 counts/(m/s²) |

### 実行例

震度 3 をシミュレート（静穏 20 秒 + 地震 60 秒）：
```bash
.venv/bin/python3 src/simulate_udp.py \
  --intensity 3.0 \
  --duration 60 \
  --quiet-sec 20 \
  --dest 127.0.0.1:9999
```

震度 5 弱をシミュレート：
```bash
.venv/bin/python3 src/simulate_udp.py \
  --intensity 5.1 \
  --duration 60 \
  --quiet-sec 20 \
  --dest 127.0.0.1:9999
```

### 注意事項

- `--quiet-sec` は STA/LTA の LTA バッファを安定させるために必要です。`--lta` の値以上を推奨します。
- TUI の `--rt-window` より短い `--quiet-sec` だと、トリガ時点でまだ静穏期間データが窓に残り、I 値が低めに算出されます。
- Z 成分（ENZ）のみに信号を入れ、N/E はノイズのみです。

---

## 8. 音声アラート

### VoiceVox

起動時に `http://localhost:50021` の VoiceVox Engine を自動検出します。起動していれば **No.7（アナウンス、速度 1.1 倍）** で読み上げます。

VoiceVox のインストール・起動は [VoiceVox 公式サイト](https://voicevox.hiroshiba.jp/) を参照してください。

### フォールバック

VoiceVox が未起動の場合は macOS の `say -v Kyoko` で読み上げます。

### 読み上げ内容

震度検出時の発話例（震度 3 の場合）：
```
注意！地震です。震度3。計測震度3点00。落下物などに気をつけてください。
```

| 震度 | 冒頭警告語 | 注意喚起メッセージ |
|------|-----------|-----------------|
| 1〜2 | 揺れを検出。 | 周囲の状況を確認してください。 |
| 3〜4 | 注意！地震です。 | 落下物などに気をつけてください。 / 不安定な場所から離れてください。 |
| 5弱・5強 | 警告！強い地震です。 | 今すぐ身を守ってください。 |
| 6弱・6強・7 | 緊急警報！非常に強い地震です。 | 今すぐ安全な場所に避難してください。 |

---

## 9. P2P地震情報・EEW 表示

起動時に [P2P地震情報 WebSocket API](https://www.p2pquake.net/develop/json_api_v2/) へ自動接続し、以下の情報をリアルタイムで受信します。

| 情報 | 内容 |
|------|------|
| 地震情報 (code 551) | 直近5件の震源・M・最大震度・津波情報 |
| EEW（code 556） | 震源・M・最大予測震度（参考値） |

### EEW について

- P2P地震情報経由の EEW は**内容・配信品質ともに無保証**です。参考情報に留めてください。
- 音声アラートは EEW に対しては発声しません（RS4D 自身の計測震度が確定した際にのみ発声します）。

### フォールバック

`websocket-client` がインストールされていない場合は、60 秒間隔の HTTP ポーリングに自動フォールバックします。EEW 表示はフォールバック時は利用できません。

---

## 10. フィルタ検証

JMA フィルタの実装が正しいことを確認する pytest スイートです。

```bash
.venv/bin/pytest src/verify_filter.py -v
```

41テストで以下を検証します：
1. フィルタ振幅特性（各周波数で理論値と 5% 以内の誤差）
2. DC 成分の除去（f=0 → 出力≈0）
3. 計測震度の逆算（目標 I 値から設計した振幅で計算値が一致）
4. 0.3 秒閾値の動作（定常波・短スパイク・長スパイク）
5. `jma_scale_from_I` の境界値（21ケース）
6. `compute_intensity_timeseries` と realtime の出力一致（I=2,3,4 の3ケース）

---

## 11. アルゴリズム解説

### JMA 計測震度の算出手順

気象庁「計測震度の算出方法」に基づきます。

**Step 1: 加速度データの取得**

RS4D の DATACAST パケットから 3 成分（Z/N/E）の counts データを受信し、感度値で除算して加速度 [m/s²] に変換します。

**Step 2: JMA フィルタの適用**

周波数領域で以下の応答 H(f) を乗算します：

```
H(f) = FL(f) × FH(f) × FF(f)

FL(f) = √(1 - exp(-(f/0.5)³))          # ハイパス
FH(f) = (1 + 0.694y² + 0.241y⁴ + ...)⁻¹/² (y=f/10)  # ローパス
FF(f) = 1/√f                             # 速度比例補正
```

**Step 3: 3 成分合成と 0.3 秒閾値**

フィルタ後の 3 成分を二乗和平方根で合成し、各時刻における過去の波形を大きい順に並べたとき **合計して 0.3 秒分（30 サンプル @ 100Hz）を超える最大値** a [m/s²] を求めます（気象庁公式定義）。

**Step 4: 計測震度の算出**

```
I = 2 × log₁₀(a × 100) + 0.94
```

（a を gal 換算してから対数を取ります）

### STA/LTA 検出

STA（短時間平均）と LTA（長時間平均）のパワー比でトリガします。

```
ratio = mean(x[-nSTA:]²) / mean(x[-nLTA:-nSTA]²)
```

`ratio >= trig`（既定 3.5）かつ `det_hold` 秒以上経過していればトリガします。

### トリガ後の確定 I 値記録

トリガ発火直後は計算窓に静穏期間のデータが残っているため、I 値が低めになります。そのため、トリガ発火から `rt-window` 秒後に窓が地震データで満たされた時点の I 値をトリガ履歴に記録します。

---

## 12. 計測震度算出ロジックの正当性検証

本ソフトウェアの計測震度算出ロジックを、防災科研（NIED）が公開する K-NET 強震記録の公式統計値と照合した結果を以下に示します。

### 検証イベント

| 項目 | 値 |
|------|----|
| 発生日時 | 2026年5月20日 11:46:00 JST |
| 震源緯度 | 27.500°N |
| 震源経度 | 128.600°E |
| 震源深さ | 50 km |
| マグニチュード | M5.9 |
| 観測点数（NIED集計） | 12局 |
| 最大加速度（NIED公式値） | 312.3 gal |
| 計測震度（NIED公式値・最大） | 5.0 |

### 検証データ

NIED 強震観測網（K-NET）の ASCII 強震記録（3成分: NS, EW, UD）を `data/knet/` 配下に配置し、`src/analyze_knet.py` で読み込んで計測震度を算出。NIED 震度データベース公開値と比較しました。

### 検証結果

| 観測点 | 震央距離 | 最大加速度 (NIED公式) | 最大加速度 (本ソフト) | 加速度差 | 計測震度 (NIED公式) | 計測震度 (本ソフト) | 震度階級 |
|--------|---------|----------------------|----------------------|---------|---------------------|---------------------|---------|
| KGS035（与論） | 53 km | 312.3 gal | 293.9 gal | −5.9% | 5.0 | 5.08 | **完全一致**（5強） |
| KGS034（知名） | 19 km | 150.0 gal | 147.4 gal | −1.7% | 4.2 | 4.29 | **完全一致**（4） |

### 加速度値の差について

加速度値の小さな差（−6%、−2%）は、算出手法の違いに起因します。

- **NIED 公式値**: 3成分（NS, EW, UD）それぞれの最大値のうち最大のもの（成分別最大）
- **本ソフトウェア**: 3成分のベクトル合成波形（√(NS² + EW² + UD²)）の最大値

成分別最大は瞬時値の単純最大、ベクトル合成は同時刻の3成分の合成値なので、必ずしも一致しません。計測震度算出に使うのは JMA 公式定義通り **ベクトル合成波形に JMA フィルタを掛けた後の 0.3 秒持続値** であり、加速度の単純比較ではないため、計測震度値が公式値とほぼ一致していることが本質的な検証結果です。

### 検証された範囲

以下のコンポーネントが NIED 公式値と一致することを確認:

| ファイル | 関数 | 内容 |
|---------|------|------|
| `src/jma_intensity_realtime.py` | `apply_jma_filter_time` | JMA 周波数フィルタ（時間領域実装） |
| `src/jma_intensity_realtime.py` | `jma_scale_from_I` | 計測震度 I → 震度階級変換 |
| `src/analyze_rs.py` | `compute_intensity_timeseries` | 0.3秒持続値の最大化処理 |
| `src/analyze_knet.py` | `load_knet_traces` | K-NET ASCII の counts → gal 単位変換 |
| `src/analyze_knet.py` | （メイン処理） | 3成分ベクトル合成・震源距離計算 |

### 検証範囲外（参考値として扱うべき機能・条件）

以下の機能・条件は本検証では確認されていません。参考値として扱ってください。

- **動作モード**:
  - リアルタイム UDP 受信処理（TUI / Web ダッシュボード）
  - STA/LTA 自動検出ロジック
  - P2P 地震情報 / EEW 表示
  - UDP シミュレーター（合成波形）
- **震度範囲**:
  - 震度2以下（小さい揺れ）の精度
  - 震度6弱以上（強い揺れ）の精度
- **距離範囲**:
  - 震央距離 100 km 超の遠地イベント
- **観測条件**:
  - Raspberry Shake 4D 自体のセンサー特性・感度値の正確性
  - 観測点直下の地盤特性（堆積層による増幅・反射）

これらの条件下では、本検証結果（公式値±0.1 で一致）の精度が保証されません。

### 再現手順

```bash
# K-NET データを data/knet/ に配置（NIED から手動取得）
ls data/knet/KGS035*.{NS,EW,UD}

# 解析実行
.venv/bin/python3 src/analyze_knet.py --station KGS035 --event 2026-05-20-11-46
```

出力された PNG/SVG の計測震度値を NIED 震度データベース（https://www.kyoshin.bosai.go.jp/）の公開値と比較してください。

---

## 13. K-NET / KiK-net 強震波形解析

`src/analyze_knet.py` は NIED の強震観測網（K-NET / KiK-net）が公開する ASCII 強震記録を読み込み、計測震度・スペクトログラム・震源マップ等を出力します。

### データの取得

NIED 強震観測網ホームページ（https://www.kyoshin.bosai.go.jp/）から、対象イベント・観測点の ASCII 強震記録（tar.gz）をダウンロードし、展開して `data/knet/` 配下に配置します。

ファイル命名規則:
```
{観測点コード(6文字)}{YYMMDDHHmm}.{成分}
```

例（KGS035 / 2026-05-20 11:46 のイベント）:
```
data/knet/
├── KGS0352605201146.NS
├── KGS0352605201146.EW
└── KGS0352605201146.UD
```

詳細は `data/knet/README.md` を参照してください。

### 起動

```bash
.venv/bin/python3 src/analyze_knet.py --station KGS035 --event 2026-05-20-11-46
```

詳細オプションは `--help` を参照してください。

---

## 14. トラブルシューティング

### パケットが届かない / カウントが増えない

- Shake の DATACAST 設定で送信先 IP とポートが正しいか確認してください。
- ファイアウォールが UDP ポートをブロックしていないか確認してください。
- `--bind 0.0.0.0:8888` でバインドしているか確認してください。

### STA/LTA が上がらない

- `--lta` の値だけ静穏期間のデータが必要です。起動直後は LTA バッファが不安定です。
- シミュレーターを使う場合は `--quiet-sec` を `--lta` 以上に設定してください。

### 震度が実際より高い / 低い

- `--sensitivity` を実機の感度値に合わせてください。
- `--rt-window` を長くすると安定した値になります（既定 90 秒推奨）。

### 音声が鳴らない

- VoiceVox Engine が `http://localhost:50021` で起動しているか確認してください。
- VoiceVox が使えない場合は macOS say にフォールバックします。macOS の音量設定を確認してください。

### チャンネル名が一致しない

RS4D の加速度計チャンネルは `ENZ/ENN/ENE` です。速度計や他機種では異なる場合があります。`--channels` で実機に合わせてください。

### Web ダッシュボードに接続できない

- `--web-bind 127.0.0.1`（既定）の場合、同じマシンからしかアクセスできません。外部からアクセスする場合は `--web-bind 0.0.0.0` を指定してください。
- ファイアウォールが `--web-port`（既定 8080）をブロックしていないか確認してください。

### データギャップ後に震度が異常値になる

RS4D との通信が一時的に途絶した後に受信が再開すると、LTA バッファが正しく充填されていない状態で STA/LTA が計算され、誤警報が発生することがあります。v0.6.0 以降では以下の多重防御で対処しています：

1. `compute_loop` が LTA秒数（既定 20 秒）以上パケットが届かないことを検出し、Ring バッファと `shared.fs` をリセット
2. `recv_loop` でパケット間隔が 3 秒を超えた場合も同様にリセット
3. LTA エネルギーが極小（実質ゼロ）の場合は STA/LTA = 0 を返すガード

通信断が続く場合は RS4D の DATACAST 設定と LAN 環境を確認してください。

---

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
