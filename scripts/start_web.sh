#!/bin/bash
# rs4d-jma-intensity Web版起動スクリプト
# VoiceVox を自動起動してから HTTP ダッシュボードを起動する

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT_DIR/.venv/bin/python3"
VOICEVOX_URL="http://localhost:50021"
VOICEVOX_WAIT=30  # 起動待ち最大秒数（Dockerイメージ初回pull後の起動を考慮）
# VOICEVOX ENGINE 単体（OSS）を Docker で起動する。
# エディタ込みの VOICEVOX.app は不要。エンジンが提供する HTTP API
# （audio_query / synthesis）だけを使うため、CPU 版イメージで完結する。
VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-latest"
VOICEVOX_CONTAINER="voicevox-engine"

LOG_FILE="/tmp/earthquake_web_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[INFO] ログファイル: $LOG_FILE"

# ===== 引数（Web へそのまま渡す） =====
WEB_ARGS="${*:---station R38DC --web-port 8080}"

# ===== 依存パッケージ確認 =====
for pkg in websocket fastapi uvicorn; do
    if ! "$VENV" -c "import $pkg" 2>/dev/null; then
        echo "[INFO] $pkg をインストールします..."
        "$VENV" -m pip install "$pkg" -q
    fi
done

# ===== VoiceVox ENGINE（Docker）起動 =====
echo "[INFO] VoiceVox Engine を確認中..."

if curl -s --max-time 2 "$VOICEVOX_URL/version" > /dev/null 2>&1; then
    echo "[INFO] VoiceVox Engine はすでに起動しています。"
elif ! command -v docker > /dev/null 2>&1; then
    echo "[WARN] docker が見つかりません。macOS say にフォールバックします。"
else
    # 同名コンテナが停止状態で残っていれば再利用、なければ新規 run。
    # 失敗してもスクリプトを止めず say にフォールバックさせる（|| true）
    if docker ps -a --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
        echo "[INFO] 既存の $VOICEVOX_CONTAINER コンテナを起動します..."
        docker start "$VOICEVOX_CONTAINER" > /dev/null || true
    else
        echo "[INFO] VoiceVox ENGINE コンテナを起動します（初回はイメージ取得に時間がかかります）..."
        docker run -d --name "$VOICEVOX_CONTAINER" \
            -p 50021:50021 \
            --restart unless-stopped \
            "$VOICEVOX_IMAGE" > /dev/null || true
    fi

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
fi

# ===== Web 起動 =====
echo "[INFO] Web ダッシュボードを起動します: $WEB_ARGS"
echo "[INFO] ブラウザで http://localhost:8080 を開いてください。"
"$VENV" "$ROOT_DIR/src/jma_intensity_web.py" $WEB_ARGS
