# earthQuake Project Instructions

## 環境

- Python仮想環境: `.venv`
- 起動: `bash scripts/start_web.sh`

## 主要スクリプト

- `src/jma_intensity_web.py` — Web ダッシュボード本体（FastAPI + uvicorn、HTTP 8080 / UDP 8888）
- `src/jma_intensity_tui.py` — SharedState・計算ループ・音声アラート（web.py が import するコア）
- `src/jma_intensity_realtime.py` — JMA フィルタ・Ring バッファ・震度換算
- `src/analyze_rs.py` — 波形解析・スペクトログラム
- `src/monthly_report.py` / `run_monthly_report_if_last_day.py` — 月次レポート生成（launchd）
- `src/fetch_p2p_daily.py` — P2P地震情報の日次キャッシュ収集（launchd）

## 音声アラート

- 現在は macOS `say -v Kyoko` 固定（**macOS 専用**）。VoiceVox コードは `AlertSpeaker._check_voicevox()` にコメントアウトで保持（合成遅延のため無効）。

## テスト

```bash
source .venv/bin/activate
python src/test_template_parity.py   # Jinja2 テンプレート整合
pytest src/verify_filter.py -v       # JMA フィルタ・震度計算（41テスト）
pytest src/test_api_events.py -v     # /api/events（23テスト）
```

## バージョン

- 現行 v1.5.0。`src/jma_intensity_web.py` の `__version__` を git タグと揃えて手動更新する。
