# 実装仕様書: HVSR週次モニタリング機能

- 作成日: 2026-07-14
- 対象プロジェクト: earthQuake
- 要件トレーサビリティ: ユーザー合意事項（2026-07-14会話）。試算根拠は2026-06-26山梨県東部・富士五湖M5.6データ
- 関連設計書: `documents/designs/2026-07-14-hvsr-weekly-monitoring.md`
- 関連レビュー報告書: `documents/reviews/2026-07-14-hvsr-weekly-monitoring-review.md`（判定「承認」、中重大度2件・低重大度2件を実装時に解消）
- 関連Issue / PR: なし（新規機能）

## 概要

R38DC観測点の深夜帯常時微動データからHVSR（水平/上下スペクトル比、Nakamura法）を週次で計算・蓄積し、Webダッシュボードに新規パネルとして可視化する機能を実装した。計算バッチ・読み取り専用API・WebUIパネル・launchd plistテンプレート・マニュアルを追加し、既存のリアルタイム系（`SharedState`・`broadcast_loop`・`compute_loop`・`recv_loop_fn`）には一切変更を加えていない。

## 変更ファイル一覧

| ファイル | 変更種別 | 変更内容 |
|---------|---------|---------|
| `src/hvsr_weekly.py` | 新規作成 | HVSR週次計算バッチ本体。`analyze_rs.py`の`download_channel`/`download_channel_seedlink`/`compute_stalta`をコピーして複製。HVSR計算コアロジック（窓分割・アンチトリガ・FFT・スタッキング・Konno-Ohmachi平滑化・SESAME簡易クライテリア）を新規実装 |
| `src/test_hvsr_weekly.py` | 新規作成 | `hvsr_weekly.py`のユニットテスト・統合テスト（34件） |
| `src/test_api_hvsr_history.py` | 新規作成 | `GET /api/hvsr_history`のテスト（12件、`test_api_events.py`のASGI直接呼び出しパターンを踏襲） |
| `src/jma_intensity_web.py` | 修正 | 読み取り専用エンドポイント`GET /api/hvsr_history`を追加。`_read_hvsr_history`・`_hvsr_history_snapshot`（mtimeベースキャッシュ）を新規追加。既存のSharedState・broadcast_loop・lifespan内スレッド起動処理は無変更 |
| `src/templates/dashboard.html` | 修正 | 新規パネル`hvsr-panel`を`#col-right`内`bphistory-panel`直後に追加（週次推移チャート・HVSR曲線チャート・SESAMEバッジ）。レイアウト是正のため`#col-right`のCSSを`overflow: hidden`→`overflow-y: auto`に変更し、`#bphistory-panel`を`flex: 1 1 0`→`flex: 0 0 auto`（固定高さ110px）に変更（下記「レイアウト変更の詳細」参照） |
| `scripts/launchd/com.riverruns.earthquake-hvsr-weekly.plist` | 新規作成 | iMac本番機へのlaunchd登録用plistテンプレート。実際のiMacへのデプロイ・launchctl登録は本実装のスコープ外 |
| `docs/MANUAL.md` | 修正 | 新規セクション「16. HVSR週次モニタリング」を追加。`microseism.py`の既存H/V比計算との違いを明記（レビュー低重大度指摘への対応） |

## 実装の詳細

### `src/hvsr_weekly.py`

**関数複製方針**: `download_channel()`・`download_channel_seedlink()`・`compute_stalta()`を`analyze_rs.py`からコピーして複製した（import不使用）。理由は`analyze_rs.py`がトップレベルでgeopandas・matplotlib（Agg初期化含む）を読み込む重量級モジュールであり、週次バッチには不要な依存を持ち込むため。複製した関数の冒頭には「このロジックはanalyze_rs.py/hvsr_weekly.pyの対となる関数と重複しています。修正時は両方を確認してください」という趣旨のコメントを付与した。

