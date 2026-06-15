# レビュー報告書（再レビュー）: 読み取り専用API `GET /api/events` の追加

- 作成日: 2026-06-15
- 対象プロジェクト: earthQuake
- 要件トレーサビリティ: fujimidai-observatory 実装依頼書（`/Users/sakaimasanori/Dropbox/fujimidai-observatory/docs/api-requests/earthquake-api-request.md`）
- 関連実装仕様書: 同上（依頼書が仕様の正本）
- 関連設計書: なし
- 関連Issue / PR: 未コミット差分
- 関連レビュー: 第1回 `documents/reviews/2026-06-15-api-events-readonly.md`
- レビュー回数: 第2回（第1回の指摘6点に対する是正の再レビュー）

## レビュー対象
- `src/jma_intensity_web.py`（第1回からの是正差分）
- `src/test_api_events.py`（テスト追加・置換）
- 差分規模: `git diff --stat` → `jma_intensity_web.py` 106 insertions / 1 deletion（既存コードの実変更は import 1行のみ）。

テスト実行結果: `.venv/bin/python -m pytest src/test_api_events.py src/test_template_parity.py -q` → **26 passed**（developer 報告と一致。既存 test_template_parity 3件含め緑）。

## 第1回指摘の是正確認

### 指摘1（中）無効 min_scale の fail-open → 是正済
`_read_trigger_events` 冒頭で `if min_scale is not None and min_scale not in _SCALE_RANK: raise ValueError(...)`（`jma_intensity_web.py:738-739`）。エンドポイント `api_events` が `except ValueError` で捕捉し `JSONResponse(status_code=422, content={"error": ...})` を返す（`:805-806`）。第1回で問題視した「不正値で全件素通り」は解消。`test_min_scale_invalid_raises`（"5","X"）と `test_endpoint_invalid_min_scale_422`（HTTP 422 + error キー）で正常系/異常系の両経路をカバー。**解消。**

### 指摘2（中）encoding 未指定 → 是正済
`open(log_path, "r", encoding="utf-8")`（`:746`）。書き込み側 `jma_intensity_tui.py:add_event`（`encoding="utf-8"`）と対称になった。`test_reads_utf8_multibyte_scale` で "5弱" の読み取りを検証。**解消。**

### 指摘3（中）limit 上限・負値 → 是正済
`_EVENTS_LIMIT_MAX = 10000` を追加し、`limit: int = Query(default=1000, ge=0, le=_EVENTS_LIMIT_MAX)`（`:786`）で境界を制約。`test_endpoint_negative_limit_422`（`limit=-5`）・`test_endpoint_limit_over_max_422`（上限+1）で 422 を確認。負値の全件返却・無制限読み込みは塞がれた。**解消。**

### 指摘4（低）例外の握りつぶし → 是正済
ファイル単位の `try/except Exception: return []` を撤廃。行単位の `json.loads` の except（破損行スキップ）のみ残置（`:756-758`）。I/O・デコード障害は呼び出し側へ伝播し、`api_events` は `ValueError` のみ意図的に 422 化、それ以外（`UnicodeDecodeError`・`OSError` 等）は未捕捉のため FastAPI が HTTP 500 で顕在化する。「障害がイベント0件に化ける」を解消。`test_io_error_propagates_not_empty`（不正UTF-8で `UnicodeDecodeError` 伝播）で担保。`feedback_detection_policy`（見逃しの方が深刻）の方針に整合。**解消。**

  影響範囲確認: `_read_trigger_events` の呼び出し元は `api_events` のみ（grep で確認）。例外伝播の挙動変更が `/`・`/ws`・`/api/config`・`/api/analyze`・WebSocketブロードキャスト・リアルタイム処理スレッドに波及しないことを確認した。

### 指摘5（低）トートロジーテストの置換 → 部分的是正（下記「新規指摘」参照）
`test_min_scale_order_is_not_lexical` は、`_SCALE_ORDER.index()` を読み戻すだけのトートロジーから、実ログを読ませて `min_scale="5強"` 指定時に "5弱" が除外されることを確認する実経路テストへ置換された。実経路を通すようになった点は改善。ただし「文字列比較ではないことの保証」という主張は依然として成立していない（新規指摘 NR-1 を参照）。**実経路化は達成。lexical 識別能力は未達。**

