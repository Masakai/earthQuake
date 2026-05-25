# CHANGELOG

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

Copyright (c) 2026 株式会社リバーランズ・コンサルティング
