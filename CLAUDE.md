# earthQuake Project Instructions

## 環境

- Python仮想環境: `.venv`
- 起動: `bash scripts/start_web.sh`

## 主要スクリプト

- `src/jma_intensity_web.py` — HTTPダッシュボード本体
- `src/analyze_rs.py` — 波形解析・スペクトログラム
- `src/jma_intensity_realtime.py` — リアルタイム受信

## テスト

```bash
source .venv/bin/activate
python src/test_template_parity.py
```
