#!/usr/bin/env bash
# Установка зависимостей Чушпана on macOS (Apple Silicon). Usage: ./scripts/setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

info()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[32m✔ %s\033[0m\n' "$*"; }
fail()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] \
  || fail "Установка поддерживается на macOS с Apple Silicon."

command -v brew >/dev/null || fail "Homebrew не найден. Установите его с https://brew.sh"

if [[ ! -f .env ]]; then
  cp .env.example .env
  info "Создан .env из шаблона. Укажите в нём имя, город и язык."
fi

if ! command -v uv >/dev/null; then
  info "Установка uv…"
  brew install uv
fi
ok "uv $(uv --version | awk '{print $2}')"

if ! command -v ollama >/dev/null; then
  info "Установка Ollama…"
  brew install ollama
fi
ok "Ollama установлен"

if ! curl -sf http://localhost:11434/api/version >/dev/null; then
  info "Запуск сервера Ollama…"
  brew services start ollama >/dev/null 2>&1 || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
  for _ in {1..30}; do
    curl -sf http://localhost:11434/api/version >/dev/null && break
    sleep 1
  done
  curl -sf http://localhost:11434/api/version >/dev/null || fail "Ollama не отвечает на порту 11434"
fi
ok "Сервер Ollama запущен"

info "Подготовка окружения Python…"
uv sync
ok "Зависимости Python готовы"

read_cfg() { uv run --quiet python -c "from backend.config import get_settings as g; s=g(); print($1)"; }

MODEL="$(read_cfg 's.llm.model')"
info "Загрузка модели $MODEL (в первый раз это займёт время)…"
ollama pull "$MODEL"
ok "модель $MODEL"

VOICE="$(read_cfg 's.voice_name')"
VOICE_PATH="$(read_cfg 's.voice_path')"
if [[ ! -f "$VOICE_PATH" ]]; then
  ENGINE="$(read_cfg 's.tts.engine')"
  mkdir -p "$(dirname "$VOICE_PATH")"
  if [[ "$ENGINE" == "silero" ]]; then
    info "Загрузка русской модели Silero…"
    curl -fL --progress-bar -o "$VOICE_PATH" "https://models.silero.ai/models/tts/ru/v5_6_ru.pt"
  elif [[ "$ENGINE" == "piper" ]]; then
    # pt_BR-faber-medium -> pt/pt_BR/faber/medium on Hugging Face
    IFS='-' read -r LOCALE SPEAKER QUALITY <<<"$VOICE"
    BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/${LOCALE%%_*}/${LOCALE}/${SPEAKER}/${QUALITY}/${VOICE}"
    info "Загрузка голоса $VOICE…"
    curl -fL --progress-bar -o "$VOICE_PATH" "$BASE.onnx"
    curl -fsL -o "$VOICE_PATH.json" "$BASE.onnx.json"
  fi
fi
ok "голос $VOICE"

STT_MODEL="$(read_cfg 's.stt.model')"
info "Загрузка модели распознавания речи $STT_MODEL…"
uv run --quiet python -c "from huggingface_hub import snapshot_download as d; d('$STT_MODEL')"
ok "модель распознавания $STT_MODEL"

WAKE_PHRASE="$(read_cfg 's.wakeword.phrase or ""')"
if [[ -n "$WAKE_PHRASE" ]]; then
  ok "Фраза активации «$WAKE_PHRASE»: используется Whisper, отдельная модель не нужна"
else
  WAKE_PATH="$(read_cfg 's.wakeword_path')"
  WAKE_DIR="$(read_cfg 's.wakeword_dir')"
  WAKE_BASE="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
  mkdir -p "$WAKE_DIR"
  for FILE in melspectrogram.onnx embedding_model.onnx; do
    [[ -f "$WAKE_DIR/$FILE" ]] || curl -fsL -o "$WAKE_DIR/$FILE" "$WAKE_BASE/$FILE"
  done
  if [[ ! -f "$WAKE_PATH" ]]; then
    info "Загрузка модели фразы активации $(basename "$WAKE_PATH")..."
    curl -fsL -o "$WAKE_PATH" "$WAKE_BASE/$(basename "$WAKE_PATH")" \
      || fail "Нет готовой модели фразы активации $(basename "$WAKE_PATH")"
  fi
  ok "фраза активации $(basename "$WAKE_PATH" .onnx)"
fi

if ! command -v npm >/dev/null; then
  info "Установка Node.js для сборки интерфейса…"
  brew install node
fi
info "Сборка интерфейса…"
npm --prefix frontend ci --no-audit --no-fund --loglevel=error
npm --prefix frontend run build --silent >/dev/null
ok "Интерфейс собран (frontend/dist)"

echo
uv run python -m backend doctor
