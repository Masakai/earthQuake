# CHANGELOG

## [1.5.1] - 2026-06-27

### Added
- `templates/dashboard.html`: 地図上の観測点震度バッジの表示ON/OFFトグルを追加
  - 右下の震度凡例の最上部にチェックボックス「観測点バッジ」を配置（デフォルトON）
  - OFFにすると観測点ごとの震度バッジを非表示にし、地震を選択し直しても描画しない
  - 状態を `localStorage`（キー `badgeVisible`）に永続化し、リロード後も維持
  - 凡例本体は `pointer-events:none` のため、トグル行のみクリック可能にし `L.DomEvent.disableClickPropagation` で地図への伝播を遮断
  - 制御は `drawBadgeLayer` 冒頭の1判定に集約（呼び出し箇所が複数あるため漏れを防止）

## [1.5.0] - 2026-06-25

### Changed
- `jma_intensity_tui.py`: 地震警報の発話を STA/LTA トリガから切り離し、ライブの計測震度 `I_final` を監視する方式に変更
  - 背景: 旧実装はトリガ発火の `confirm_window` 秒後（既定10秒）の窓値で発話判定していた。STA/LTA 比のピーク（発火）から 90秒窓の計測震度 I が立ち上がるまで数十秒のラグがあり、発火直後の立ち上がり前の値（震度0）で判定するため、ダッシュボード表示が震度1・2を示しても発話されなかった（履歴 2226件中 I≥0.5 はわずか3件だった）
  - 遠地地震（S-P 時間が長く発火から震度立ち上がりまで時間がかかる）でも近地地震でも、計測震度が実際に 0.5（震度1相当）を超えた時点で発話する
  - 0.5 超過から `speak_delay` 秒ピークを観測してから初回発話。発話後に震度スケールが1段階以上上がったら、再生中の `say` を `terminate()` して新しい値で言い直す（同一スケール内の数値変動では言い直さない）
  - 新引数 `--speak-delay`（既定 2.0秒）を `jma_intensity_tui.py` / `jma_intensity_web.py` に追加
  - `_say_speak` を `_say_popen`（Popen ハンドルを返す非ブロッキング版）に変更し、言い直しのため `AlertSpeaker` にプロセスハンドル（`_cur_proc` / `_proc_lock`）を保持
- `jma_intensity_tui.py`: トリガ履歴に記録する計測震度を、確定時点の窓値から `confirm_window` 期間内のピーク値に変更
  - `pending_queue` の各イベントで `I_final` の最大値を追跡し、確定時にピーク値（とスケール）を `add_event` に渡す
  - 本番 iMac で発話エンジン（`say -v Kyoko`）が 8.2秒の発話を実行し音声出力されることを確認

### Added
- `jma_intensity_web.py`: アプリバージョンを定義（`__version__ = "1.5.0"`）し、ダッシュボードのステータスバーに `v1.5.0` を表示。デプロイ反映を画面から確認できるようにした
  - 外部確認用に `GET /api/version`（→ `{"version": "1.5.0"}`）を追加。バージョンはリリース時に git タグと揃えて手動更新する

## [1.4.0] - 2026-06-16

### Added
- `analyze_rs.py`: 公式FDSN（data.raspberryshake.org）にデータが無い場合、自局Raspberry Shake内蔵のSeedLink（`10.0.1.53:18000`、遅延約7秒）から波形を取得するフォールバックを追加
  - 背景: 公式FDSNは発生直後のデータが20〜30分遅れるため、発生直後の解析が空振りしていた
  - `download_channel` を二段化し、公式FDSNが空のとき `download_channel_seedlink`（新規）へフォールバック
  - 接続先は `RS_SEEDLINK_HOST` / `RS_SEEDLINK_PORT`（`.env` で上書き可）
  - 本番（発生直後シナリオ）で公式FDSN空→自局SeedLinkから35KB取得成功を確認