**HVSR計算コアロジック**:
- `split_windows()`: 40秒窓・50%オーバーラップで波形を分割（3時間データで539窓）
- `is_window_valid()`: STA/LTA比が`[0.5, 2.0]`の範囲を外れる時刻を含む窓をSESAME準拠のアンチトリガとして棄却。`analyze_rs.py`の`trig=3.5`（地震検知トリガ）とは意味論が別物である点をコメントで明記
- `apply_cosine_taper()`: 5%コサイン（Tukey窓）テーパー
- `compute_window_hv()`: FFT→水平2成分（ENN, ENE）幾何平均→H/V比。DC成分（周波数0）は比の対象として無意味なため除外
- `stack_log_average()`: 対数平均（幾何平均）でスタッキング
- `smooth_and_resample()`: Konno-Ohmachi平滑化→対数等間隔81点への補間
- `sesame_criteria_ok()`: SESAME (2004) 信頼性クライテリア3項目（`window_length_ok`/`amplitude_ok`/`stability_ok`）を算出
- `compute_hvsr_from_traces()`: 上記を統合し、`status: "ok"/"insufficient_data"/"failed"`を含むレコード辞書を返す

**実装中に発見・修正した不具合（Konno-Ohmachi平滑化のnormalizeパラメータ）**: 実装直後に既知周波数（1.0Hz）の正弦波+ノイズによる合成波形テストを実施したところ、ピーク周波数がナイキスト周波数（50Hz）付近の20Hzに誤検出される現象を確認した。原因調査のため`konno_ohmachi_smoothing()`のソースを確認し、`normalize=False`（デフォルト）では平滑化窓が対数尺度で正規化されず、周波数ビン構成上ナイキスト周波数に近い高周波側で窓の重み和が縮小し、本来ノイズフロア相当の値が不当に増幅されることを実測で確認した。`normalize=True`（SESAMEガイドライン標準・Geopsy実装に合わせた値）に変更することで、期待通り1.0Hz付近にピークが出ることを確認した。設計書には`normalize`パラメータの明記がなかったため、この修正内容・根拠をコードコメントに明記した。

**`run()`のダウンロード失敗時の扱い**: 3成分のいずれか1つでもダウンロード失敗（ファイル未生成・0バイト）した場合、HVSR計算自体を行わず`status: "failed"`のレコードを`data/hvsr_history.jsonl`に追記する。障害を「データなし」に化けさせない既存方針（`_read_trigger_events`のコメント方針）を踏襲した。

### `src/jma_intensity_web.py`

`GET /api/hvsr_history`を`GET /api/events`の直後に追加した。設計書は「`_bp_history_load`パターンを踏襲」としていたが、レビューで指摘された通りこれは不正確（`_bp_history_load`は起動時1回ロード＋deque追記のみで再読み込み経路を持たない）。実際には`_read_trigger_events`/`api_events`に近い、mtimeチェック＋全量再読み込みの方式で実装した。

**同期I/Oのままにした判断根拠（レビュー中重大度指摘への対応）**: `_hvsr_history_snapshot()`のdocstringに、(1) `hvsr_history.jsonl`は週1レコードのみで10年運用でも520行程度に収まること、(2) 実測（520行・freq_hz/hv_ratio各81点を含む合成データ、20回平均）で`os.stat`が約0.02ms、全量読み込みが約8msと、1リクエストあたり10ms未満に収まること、(3) リクエスト頻度もWebUI起動時の1回のみで高頻度ポーリング対象ではないこと、を明記した。ファイルサイズ・リクエスト頻度が将来変わる場合はこの判断を見直し`run_in_executor`に処理を逃がすことを検討する旨も明記した。

### `src/templates/dashboard.html`

**パネル追加**: `hvsr-panel`を`#col-right`内`bphistory-panel`直後に追加。(a) ピーク周波数の週次推移（折れ線、新規アクセント色`#a371f7`）、(b) 最新週のHVSR曲線（対数X軸）の2チャート、SESAMEクライテリアバッジ（`window_length_ok`/`amplitude_ok`/`stability_ok`、満たす=緑・満たさない=グレー、赤系警報色は不使用）を実装した。起動時に`/api/hvsr_history`を1回fetchして描画し、WebSocket経由のリアルタイム更新は行わない（週次更新データのため、SharedStateには一切触れない）。

