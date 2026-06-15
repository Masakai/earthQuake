# レビュー報告書: 読み取り専用API `GET /api/events` の追加

- 作成日: 2026-06-15
- 対象プロジェクト: earthQuake
- 要件トレーサビリティ: fujimidai-observatory 実装依頼書（`/Users/sakaimasanori/Dropbox/fujimidai-observatory/docs/api-requests/earthquake-api-request.md`）
- 関連実装仕様書: 同上（依頼書が仕様の正本）
- 関連設計書: なし（earthQuake 側の MCP 実装は不要との依頼）
- 関連Issue / PR: 未コミット差分
- レビュー回数: 第1回

## レビュー対象
- `src/jma_intensity_web.py`（diff: `from fastapi import` に `Query` 追加、`_SCALE_ORDER` / `_SCALE_RANK` / `_TRIGGER_LOG_PATH` 定数、`_read_trigger_events()` ヘルパー、`@app.get("/api/events")` エンドポイントを追加。既存コードの変更は import 1行のみ）
- `src/test_api_events.py`（新規。単体10件＋結合7件＝17件）
- `requirements.lock.txt`（未追跡。本変更とは無関係の依存スナップショットと判断。スコープ外）

テスト実行結果: `pytest src/test_api_events.py src/test_template_parity.py -v` → 20 passed（既存 test_template_parity も緑のまま）。

## 合格項目
- **厳守事項（触ってはいけない範囲）はすべて遵守**。
  - `jma_intensity_realtime.py` のコア（JMAフィルタ・UDP受信・Ringバッファ・震度換算）に変更なし。
  - `trigger_log.jsonl` の書き込み経路・キー名に変更なし。`jma_intensity_tui.py:add_event`（`date, ts, I, scale, ratio` を書く）と読み取り側のキー名が一致。本APIは読み取り専用で、ファイルへの書き込み・追記は一切行わない。
  - 追加は読み取り専用 `GET /api/events` 1本のみ。既存エンドポイント（`/`, `/ws`, `/api/config`, `/api/analyze` 等）・WebSocketブロードキャストの挙動に変更なし。
  - リアルタイム処理・WebSocket配信スレッドへの干渉なし（別スレッド/別タスクで動く受信・compute・broadcast に触れていない）。
- **`min_scale` の震度順序比較が正しい**。`_SCALE_ORDER` によるランク比較を採用しており、依頼書の厳守点（単純文字列比較禁止）を満たす。`"5弱" < "5強" < "6弱"` 等が正しく扱える（test_min_scale_intensity_order で検証）。
- **新しい順で返す**。`(date, ts)` 降順ソート済み。`limit` は新しい順での先頭スライス。MCP ライブツールが「直近数件」をすぐ取れる要件に適合。
- **空ログ・ファイル不在時に `{"count":0,"events":[]}` を返す**（test_endpoint_empty で検証）。
- **破損行・空行のスキップ**。`json.loads` 失敗・非 dict 行・空行を握りつぶしてスキップ（test_read_all のフィクスチャに空行＋破損行を混入して検証）。
- **パストラバーサルなし**。読み取り先は module 定数 `_TRIGGER_LOG_PATH` 固定で、クエリパラメータがファイルパスに混入する経路はない。
- **コードスタイル適合**。4スペースインデント、日本語コメント、docstring あり。プロジェクト規約に沿う。
- **`limit` の型検証は FastAPI 層で機能**。`limit=abc` は 422 を返すことを実機確認。`from` の alias 解決（`Query(alias="from")`）も結合テストで実経路を通っており、テスト注記の主張は事実。

## 指摘事項

### [重大度: 中] src/jma_intensity_web.py:723, 733-736 — 無効な `min_scale` がサイレントに fail-open（全件返却）
`min_rank = _SCALE_RANK.get(min_scale)` は、`min_scale` が `_SCALE_ORDER` に無い値（例: `"5"`、`"5.0"`、`"V"`、全角ゆらぎ等）のとき `None` を返す。その結果 `if min_rank is not None:` のフィルタブロックがまるごとスキップされ、**フィルタ未適用＝全件返却**になる。

実機確認:
- `_read_trigger_events(p, min_scale="5")` → 3件（ノイズ含む全件。"5弱"/"5強" のつもりが素通り）
- `_read_trigger_events(p, min_scale="X")` → 3件（同上）

依頼書では `trigger_log.jsonl` の **99.86% が scale="0"（ノイズ）** であり、統合側は `min_scale` でノイズ除外して転送量を減らすのが主目的。`min_scale="5"`（"5弱"と書くべきところを誤って "5" と渡す）のような取り違えが起きると、フィルタが効かず**大量のノイズが 200 OK で素通りする**。エラーにならないため統合側は誤りに気づけない。

