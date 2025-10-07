# Raspberry Shake 計測震度（JMA）算出プログラム

Raspberry Shake の UDP 出力（DATACAST）を受信し、リアルタイムに地震検出（STA/LTA）と計測震度（JMA）を算出・表示します。

本リポジトリには次の 2 つのスクリプトが含まれます。以下ではスクリプトごとに説明を分けて記載します。
- jma_intensity_realtime.py
- jma_intensity_rs4d.py（RS4D での利用を想定した名称）

計測震度は、3 成分加速度の合成に JMA 応答（周波数領域フィルタ）を適用し、0.3 秒間の最大値近傍の振幅 a[gal] から I = 2log10(a) + 0.94 で算出します。I（小数 2 桁）と震度階級（0〜7、5弱/5強/6弱/6強）を出力します。

せっかくRaspberry Shakeを設置しているのだから、地震が発生した際に自分のRaspberry Shakeがどの程度揺れたのかを知りたい、というニーズに応えるものです。公式な震度情報ではなく参考値ですが、公式の震度計がない場所でも揺れの大きさを把握する一助になるでしょう。

---

## 共通の特長
- Raspberry Shake の DATACAST（UDP, 既定ポート 8888）を直接受信
- 3 成分（N/E/Z）をリングバッファで保持し、直近窓でリアルタイム計測震度を表示
- STA/LTA による簡易検出（トリガ）と定期ステータス出力（5 秒間隔）
- StationXML（ローカル or FDSN）で counts → 加速度 [m/s^2] に応答除去（ObsPy）

## 共通の動作要件とインストール
- Python 3.8+
- 必要パッケージ: numpy, obspy

インストール例:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install numpy obspy
```

## Raspberry Shake 側の設定（DATACAST・共通）
- Shake の Web 設定画面で UDP/DATACAST を有効化し、送信先 IP を本スクリプトを実行するマシンの IP に、ポートを 8888（既定）に設定します。
- 送信フォーマットは Raspberry Shake マニュアル記載の DATACAST 形式（例: `{ 'HNZ', epoch, 123,456, ... }`）。1 パケット 1 チャンネルを想定します。

## StationXML の用意（共通）
- いずれかを選択:
  1) ローカルファイルを指定（例: data/R38DC.xml）
  2) FDSN Stations API から取得（例: Raspberry Shake 本体の http://<shake-ip>:16023）

本リポジトリには例として `data/R38DC.xml` が含まれています。

---

# 1. jma_intensity_realtime.py

Raspberry Shake からの UDP（DATACAST）を受信し、リアルタイムに計測震度を表示します。汎用的な名称のため、RS4D 以外でも使用できます。

## 使い方（オプション）
- --bind 受信アドレス:ポート（既定: 0.0.0.0:8888）
- --channels 3 成分チャンネル（既定: HNN,HNE,HNZ）
- --network ネットワークコード（既定: AM）
- --station ステーションコード（必須。例: R38DC）
- --stationxml ローカル StationXML のパス
- --fdsn FDSN Stations ベース URL（例: http://rs.local:16023）
- --rt-window 計測震度の計算窓長 [秒]（既定: 90）
- --sta STA 窓 [秒]（既定: 1.0）
- --lta LTA 窓 [秒]（既定: 20.0）
- --trig STA/LTA しきい値（既定: 3.5）
- --det-hold 検出後の再検出抑制 [秒]（既定: 20）

注意:
- --stationxml と --fdsn はどちらか一方を指定してください（どちらも未指定だと応答除去ができず警告になります）。
- --station は必須です。ネットワークコードは適宜合わせてください（Raspberry Shake は AM が一般的）。

## 実行例（realtime）
1) ローカル StationXML を用いる場合

```bash
python3 jma_intensity_realtime.py \
  --station R38DC \
  --network AM \
  --stationxml data/R38DC.xml
```

2) FDSN サーバ（Shake 本体）から取得する場合

```bash
python3 jma_intensity_realtime.py \
  --station R38DC \
  --network AM \
  --fdsn http://<shake-ip>:16023