**レイアウト変更の詳細（レビュー中重大度指摘への対応）**: 実機ブラウザ確認（ヘッドレスChrome、`window-size=1600,700`で画面高さが低い環境を再現）で、既存の`#col-right { overflow: hidden }`のままだと5パネル目（`hvsr-panel`）がカラム高さを超えて完全に見切れる（スクロールバーが出ず末尾パネルが不可視になる）ことを確認した。これに対応するため以下の2点を変更した。

1. `#col-right`のCSSを`overflow: hidden`から`overflow-y: auto`に変更した。これによりカラム全体がスクロール可能になり、パネルが見切れる代わりにスクロールで到達できるようになる。
2. `#bphistory-panel`を`flex: 1 1 0`（残り空間を吸収）から`flex: 0 0 auto`（固定高さ、内部chartは`height: 110px`固定）に変更した。これは既存パネルの見た目に影響する変更である。理由は、5パネル構成でも`bphistory-panel`が可変で残り空間を吸収する設計のままだと、画面高さによってグラフの高さが不安定になり、新規`hvsr-panel`の表示位置・高さの予測が困難になるため。固定高さにすることで、収まらない分は`#col-right`のスクロールに委ねる設計に統一した。

Puppeteer（`puppeteer-core`、スクラッチパッド上に一時インストール、コミット対象外）を使い、`document.getElementById('col-right').scrollTop`を操作して実際にスクロール後の描画を確認し、HVSR曲線チャート・ステータスノート・SESAMEバッジが正しく表示されることを確認した（`scrollHeight`(1145px) > `clientHeight`(612px)でスクロール可能であることも実測で確認）。

### `scripts/launchd/com.riverruns.earthquake-hvsr-weekly.plist`

`com.riverruns.earthquake-web`の命名規則（`com.riverruns.*`）に揃えた。毎週月曜04:00 JST起動（`StartCalendarInterval`、`Weekday=1`）。`plutil -lint`でXML構文を検証済み。実際のiMacへの配置・`launchctl load`によるデプロイはスコープ外（設計書に明記の通り、developer/release-managerフェーズまたは実運用作業時に実施）。

### `docs/MANUAL.md`

新規セクション「16. HVSR週次モニタリング」を追加。目的・限界、`microseism.py`の既存H/V比計算との違い（手法・目的の比較表、レビュー低重大度指摘への対応）、実行方法、アルゴリズム概要、SESAME簡易クライテリアの記録範囲の限定、`GET /api/hvsr_history`のAPI仕様、本番運用（iMac側）を記載した。記載したファイルパス・関数名・定数値は実装コードと突き合わせて確認済み。

## テスト結果

| テストコマンド | 結果 |
|-------------|-----|
| `.venv/bin/python3 -m pytest src/test_hvsr_weekly.py -v` | PASSED（34件） |
| `.venv/bin/python3 -m pytest src/test_api_hvsr_history.py -v` | PASSED（12件） |
| `.venv/bin/python3 -m pytest src/test_api_events.py -v`（既存） | PASSED（23件、回帰なし） |
| `.venv/bin/python3 -m pytest src/verify_filter.py -v`（既存） | PASSED（41件、回帰なし） |
| `.venv/bin/python3 src/test_template_parity.py`（既存） | PASSED（exit code 0、回帰なし） |
| `plutil -lint scripts/launchd/com.riverruns.earthquake-hvsr-weekly.plist` | OK |
| `.venv/bin/python3 -m py_compile src/hvsr_weekly.py src/jma_intensity_web.py` | OK |

合計110件の自動テストが全て成功。既存テストスイート（`test_api_events.py`・`verify_filter.py`・`test_template_parity.py`）も回帰なしで全件成功を確認した。

### 実機ブラウザ確認

`.venv/bin/python3 src/jma_intensity_web.py`をテストポート（8091）で起動し、ヘッドレスChromeで以下を確認した。