期待挙動の例: 未知の `min_scale` は HTTP 400/422 で弾く、もしくは「不正値は無視」を明示的にドキュメント化する。少なくとも現状の「不正値→全件」はもっとも危険な fail-open であり、避けるべき。

### [重大度: 中] src/jma_intensity_web.py:738 — ファイル読み取りに `encoding` 未指定（ロケール依存で UnicodeDecodeError の可能性）
`with open(log_path, "r") as f:` が encoding を指定していない。一方、書き込み側 `jma_intensity_tui.py:add_event` は `encoding="utf-8"` かつ `ensure_ascii=False` で**マルチバイトの scale（"5弱","6強" 等）を生 UTF-8 で書いている**。

読み取り側のデフォルトエンコーディングはロケール依存（`locale.getpreferredencoding()`）。本番 iMac の通常環境では UTF-8 になるため現状動くが、`LANG=C` / `LC_ALL=C` のような環境（cron・systemd・最小化されたサービス起動時に起こりうる）では ASCII/latin-1 になり、"5弱" を含む行で `UnicodeDecodeError` を起こす。

`_read_trigger_events` は読み取り全体を `try/except Exception: return []` で囲っているため、**この例外は捕捉されて空リストが返り、地震イベントがサイレントに 0 件になる**（後述の指摘とも関連）。書き込み側と対称に `open(log_path, "r", encoding="utf-8")` を明示すべき。

### [重大度: 中] src/jma_intensity_web.py:716, 745-746 — `limit` に上限がなく、負値も未検証（DoS／意図しない全件返却）
- `limit` に上限キャップがない。依頼書は「巨大化に備え limit を効かせる」と明記するが、呼び出し側が `limit=100000000` を渡せば**ファイル全行を読み込み・パース・ソートしてから**スライスする（読み込み自体は全件メモリ展開）。limit はあくまで返却件数の制限であり、読み取り量・メモリ消費の制限にはなっていない。trigger_log.jsonl は append-only で増え続けるため、将来的なメモリ・レイテンシのリスク。
- 負の `limit` がスルーされる。`if limit is not None and limit >= 0:` のため `limit=-1` ではスライスが行われず**全件返却**になる（実機確認: HTTP `limit=-5` → 200 で全件）。FastAPI 層でも `Query(ge=1, le=...)` 等の境界制約が無いため弾かれない。`limit` に `Query(default=1000, ge=0, le=<上限>)` 相当の制約を付けるのが望ましい。

> 補足: 読み取りを全行メモリ展開する設計自体は、現状のログ規模（21日で1386件）では実害は小さい。ただし「最新N件だけ欲しい」要件に対しては将来的に末尾読み（あるいは行数上限）を検討する価値がある。本指摘は重大度・中（将来リスク＋負値の即時バグ）。

### [重大度: 低] src/jma_intensity_web.py:737-744 — 例外の握りつぶしが広すぎて障害が「空リスト」に化ける
`for line in f:` を囲う外側の `try/except Exception: return []` は、エンコーディングエラー・I/O エラー・想定外の例外をすべて「イベント0件」に変換する。統合側からは「地震が無い」と区別できず、**障害がサイレントに地震ゼロとして伝播**する。`feedback_detection_policy.md`（見逃しの方が深刻）の方針に照らしても、読み取り障害は 500 等で顕在化させるか、少なくともログ出力すべき。行単位の `json.loads` の except は破損行スキップとして妥当だが、ファイル単位の握りつぶしは過剰。

### [重大度: 低] src/test_api_events.py:110-115 — `test_min_scale_order_is_not_lexical` が「文字列比較でないこと」を実質検証していない
このテストは `_SCALE_ORDER.index(...)` の結果が単調増加であることを確認しているだけで、これは `_SCALE_ORDER` の定義（リテラルの並び）をそのまま読み戻しているにすぎず、トートロジーに近い。「単純文字列比較ではダメ」という依頼書の核心（例: lexical では `"5弱" > "10"` 的な逆転や、`"6弱"` と `"5強"` の順序）を突いていない。`test_min_scale_intensity_order`（"5弱" 指定で "1" が除外され "5弱","6強" のみ）が実質的な担保になっているので機能検証は足りているが、本テストは名前に反して保証が弱い。

### [重大度: 低] src/test_api_events.py — 「無効 min_scale」「負/超過 limit」の経路が未テスト
上記の中・重大度指摘（無効 min_scale の fail-open、負値 limit の全件返却）に対応するテストが無い。これらは正常系のみ検証されており、異常系・境界の振る舞いが仕様として固定されていない。是正時にあわせてテスト追加を推奨。