### Fixed
- `analyze_rs.py`: 公式FDSN / P2P API への HTTPS を certifi の証明書バンドルで検証するよう修正
  - 原因: python.org 製 Python 3.12.1（本番）で `ssl.get_default_verify_paths()` の cafile/capath が共に None となり、HTTPS が `CERTIFICATE_VERIFY_FAILED` で全滅していた
  - モジュール冒頭で `certifi.where()` から `_SSL_CTX = ssl.create_default_context(cafile=...)` を生成し、全 urlopen（P2P×2・FDSN station・FDSN dataselect）に `context=_SSL_CTX` を渡す
  - certifi 未導入環境では `None` で標準挙動を維持（システム Python 等は無変更）
- `analyze_rs.py`: SeedLink フォールバックで終端が未来の区間を要求するとハングする問題を修正
  - 原因: SeedLink は終端が未来の区間を要求するとデータ到着までブロックし続ける（obspy SeedLink Client の timeout はこの待機に効かない）。WebUI は発生直後の地震を `--duration 420` で解析するため要求区間の終端が未来になりハングしていた
  - `download_channel_seedlink` で `get_waveforms` 前に終端を「現在UTC−5秒」でクランプ。区間全体が未来なら即時 False を返す
  - WebUI 相当（発生直後+duration420）の E2E がローカル12秒・本番14秒で完走することを確認

## [1.3.0] - 2026-06-15

### Added
- `jma_intensity_web.py`: 読み取り専用 API `GET /api/events` を追加（統合システム fujimidai-observatory 連携）
  - トリガ履歴 `logs/trigger_log.jsonl` を HTTP 経由・読み取り専用で取得するエンドポイント
  - クエリパラメータ（すべて任意）: `date`（YYYY-MM-DD）/ `from` / `to`（期間）/ `limit`（新しい順、デフォルト 1000、0〜10000）/ `min_scale`（震度順序で比較、それ以上のみ）
  - ヘルパー `_read_trigger_events()` を新設。震度は `_SCALE_ORDER`（"5弱"/"5強" 等を正しく扱う）で比較
  - 正常レスポンス形式: `{"count": N, "events": [...]}`（既存キー date/ts/I/scale/ratio を保持、新しい順）
  - 入力契約: 不正な `min_scale`（"0".."4","5弱","5強","6弱","6強","7" 以外）→ HTTP 422、`limit` が 0〜10000 の範囲外 → HTTP 422
  - I/O・デコード障害は握りつぶさず伝播（障害をイベント0件に化けさせない方針）
  - **無認証エンドポイント**。`10.0.1.28:8080` が LAN 外へ露出していないこと（非露出）を確認のうえリリース（2026-06-15）
- `test_api_events.py`: 上記 API のテスト 23 件を追加（正常系・境界・異常系 422・UTF-8 多バイト・I/O エラー伝播）

## [1.2.1] - 2026-06-14

### Changed
- `dashboard.html`: トリガ閾値（STA/LTA）設定スライダーの上限を `max="10.0"` → `max="20.0"` に拡張。Web UI から閾値を最大20まで設定可能になった
  - 背景: 閾値16で運用しているが従来の上限10では設定できなかったため
  - サーバ側 `_CONFIG_LIMITS`（`jma_intensity_web.py`）の trig 上限は元々 (0.5, 50.0) で20を許容済みのため、変更はこの1箇所のみで完結

## [1.2.0] - 2026-06-14

### Changed
- `jma_intensity_tui.py`: 地震警報の話速を震度に応じて変更。震度5以上（5弱/5強/6弱/6強/7）は `say -r 240`（約240wpm）で発声し緊迫感を出す。震度4以下は Kyoko のデフォルト話速（約175wpm）のまま
  - `_say_speak(text, rate=None)` に話速引数を追加
- `start_web.sh`: VoiceVox ENGINE の起動方式を VOICEVOX.app（`open -a`）から Docker コンテナ（`voicevox/voicevox_engine:cpu-latest`）方式へ変更
  - エディタ込みアプリは不要となり HTTP API（audio_query / synthesis）のみで完結
  - docker 未インストール時は macOS `say` にフォールバック
  - イメージ初回取得を考慮し Engine 起動待ちを最大15秒→30秒に延長

## [1.1.1] - 2026-06-01

### Fixed
- `dashboard.html`: トリガ履歴とP2P地震履歴のリンク判定を改善
  - 震源座標が既知の場合、P波推定到達時刻（斜距離 ÷ 6.0 km/s）を基準に ±30秒で同一判定
  - 震源座標が不明な場合（ScalePrompt等）は従来の固定 ±5分窓にフォールバック
  - 固定10分窓に比べ、遠方地震での誤リンクを排除し近方地震の精度も向上