- 通常サイズ（1600x1000）: 既存パネル（震度・STA/LTA・トリガ履歴・地図・P2P情報・帯域パワー・バンドパワー履歴）が崩れておらず、新規`hvsr-panel`がレイアウトの末尾に正しく追加されている
- 画面高さが低い環境（1600x700）: `#col-right`のスクロールで`hvsr-panel`全体（トレンドチャート・HVSR曲線チャート・ステータスノート・SESAMEバッジ）に到達可能であることをPuppeteerでのスクロール操作＋スクリーンショットで確認
- `GET /api/hvsr_history`がダミーデータ（4週分、`status`が`ok`/`insufficient_data`/`failed`各パターンを含む）を正しく返し、WebUI側で正しく描画されることを確認
- SESAMEバッジが`sesame_criteria`の真偽値に応じて緑（`ok`クラス）/グレーで表示されることを確認
- `status: "failed"`の週ではバッジが非表示になることを確認（設計書の仕様通り）
- **初回デプロイ直後の空状態（`data/hvsr_history.jsonl`が未生成、`GET /api/hvsr_history`が`{"count":0,"history":[]}`を返す状態）を、`hvsr_history.jsonl`を作成せずにサーバーを再起動して別途確認した。** ステータスノートに「HVSR履歴データがありません（初回計測待ち）。」が表示され、SESAMEバッジは3つとも非表示（`display: none`）、両チャートのcanvasは軸のグリッド線のみの空状態で正常描画され、ブラウザコンソールにJSエラーは出ない（`favicon.ico`の404のみで無関係）ことを確認した。設計書の本番デプロイ手順（実装タスク5項、手順7「`GET /api/hvsr_history`が空配列を返すことを確認する」）に対応する初回状態が正しく処理されることを実機で確認した

なお、`bash scripts/start_web.sh`によるVoiceVox込みの起動テストも試みたが、VoiceVox Dockerイメージの初回pullに時間がかかったため、代わりに`jma_intensity_web.py`を直接起動して検証した（VoiceVoxはHVSR機能と無関係のため、この代替で目的は達成できると判断した）。このセッション中に`voicevox-engine`コンテナが新規に作成・起動されている（`docker ps`の`CreatedAt`が本セッション開始時刻と一致）。

## 残課題・既知の制限

- 設計書のオープンクエスチョンに記載の通り、深夜取得ブロック時刻（02:00-05:00 JST）・launchd実行曜日時刻（毎週月曜04:00）・目標窓数（45窓）は暫定値であり、運用実績を見て調整が必要になる可能性がある
- iMac本番機へのデプロイ（scp転送・依存パッケージ確認・launchctl登録）は本実装のスコープ外。設計書「実装タスク」5項の手順に従い、別途developer/release-managerフェーズで実施する必要がある
- 実際の深夜帯データでの動作確認（取得量・処理時間・実際のHVSR曲線の形状）は未実施。開発機の既存MiniSEEDキャッシュ（7分程度の短時間データ）を使った統合テストのみ実施済み
- `bphistory-panel`のflex比変更（`flex: 1 1 0`→`flex: 0 0 auto`）は既存パネルの見た目を変更する。実機確認では問題ないことを確認したが、ユーザーの主観的な見た目の好みまでは確認していない

## reviewerへの引き継ぎ事項

- レビュー指摘の中重大度2件（リアルタイム性のブロッキング懸念・レイアウト見切れ懸念）への対応内容（`_hvsr_history_snapshot()`のdocstring、`#col-right`のCSS変更）を重点的に確認してほしい
- Konno-Ohmachi平滑化の`normalize=True`変更は設計書に明記がなかった実装上の判断のため、`src/hvsr_weekly.py`の`smooth_and_resample()`のコメント・実装意図を確認してほしい
- `#bphistory-panel`のflex比変更は既存パネルの挙動変更であるため、意図しない副作用がないか確認してほしい
- `scripts/launchd/*.plist`は既存の実際のlaunchd設定（iMac側の運用実績）と実際に照合していない（設計書のオープンクエスチョン6に記載の通り、未確認のまま新規作成した標準的な内容）
