# K-NET / KiK-net データ配置ディレクトリ

`src/analyze_knet.py` が読み込む K-NET / KiK-net 強震波形データを置く場所。

## ダウンロード手順

NIED（防災科研）の K-NET / KiK-net サービスから手動で取得する。

1. https://www.kyoshin.bosai.go.jp/ にアクセス
2. NIED アカウント（K-NET ID）でログイン
3. 「データダウンロード」→「イベント検索」で対象地震を選ぶ
4. 観測点を選んで tar.gz をダウンロード
5. このディレクトリ（`data/knet/`）に置く

## ファイル形式

K-NET ASCII 形式（3成分: NS, EW, UD）。1観測点1イベントで3ファイル。

例:
```
data/knet/
├── SZO0010605240705.NS    # 観測点SZO001、2026/05/24 07:05、NS成分
├── SZO0010605240705.EW
└── SZO0010605240705.UD
```

ファイル名規則: `{観測点コード(6)}{YYMMDDHHmm}.{成分}`

## tar.gz の自動展開について

スクリプトは tar.gz の自動展開には対応していない。手動で `tar xzf` してから配置。

```bash
cd data/knet/
tar xzf 20260524070500.knt.tar.gz
```

## 使い方

```bash
.venv/bin/python3 src/analyze_knet.py --station SZO001 --event 2026-05-24-07-05
```

詳細は `src/analyze_knet.py --help` を参照。