## [1.1.0] - 2026-06-01

### Fixed
- `analyze_rs.py`: STA/LTA の LTA 区間からSTA区間を除外し Withers et al. (1998) 標準定義に準拠
  - 従来実装はLTAにSTA区間を含む定義誤りで、ピーク時の ratio が約1/8に低下していた
- `jma_intensity_web.py`: バンドパワーをdBではなくパワー真値（線形値）で蓄積し、1分平均後にdB変換することで 0.1dB 丸め誤差を排除（精度 0.01dB）
- `fetch_p2p_daily.py`: issue_type の優先順位（ScalePrompt < Destination < DetailScale）で同一時刻の複数報を正しく統合するよう修正

### Added
- `analyze_rs.py`: WebUI 設定（`~/.config/jma_intensity/config.json`）から sta/lta/trig を自動読み込みし、解析グラフの閾値線を WebUI と一致させる
- `analyze_rs.py`: STA/LTA グラフのタイトルに使用センサ名（EHZ または EN成分）を表示

## [1.0.0] - 2026-05-28

### Added
- `jma_intensity_tui.py` / `jma_intensity_web.py`: EHZ チャネル（速度計）を STA/LTA トリガ検出に使用するデュアルチャネル方式を実装
  - EHZ に Butterworth 4次バンドパスフィルタ（1〜10Hz）を適用して STA/LTA を算出
  - I 値計算は従来通り ENZ/ENN/ENE（MEMS加速度計）3成分を使用
  - EHZ 未受信時は ENZ 単体にフォールバック
- `--confirm-window` 引数を新設（既定 10.0 秒）
  - `--rt-window`（I 値計算窓長）からトリガ確認待ち時間を分離し、アラート発報ラグを約 90 秒 → 10 秒に短縮
- I 値フィルタ: 計測震度 I < 0.5 の場合は音声アラートを発報しない（誤検出抑制）
- パケット伝送ラグ（`pkt_lag`）の計測・WebSocket ブロードキャスト・ヘッダー表示（`lag: X.XXs`）
- アラート遅延ログ: トリガ検出から発話完了までの時間を `~/Dropbox/earthQuake/logs/alert_latency.jsonl` に記録
- `dashboard.html`: 設定パネルに「確認窓」スライダー（1〜60 秒）を追加

### Changed
- 音声エンジン: VoiceVox 合成遅延のため、現在は macOS `say -v Kyoko` に固定（VoiceVox コードは維持・コメントアウト）

## [0.9.4] - 2026-05-27

### Added
- `dashboard.html`: 気象庁観測点マスターを使った震度バッジ表示（JMA 観測点座標に DivIcon バッジ）
- `README.md` / `docs/MANUAL.md` / 解析画像フッター: K-NET/KiK-net データ謝辞（NIED）を追加

### Fixed
- `dashboard.html`: `extractCity` 正規表現バグ修正（「大町市役所」→「大町市」が「大町」になっていた問題）
- `dashboard.html`: WSプッシュでバッジが消えていた問題を修正（cityPane z-index 調整 + pinnedQuake 保持）
- `dashboard.html`: コードレビュー指摘10件の修正（XSS対策・続報検知・エラーログ改善等）

### Changed
- `scripts/start_web.sh`: ログファイル出力を追加

## [0.9.3] - 2026-05-27

### Changed
- **著作権者を `株式会社リバーランズ・コンサルティング` から `Masanori Sakai` に全面変更**
  - 対象16ファイル: README.md, docs/CHANGELOG.md, docs/MANUAL.md, docs/index.html, docs/mockup.html, docs/infrasound_plumbing.html, src/analyze_knet.py, src/analyze_rs.py, src/jma_intensity_realtime.py, src/jma_intensity_tui.py (2箇所), src/jma_intensity_web.py, src/replay_udp.py, src/simulate_udp.py, src/templates/dashboard.html, src/verify_filter.py
  - 理由: 個人著作物として明確化し、VOICEVOX 等の外部リソースの「商用/非商用」判定をシンプル化（個人OSS = 非商用扱いとして整理しやすくする）
