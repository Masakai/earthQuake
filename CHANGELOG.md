# CHANGELOG

## [Unreleased]

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