## セキュリティ検査結果
- **パストラバーサル**: なし。読み取りパスは module 定数固定。クエリパラメータがパスに混入する経路は存在しない。
- **XSS**: 該当なし（JSON レスポンスのみ。HTML テンプレートへユーザー入力を差し込む経路なし）。
- **コマンドインジェクション**: なし（subprocess 等の呼び出しを追加していない）。
- **認証情報漏洩**: なし。返すのは trigger_log の既存フィールド（date, ts, I, scale, ratio）のみ。SECRET / .env / 認証情報を出力する経路なし。
- **情報漏洩（その他）**: 本エンドポイントは無認証で、依頼書どおり LAN 内（`10.0.1.28:8080`）の統合システムからの pull を前提とする。トリガ履歴自体は機微情報ではないが、無認証である点は release-manager / インフラ側で「8080 が LAN 外へ露出しない」ことを確認しておくこと（本コードの責務外だが申し送り）。
- **DoS**: 上記「limit 上限なし・全行メモリ展開」が該当（重大度・中の指摘に記載）。現状規模では実害小だが設計上の留意点。

## ドキュメント整合性
- 依頼書のレスポンス形式（`{"count": N, "events": [...]}`、既存キー名 `date, ts, I, scale, ratio` 保持、新しい順）と実装は一致。**統合側 config（`base_url: http://10.0.1.28:8080`, `endpoint: /api/events`）への申し送りが必要なレスポンス形式変更は無し**。
- earthQuake 側に MCP 実装を追加しないという依頼を遵守（追加は GET 1本のみ）。
- `documents/` 配下に本APIの仕様を記す文書は未作成。依頼書が別リポジトリ（fujimidai-observatory）にあるため、earthQuake 側のリリース記録（`documents/releases/`）にエンドポイント追加を1行残すことを release-manager に申し送る。

## 総評
- 判定: **要修正**

  厳守事項は完全に守られており、機能の主要部（震度順序比較・新しい順・空/破損ログ処理・パス安全性）は仕様適合かつテストで担保されている。一方で **「無効な min_scale で全件 fail-open」「encoding 未指定によるロケール依存の読み取り失敗が空リストに化ける」「limit に上限・負値検証がない」** の3点は、いずれも *エラーにならずノイズ全件素通り or サイレント0件* という統合側が気づけない失敗モードを生む。依頼書が min_scale の正確性と limit の有効化を明示的に求めている以上、中重大度として是正対象とする。

  > 注: 本プロジェクトの規約では「重大度・高の指摘がある場合に要修正」とされるが、本件は高は無く中が3点。うち min_scale fail-open は依頼書の主目的（ノイズ除外）を無効化しうるため、中だが是正必須と判断した。軽微（low）扱いに留めるのが妥当との判断であれば、最低限 min_scale fail-open のみでも是正を求める。

- release-manager への申し送り:
  - 本エンドポイントは無認証。`10.0.1.28:8080` が LAN 外へ露出していないことを確認のうえリリースすること。
  - `documents/releases/` に「GET /api/events 追加（fujimidai-observatory 連携）」を記録すること。
  - 統合側 config に影響するレスポンス形式変更は無し（現時点で申し送り不要）。是正で min_scale を 400 で弾く挙動に変える場合は、統合側へ「不正 min_scale はエラーになる」旨を申し送ること。
  - 未追跡の `requirements.lock.txt` は本変更のスコープ外。コミット対象に含めるかは別途判断。

## 是正処置記録（要修正）

| 指摘番号 | 指摘内容の要約 | 是正期限 | 是正担当 | 是正状況 |
|---------|-------------|---------|---------|---------|
| 1 | 無効な `min_scale` がフィルタ未適用（全件 fail-open）になる。400/422 で弾くか仕様明記し、テスト追加 | 2026-06-19 | developer | 未対応 |
| 2 | ファイル読み取りの `open` に `encoding="utf-8"` を明示（書き込み側と対称、ロケール依存回避） | 2026-06-19 | developer | 未対応 |
| 3 | `limit` の上限キャップと負値検証を追加（`Query(ge=0, le=<上限>)` 等）。境界テスト追加 | 2026-06-19 | developer | 未対応 |
| 4 | ファイル単位の例外握りつぶしを見直し、読み取り障害を空リストに化けさせない（顕在化 or ログ） | 2026-06-19 | developer | 未対応 |
| 5 | `test_min_scale_order_is_not_lexical` のトートロジー解消（実比較経路を突くテストへ） | 2026-06-19 | developer | 未対応 |

> 是正完了後、developer は対応内容をチャットで報告し、reviewer が再レビューを実施して本ファイルを更新すること。
> 再レビュー時は同 slug で新しいレビュー報告書を作成し（`-2`）、「レビュー回数」を増やす。