- `src/jma_intensity_tui.py`: VoiceVox デフォルト話者を `No.7（アナウンス）` から `青山龍星（ノーマル）` に変更
  - 理由: No.7 はクレジット非表示の商用利用が 250,000円、クレジットあり 15,000円〜と有償。青山龍星は東北ずん子・ずんだもんプロジェクト系のキャラクターで、クレジット表記「VOICEVOX:青山龍星」のみで商用利用可能
  - 緊急地震速報のトーンに合う重厚な男性声を維持
  - `AlertSpeaker` の定数名を `ZUNDAMON_NAME` / `ZUNDAMON_STYLE` から `SPEAKER_NAME` / `SPEAKER_STYLE` にリネーム（実体に合わせて整理）
- `README.md` / `docs/MANUAL.md`: 音声クレジット表記「VOICEVOX:青山龍星」の記載を必須化する案内を追加
  - 共通禁止事項（公序良俗違反、政治・宗教活動、情報商材、フェイク情報、風俗営業、反社会的勢力による利用等）に注意喚起
  - 話者変更時の手順（`AlertSpeaker.SPEAKER_NAME` 変更）と他キャラクター利用時の規約確認を明記

## [0.9.2] - 2026-05-27

### Added
- `CLAUDE.md`: プロジェクト指示書（環境・主要スクリプト・テスト手順）を追跡対象に追加
- `国内RS4D.json`: 国内 Raspberry Shake 4D 観測点一覧（27局、2026-05-24時点）を追跡対象に追加
  - data.raspberryshake.org FDSN Station API + OpenStreetMap Nominatim から取得
  - 各局の緯度経度・標高・所在地（都道府県・市区町村）・稼働開始日を収録
- `docs/infrasound_plumbing.html`: インフラサウンドセンサー配管設計書（SDP810-125Pa + ATOM S3）を追跡対象に追加

### Changed
- `.gitignore`: `src/data/` を除外対象に追加（analyze_rs.py の生成物 .ms / .png の置き場）

## [0.9.1] - 2026-05-27

### Added
- `src/microseism.py`: R38DC マイクロセイズム診断図生成スクリプトを追加
  - ENZ・ENE・ENN 3成分（MEMS加速度計）を計器応答除去し m/s 単位に統一
  - スペクトログラム、平均PSD（Welch法）、ピーク検出、H/V比、帯域パワー時系列、昼夜比較を1枚にまとめた診断図を生成
  - 個別パネルPNG出力とHTMLレポート出力に対応
  - H/V計算は線形パワーで合成（H = √(ENE_power + ENN_power), V = √(ENZ_power)）
- `README.md` / `docs/MANUAL.md`: インストール手順に scipy / matplotlib を明記

## [0.9.0] - 2026-05-27

### Added
- `src/analyze_knet.py`: NIED 強震観測網（K-NET / KiK-net）の ASCII 強震記録（3成分: NS, EW, UD）をローカルから読み込み、計測震度・スペクトログラム・震源マップを出力する解析スクリプトを追加
- `data/knet/README.md`: K-NET / KiK-net データ配置ディレクトリの使用方法を追加
- `README.md` / `docs/MANUAL.md`: 計測震度算出ロジックの正当性検証セクションを追加
  - NIED 公式震度データベース値（2026-05-20 11:46 イベント、M5.9）との照合結果を記載
  - KGS035（与論、震央距離53km）: 公式値 312.3 gal / I=5.0 → 解析値 293.9 gal / I=5.08（震度階級完全一致）
  - KGS034（知名、震央距離19km）: 公式値 150.0 gal / I=4.2 → 解析値 147.4 gal / I=4.29（震度階級完全一致）
  - 検証範囲（jma_intensity_realtime.py + analyze_rs.py + analyze_knet.py）と検証範囲外（リアルタイム機能・震度2以下・震度6弱以上・遠地イベント等）を明示

### Fixed
- `src/analyze_knet.py`: obspy K-NET reader の `Trace.stats.calib` が m/s²/count（gal/count ではない）を返す仕様への対応漏れを修正
  - `load_knet_traces` で `tr.data * calib * 100.0` として gal 単位に正しく変換
  - 修正前は加速度が約 1/100 で算出されており、計測震度が公式値と大きく乖離していた