```

3) 検出パラメータの調整例（感度をやや高く）

```bash
python3 jma_intensity_realtime.py \
  --station R38DC \
  --stationxml data/R38DC.xml \
  --sta 0.5 --lta 10 --trig 3.0 --rt-window 60
```

---

# 2. jma_intensity_rs4d.py

RS4D（Raspberry Shake 4D）での利用を想定したスクリプト名ですが、処理内容やオプションは `jma_intensity_realtime.py` と同一です。RS4D の既定 3 成分（HNN/HNE/HNZ）での動作を想定しています。

## 使い方（オプション）
- --bind 受信アドレス:ポート（既定: 0.0.0.0:8888）
- --channels 3 成分チャンネル（既定: HNN,HNE,HNZ）
- --network ネットワークコード（既定: AM）
- --station ステーションコード（必須。例: R38DC）
- --stationxml ローカル StationXML のパス
- --fdsn FDSN Stations ベース URL（例: http://rs.local:16023）
- --rt-window 計測震度の計算窓長 [秒]（既定: 90）
- --sta STA 窓 [秒]（既定: 1.0）
- --lta LTA 窓 [秒]（既定: 20.0）
- --trig STA/LTA しきい値（既定: 3.5）
- --det-hold 検出後の再検出抑制 [秒]（既定: 20）

注意:
- --stationxml と --fdsn はどちらか一方を指定してください。
- --station は必須です。

## 実行例（rs4d）
1) ローカル StationXML を用いる場合

```bash
python3 jma_intensity_rs4d.py \
  --station R38DC \
  --network AM \
  --stationxml data/R38DC.xml
```

2) FDSN サーバ（Shake 本体）から取得する場合

```bash
python3 jma_intensity_rs4d.py \
  --station R38DC \
  --network AM \
  --fdsn http://<shake-ip>:16023
```

---

## 出力例（共通）
```
[INFO] UDP受信待機: 0.0.0.0:8888  （RS側DATACAST設定が必要）
[INFO] 受信開始。最初の数秒は fs（サンプリング周波数）を推定します…
[INFO] StationXML をロードしました。counts→ACC 変換を開始。
[STATUS] fs=100.0Hz ratio=0.45  a=2.34gal  I=1.52  震度:2
[TRIGGER] fs=100.0Hz ratio=4.12  a=68.23gal  I=2.78  震度:3
```

- STATUS: 5 秒ごとの現在値
- TRIGGER: STA/LTA がしきい値を超えたタイミング

## 実装メモ（内部動作の概要・共通）
- UDP パケットを受信してチャンネルごとにリングバッファへ蓄積
- 同一チャンネルの連続パケットの開始時刻差から fs（サンプリング周波数）を推定
- 直近 `--rt-window` 秒のデータを取り出し、ObsPy の `remove_response` で ACC[m/s^2] へ変換
- 3 成分の合成加速度に JMA 応答（周波数領域フィルタ）を適用
- 0.3 秒間の最大値近傍の振幅（a）をガル換算し、I = 2log10(a) + 0.94 から計測震度を算出
- 未フィルタ合成波形に対して STA/LTA を計算し、トリガ時と 5 秒おきに出力

## よくあるつまずき（共通）
- StationXML が一致しない/取得できない: `--stationxml` または `--fdsn` を正しく指定してください。Raspberry Shake 本体の FDSN は通常 16023 ポートです。
- チャンネル名が異なる: `--channels` で実機の HN?/EN?/EH? などに合わせてください（既定: HNN,HNE,HNZ）。
- 強い直流/飽和: `remove_response` 前後のデータにテーパや Water-level（既定 60）を適用していますが、状況により調整が必要な場合があります。

## 参考
- Raspberry Shake Manual: UDP Port Output / DATACAST
- ObsPy Documentation: remove_response, Stream/Trace 操作

本スクリプト群の計測震度は参考値です。公式な震度情報ではありません。