### 指摘5関連（低）異常系テスト追加 → 是正済
`test_min_scale_invalid_raises` / `test_io_error_propagates_not_empty` / `test_reads_utf8_multibyte_scale` / エンドポイント 422系3件（invalid min_scale・負limit・上限超過）が追加された。第1回で欠落していた異常系・境界の振る舞いがテストで固定された。**解消。**

## 厳守事項の再確認（前回同様維持されているか）
- `jma_intensity_realtime.py` / `jma_intensity_tui.py` に変更なし（`git diff --name-only` で対象外を確認）。コア（JMAフィルタ・UDP受信・Ringバッファ・震度換算）・`trigger_log.jsonl` 書き込み経路・キー名はいずれも不変。
- 既存コードの実変更は `from fastapi import ... Query` の import 1行のみ。`/api/config` の 422 ハンドラ（`:546,548`）は本変更とは無関係な既存実装で、触れていない。
- 追加は読み取り専用 `GET /api/events` 1本のみ。`_read_trigger_events` は単一呼び出し元で隔離されており、既存エンドポイント・WebSocket配信・リアルタイムスレッドの挙動を変えない。
- レスポンス形式（`{"count": N, "events": [...]}`、既存キー名保持、新しい順）は第1回から不変。統合側 config への申し送りが必要なレスポンス形式変更なし。

## 新規・残存指摘

### [重大度: 低] NR-1 src/test_api_events.py:110-131 — 「文字列比較ではないこと」を実質的に検証できていない（第1回指摘5の残存）
JMA 震度文字列（"0".."4","5弱","5強","6弱","6強","7"）は、**ナイーブな文字列比較 `scale >= min_scale` でも震度ランク比較と同じ結果になる**。

- 先頭文字が数字（"5"=0x35 < "6"=0x36 < "7"=0x37）で支配的に順序付く。
- "5弱"/"5強" の第2文字は "弱"=U+5F31 < "強"=U+5F37 で、lexical でも "5弱" < "5強"。ランク順と一致する。

このため、本テストが想定する `min_scale="5強"` → "5弱" 除外は、**実装をナイーブ文字列比較に退行させても同じく成立し、テストはパスしてしまう**（"5弱" は lexical でも "5強" 未満なので除外される）。`test_min_scale_intensity_order`（min_scale="5弱"）も同様に lexical と結果が一致する。

結論として、本テスト群は「rank ベースであること」を区別できておらず、docstring の「単純文字列比較ではなく震度順序で比較されることを実経路で保証する」という主張は過大。**製品コード（`:738-769`）は正しく rank で比較しており、機能は正常**。問題は「テストが lexical 退行を検出できない」点に限られるため重大度・低。

是正したい場合の方向性: lexical と rank で結果が割れる入力（例えば valid scale の集合外には作れないため、`_SCALE_RANK` のランク値そのものを使った比較ロジックを直接ユニットテストする、または `_read_trigger_events` 内の比較が `_SCALE_RANK` を参照していることを通じて検証する）か、少なくとも docstring の主張を「実経路で min_scale フィルタが効くこと」に弱める。**必須ではない**。

### [重大度: 低] NR-2 src/jma_intensity_web.py:746-771 — ファイル全行のメモリ展開は据え置き（将来リスク、今回スコープ外で可）
`limit` に `le=10000` の上限が付いたことで返却件数とソート対象は有界化されたが、`for line in f:` は依然として**ファイル全行を走査**し、フィルタ通過分を `events` に蓄積してからソート・スライスする。`min_scale` や `date` で大半が落ちる場合でも、ファイルI/O自体は全行読む。trigger_log は append-only で増え続けるため、年単位では走査コストが伸びる。

現状規模（21日で1386件）では実害はなく、第1回でも「将来リスク」と位置づけた範囲。limit 上限で最悪ケースのメモリは抑えられたため、**今回の是正対象としては不要**。将来ログが大きくなった場合に末尾読み（逆方向読み）を検討する余地がある、という申し送りに留める。