## [0.8.0] - 2026-05-25

### Added
- `src/download_geojson.py`: 国土数値情報（国土交通省、PDL1.0）から全47都道府県・1905市区町村のGeoJSONをダウンロードし `data/geojson/{pref}/{city}.json` に保存するスクリプトを追加
- `jma_intensity_web.py`: `/api/geojson/{pref}` (市区町村コード一覧) と `/api/geojson/{pref}/{city}` (GeoJSON取得) の2エンドポイントを追加

### Changed
- `dashboard.html`: 地図タイルを CartoDB light_all（英語・多言語混在）から国土地理院 blank（日本語・境界線のみ）に変更
- `dashboard.html`: 市区町村GeoJSONの取得元をGitHubの無ライセンスリポジトリへの直接fetchからサーバーAPIに変更
  - データソース: 国土交通省国土数値情報（PDL1.0）— ライセンス明確・高精度（市区町村ポリゴン精度約50倍改善）
  - 外部依存を排除しサーバーローカルのファイルから提供
- GeoJSONスタイル: 震度ありの市区町村は境界線も同色・半透明にして隙間を解消

### Fixed
- 市区町村GeoJSONの精度が低く（一部57頂点）、隣接市区町村間に視覚的な隙間が生じていた問題を、国土数値情報（2823頂点等）への切り替えで根本解決

## [0.7.0] - 2026-05-25

### Added
- `analyze_rs.py`: 震源地図に観測点中心の推定距離圏を青破線で描画
  - P2P震源情報の震源距離（公式値）を `dist_km_sp` として `plot_map()` に渡し、緯度補正済み楕円を360点折れ線で描画
- `analyze_rs.py`: Si-Midorikawa (1999) 距離減衰式によるグラフタイトルへの推定M表示
  - `log10(a) = 0.61M - 1.73*log10(r) - 0.00030*r + 0.167` の逆算
  - 震源距離 200km 超では「参考値」、200km 以内では「±0.5程度」と注記
  - 公式Mとの比較表示により正式報との照合が容易に

## [0.6.0] - 2026-05-25

### Added
- `analyze_rs.py`: データギャップをグラフ上に赤帯で可視化（`⚠ギャップXs` ラベル付き）
  - obspy `get_gaps()` でギャップを検出し、NaN埋めマージで全区間を連続配列として処理
  - `plot_analysis()` に `gap_spans` 引数を追加、全時系列パネルに `axvspan` で描画

### Fixed
- データギャップ後の STA/LTA 誤警報を多重防御で完全修正（`jma_intensity_tui.py`）
  - **根本原因**: ギャップ後の最初のパケットで `dt` が大きくなり `fs_est` が狂い `nlta` が誤計算されていた
  - **修正1**: `compute_loop` が LTA秒数（デフォルト20秒）無音を検出し能動的に Ring と `shared.fs` をリセット
  - **修正2**: `recv_loop_fn` でパケット間隔 `dt > 3秒` を検出した場合も同様にリセット
  - **修正3**: `s_lta < 1e-12` ガードを追加（LTA が実質ゼロの場合は ratio = 0.0 を返す）
- `analyze_rs.py`: `stable_idx` が配列長を超える場合のクラッシュ（ValueError）を修正

## [0.5.0] - 2026-05-25

### Added
- `src/templates/dashboard.html`: Jinja2 テンプレートとして HTML を分離
- `src/test_template_parity.py`: テンプレートレンダリング検証テスト（3テスト）

### Fixed
- `compute_intensity_timeseries`: JMA公式定義「合計0.3秒以上継続する最大値」に修正（従来は連続0.3秒判定）
- JMAフィルタ適用前に DC除去（平均除去）を追加し、重力バイアスが計測震度に混入するバグを修正
- `pending_event`（単一変数）を FIFO キュー化し、余震連発時に後続イベントが上書き消失するバグを修正
- `_analyze_jobs`、`p2p_seen_ids`、`p2p_eew` の3か所で長期稼働時にメモリが無制限増加する問題を修正

