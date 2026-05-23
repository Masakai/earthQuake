#!/bin/bash
# rs4d-jma-intensity Web版起動スクリプト
# VoiceVox を自動起動してから HTTP ダッシュボードを起動する

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT_DIR/.venv/bin/python3"
VOICEVOX_APP="/Applications/VOICEVOX.app"
VOICEVOX_URL="http://localhost:50021"
VOICEVOX_WAIT=15  # 起動待ち最大秒数

# ===== 引数（Web へそのまま渡す） =====
WEB_ARGS="${*:---station R38DC --web-port 8080}"

# ===== 依存パッケージ確認 =====
for pkg in websocket fastapi uvicorn; do
    if ! "$VENV" -c "import $pkg" 2>/dev/null; then
        echo "[INFO] $pkg をインストールします..."
        "$VENV" -m pip install "$pkg" -q
    fi
done

# ===== VoiceVox 起動 =====
echo "[INFO] VoiceVox Engine を確認中..."

if curl -s --max-time 2 "$VOICEVOX_URL/version" > /dev/null 2>&1; then
    echo "[INFO] VoiceVox Engine はすでに起動しています。"
else
    if [ -d "$VOICEVOX_APP" ]; then
        echo "[INFO] VoiceVox を起動します..."
        open -a "$VOICEVOX_APP"

        echo -n "[INFO] Engine 起動待ち"
        for i in $(seq 1 $VOICEVOX_WAIT); do
            sleep 1
            echo -n "."
            if curl -s --max-time 1 "$VOICEVOX_URL/version" > /dev/null 2>&1; then
                echo ""
                echo "[INFO] VoiceVox Engine 起動完了。"
                break
            fi
            if [ "$i" -eq "$VOICEVOX_WAIT" ]; then
                echo ""
                echo "[WARN] VoiceVox Engine がタイムアウトしました。macOS say にフォールバックします。"
            fi
        done
    else
        echo "[WARN] VOICEVOX.app が見つかりません。macOS say にフォールバックします。"
    fi
fi

# ===== Web 起動 =====
echo "[INFO] Web ダッシュボードを起動します: $WEB_ARGS"
echo "[INFO] ブラウザで http://localhost:8080 を開いてください。"
exec "$VENV" "$ROOT_DIR/src/jma_intensity_web.py" $WEB_ARGS