## セキュリティ検査結果
- パストラバーサル: なし（読み取り先は module 定数 `_TRIGGER_LOG_PATH` 固定。クエリがパスに混入しない）。
- XSS: 該当なし（JSON応答のみ）。
- コマンドインジェクション: なし。
- 認証情報漏洩: なし（返却は trigger_log の既存フィールドのみ。422 の `{"error": ...}` メッセージは min_scale の入力値と `_SCALE_ORDER` のみを含み、機微情報・パス・スタックトレースを露出しない）。
- DoS: limit 上限（10000）導入で最悪ケースの返却件数・ソート対象が有界化。残るのは全行走査コスト（NR-2、現状実害なし）。
- 無認証エンドポイントである点は第1回同様。`10.0.1.28:8080` の LAN 外非露出は引き続き release-manager / インフラ側で確認すること。

## ドキュメント整合性
- 依頼書のレスポンス形式・キー名・新しい順の要件と実装は一致（第1回から不変）。
- 新たに「不正 min_scale は 422」「limit は 0〜10000、範囲外は 422」という**入力契約が追加**された。これは統合側の挙動に影響する（統合側が "5" のような不正 min_scale を送ると 422 になる）。第1回報告書の申し送りどおり、統合側へ「不正 min_scale・範囲外 limit はエラー（422）になる」旨を伝えること。レスポンスの正常形（200時のボディ）は不変なので、正常系のパースには影響しない。
- earthQuake 側の `documents/releases/` へのエンドポイント追加記録は引き続き release-manager に申し送る。

## 総評
- 判定: **承認**

  第1回の指摘6点はすべて是正された。中重大度3点（min_scale fail-open / encoding / limit境界）は製品コード・テストの両面で確実に解消。低3点（例外握りつぶし・トートロジーテスト・異常系テスト欠落）も、例外伝播と異常系テストは完全解消、トートロジーテストは実経路化された。残る NR-1（テストが lexical 退行を検出できない）と NR-2（全行走査）はいずれも**製品コードの正しさには影響しない**低重大度であり、リリースのブロッカーにはならない。厳守事項（コア・書き込み経路・既存エンドポイント・リアルタイム/WebSocket 不変）は維持されている。

- release-manager への申し送り:
  - **入力契約の追加を統合側へ申し送ること**: 不正 min_scale（"5" 等、`_SCALE_ORDER` 外の値）→ 422 `{"error": ...}`、limit が 0〜10000 の範囲外 → 422。200時の正常レスポンス形式は不変。
  - 本エンドポイントは無認証。`10.0.1.28:8080` が LAN 外へ露出しないことを確認のうえリリースすること。
  - `documents/releases/` に「GET /api/events 追加（fujimidai-observatory 連携、入力境界 422 つき）」を記録すること。
  - NR-1 / NR-2 は将来の改善候補（任意）。リリース前対応は不要。
  - 未追跡の `requirements.lock.txt` は本変更スコープ外（コミット対象に含めるかは別判断）。

## 是正処置記録（第1回指摘の最終状況）

| 指摘番号 | 指摘内容の要約 | 是正期限 | 是正担当 | 是正状況 |
|---------|-------------|---------|---------|---------|
| 1 | 無効 `min_scale` の fail-open（全件素通り） | 2026-06-19 | developer | 対応済（ValueError→422、テスト追加） |
| 2 | 読み取り `open` の `encoding="utf-8"` 明示 | 2026-06-19 | developer | 対応済 |
| 3 | `limit` の上限・負値検証（`ge=0, le=10000`） | 2026-06-19 | developer | 対応済（境界テスト追加） |
| 4 | ファイル単位の例外握りつぶし撤廃（障害を顕在化） | 2026-06-19 | developer | 対応済（伝播テスト追加） |
| 5 | トートロジーテストの解消 | 2026-06-19 | developer | 対応済（実経路化。lexical識別は NR-1 として低重大度で残置） |
| 5関連 | 異常系・境界テストの追加 | 2026-06-19 | developer | 対応済 |

> 全指摘が解消（または製品コードに影響しない低重大度として許容）されたため本レビューは承認とし、release-manager へ引き継ぐ。
> 残る NR-1・NR-2 は任意の将来改善であり、リリースをブロックしない。
