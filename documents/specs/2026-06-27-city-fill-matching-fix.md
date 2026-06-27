# 実装仕様書: 地図の市区町村塗りつぶし照合バグ修正（区単位対応）

- 作成日: 2026-06-27
- 対象プロジェクト: earthQuake（地震計測震度ダッシュボード）
- 要件トレーサビリティ: 地図塗りつぶし不一致バグ（旧ロジックで 618/4372 局が塗り漏れ）
- 関連設計書: documents/designs/2026-06-27-city-fill-matching-fix.md（末尾「区単位対応」節が最終仕様）
- 関連Issue / PR: なし（口頭要件）

## 概要

気象庁観測点の addr 文字列を市区町村名へ変換できず政令市の区・県名プレフィックス付き addr で塗り漏れていた `extractCity` を廃止し、取得済み GeoJSON の市区町村名集合に対して照合する `resolveCity` へ置換した。政令市は区単位（中央区・天竜区など）で塗り分ける。

## 変更ファイル一覧

| ファイル | 変更種別 | 変更内容 |
|---------|---------|---------|
| src/templates/dashboard.html | 修正 | `extractCity` を削除し `resolveCity`/`matchSeirei`/`normAddr` を新設。政令市定数 `SEIREI`/`SEIREI_NOCITY`、異体字 `ADDR_NORM` を追加。`drawCityLayer` を区単位キー対応に改修 |
| src/test_city_matching.py | 追加 | resolveCity ロジックの Python 移植による全4372局照合テスト＋代表ケース（pytest） |

## 実装の詳細

### src/templates/dashboard.html

- **`extractCity`（旧1185-1195行）を削除**。最短一致で「市」「区」を切り出すだけのため、政令市の区（`浜松中央区高丘東`）・県名プレフィックス（`静岡森町森`）・最短マッチ取りこぼし（`四日市市`）を解決できなかった。
- **`SEIREI`（20政令市）/`SEIREI_NOCITY`（市なし名→市あり名の逆引き）/`ADDR_NORM`（異体字 梼→檮）/`normAddr` を新設**。
- **`matchSeirei(s, keySet, citiesWithKu)`**: addr が政令市の「市あり名／市なし名」で始まり直後が `〜区`（最短一致）で、`(政令市名, 区名)` が県の keys にあれば `[N03_004, N03_005]` を返す。長いヘッド優先でソートし誤マッチを防ぐ。
- **`resolveCity(addr, keySet)`**: 異体字正規化 → 政令市＋区照合（先頭および県名プレフィックス0〜2文字除去後、`大阪堺市堺区…` の二重構造対策）→ 区なし市町村の最長前方一致（先頭およびプレフィックス1〜4文字除去後）の順。一致なしは `null`（塗らない）。設計書の検証済み Python ロジックを忠実に JS 移植。
- **`drawCityLayer` 改修**:
  - `cityScale` 構築を `Promise.all` の後ろへ移動（県の keys は GeoJSON 取得後でないと作れないため）。
  - 結合 feature から `keysByPref`（pref → Set(`N03_004 + ' ' + (N03_005||'')`））を構築。
  - `quake.points` を `resolveCity` で照合し、政令市の区は `pref::市::区`、それ以外は `pref::市町村` のキーで `cityScale` に最大震度を集約（`toScaleNum` 比較は現行維持）。
  - style/onEachFeature のキー生成を `featureKey`（`N03_005` があれば区付き）に統一。popup は政令市で「浜松市中央区」のように市＋区を表示。

### src/test_city_matching.py

resolveCity と同一規則を Python で再現。`data/jma_stations.json` 全4372局を、`data/geojson/{prefコード}/*.json` から構築した keys で照合し、`None`（塗り漏れ）は addr に「空港」を含む局のみ許容、それ以外が出たら fail。PREF_CODE は dashboard.html と同一。代表7ケースもアサート。

## テスト結果

| テストコマンド | 結果 |
|-------------|-----|
| `.venv/bin/python -m pytest src/test_city_matching.py -v` | 8 passed（全局塗り漏れ0、空港13件のみ None、代表7ケース一致） |
| `.venv/bin/python -m pytest src/verify_filter.py src/test_api_events.py -q` | 64 passed |
| `.venv/bin/python src/test_template_parity.py` | exit 0（PASSED） |

全局検証の内訳: 非空港局の塗り漏れ 0件、`None` は空港13局（新千歳・仙台・成田・羽田 等）のみで仕様どおり塗り対象外。

## 残課題・既知の制限

- 観測点座標ではなく addr 文字列照合のため、市町村合併・気象庁 addr 表記変更時は GeoJSON 更新で多くが自動追従するが、新たな異体字・特殊表記が出た場合は `ADDR_NORM` または照合ロジックの追加が必要。全局検証テストが回帰の番人になる。
- 空港等13局はバッジのみ表示で塗らない（市町村ポリゴンを持たないため正常）。

## reviewerへの引き継ぎ事項

- `resolveCity` の JS（dashboard.html）と Python（test_city_matching.py）が同一規則であること（照合順・cut範囲・長いヘッド優先・最長前方一致）。
- `drawCityLayer` で `cityScale` 構築を `Promise.all` 後へ移したことによる順序依存（keys が先、照合が後）。
- キー規則が cityScale 集約・style・onEachFeature の3箇所で一致していること（区付き/なしの分岐）。
- スコープは dashboard.html の該当2関数と新規テストのみ。`jma_intensity_web.py`・GeoJSON データ・`drawBadgeLayer`・`__version__` は未変更。