### Changed
- `src/jma_intensity_web.py`: `_make_html()` 関数（約1500行の f-string）を削除し、Jinja2 `FileSystemLoader` でレンダリングするよう変更
- `src/verify_filter.py`: モジュールレベル実行から pytest テストスイートに全面書き直し（41テスト）

## [0.4.0] - 2026-05-24

### Added
- Web版: P2P地震情報テーブルの各行に「解析」ボタンを追加
  - クリックで `analyze_rs.py` をサブプロセス起動し、自局の波形をダウンロード・解析
  - 解析完了後に波形解析画像をモーダルで表示（ポーリング方式、最大120秒待機）
  - `POST /api/analyze`, `GET /api/analyze/{job_id}`, `GET /api/analyze_img/{job_id}` エンドポイント追加
- Web版: P2P地震情報の履歴表示件数を20件に拡張（従来10件）

## [0.3.1] - 2026-05-24

### Added
- P2P地震情報 WebSocket API によるリアルタイム受信（60 秒ポーリングから移行）
- EEW（緊急地震速報・警報）の TUI 参考表示（P2P経由・無保証、音声アラートなし）
- WebSocket 未接続時は HTTP ポーリングへ自動フォールバック
- VoiceVox（No.7 アナウンス）による震度別音声アラート
- macOS say (Kyoko) へのフォールバック
- 震度別警告語（揺れを検出 / 注意！ / 警告！ / 緊急警報！）
- 震度別注意喚起メッセージの読み上げ
- `simulate_udp.py` に `--quiet-sec` オプション（静穏期間の設定）
- STA/LTA 用バッファを rt-window と独立して確保（rt-window < lta でも正常動作）
- トリガ後 rt-window 秒待機してから確定 I 値をトリガ履歴に記録（pending_event 方式）

### Fixed
- チャンネル名に先頭スペースが混入してパケットが無視されるバグ
- `socket.timeout` で受信スレッドが終了するバグ
- STA/LTA が常に 1.0 になる問題（`--quiet-sec` による静穏期間で解消）
- トリガ直後に I 値が低く記録される問題（rt-window 待機で解消）

### Changed
- `jma_intensity_rs4d.py` を削除（`jma_intensity_realtime.py` に統合済み）
- 音声アラートをシステムサウンドから VoiceVox 音声読み上げに変更
- 音声速度を 1.1 倍に設定

## [0.3.0] - 2026-05-23

### Added
- `jma_intensity_web.py`: FastAPI + WebSocket によるブラウザ版ダッシュボード
  - 1秒ごと WebSocket ブロードキャスト
  - Leaflet.js による震源地図（×印マーカー、M比例サイズ）
  - 市区町村単位の震度色分け表示（都道府県別 GeoJSON を並列 fetch・キャッシュ）
  - 震度カラー凡例（気象庁10段階準拠）
  - `navigator.geolocation` による震源までの直線距離表示（ハバーサイン公式）
  - トリガ履歴クリック時に P2P 地震情報と時刻照合してズーム表示
  - バックグラウンドタブ離脱時に WebSocket 自動切断・復帰時再接続
  - `requestAnimationFrame` デバウンスによる連続メッセージの1フレーム集約
- 全ソースファイルに著作権表示を追加
- TUI フッターに著作権表示を追加

## [0.1.0] - 2026-05-22

### Added
- `jma_intensity_realtime.py`: JMA 計測震度コアライブラリ
  - `jma_frequency_response`: JMA フィルタ周波数応答
  - `apply_jma_filter_time`: 時間領域での JMA フィルタ適用
  - `a_threshold_for_03s`: 0.3 秒閾値による加速度算出
  - `jma_scale_from_I`: 計測震度から震度階級への変換
  - `parse_udp_packet`: RS DATACAST パケットパーサ
  - `Ring`: リングバッファ
- `jma_intensity_tui.py`: rich による TUI ダッシュボード
  - 震度バー・波形グラフ（スパークライン）・STA/LTA バー・トリガ履歴
  - 3 スレッド構成（recv_loop / compute_loop / 描画）
- `simulate_udp.py`: 任意震度の合成 UDP パケット送出シミュレーター
- `verify_filter.py`: JMA フィルタ特性の検証スクリプト（5 項目）
- `data/R38DC.xml`: StationXML

---

Copyright (c) 2026 Masanori Sakai
