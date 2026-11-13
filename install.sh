#!/usr/bin/env bash
# Установка «Оркестра» на Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/t1murjan/orkestr/main/install.sh | bash
#
# Повторный запуск обновляет программу. Переменные:
#   ORKESTR_DIR   куда положить программу (по умолчанию ~/projects/orkestr)
#   ORKESTR_REPO  откуда скачивать (по умолчанию https://github.com/t1murjan/orkestr)
#   ORKESTR_PORT  порт пульта (по умолчанию 8787)
set -euo pipefail

REPO="${ORKESTR_REPO:-https://github.com/t1murjan/orkestr}"
DIR="${ORKESTR_DIR:-$HOME/projects/orkestr}"
PORT="${ORKESTR_PORT:-8787}"
BIN_DIR="$HOME/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/orkestr"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/orkestr"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say()  { printf '\033[1;36m›\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m×\033[0m %s\n' "$*" >&2; exit 1; }

# --- проверки ------------------------------------------------------------
command -v git >/dev/null || die "Нужен git: sudo apt install git (или пакет git вашего дистрибутива)"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || die "Нужен Python 3.11 или новее. Сейчас: $(python3 --version 2>&1 || echo 'не установлен')"
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
  || die "Нет модуля venv. Установите: sudo apt install python3-venv (для $("$PY" --version))"

# --- скачать или обновить -----------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Обновляю $DIR"
  git -C "$DIR" pull --ff-only --quiet || die "Не удалось обновить: в $DIR есть локальные изменения или ветки разошлись"
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "Папка $DIR уже существует и не пуста. Укажите другую: ORKESTR_DIR=~/путь bash install.sh"
else
  say "Скачиваю $REPO в $DIR"
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet "$REPO" "$DIR" \
    || die "Не удалось скачать. Если репозиторий приватный, войдите в GitHub (gh auth login) или клонируйте его сами и запустите ./install.sh из папки"
fi

# --- зависимости ---------------------------------------------------------
say "Ставлю зависимости (виртуальное окружение в $DIR/.venv)"
[ -x "$DIR/.venv/bin/python" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$DIR/.venv/bin/python" -m pip install --quiet -e "$DIR"

# --- где лежат агенты ----------------------------------------------------
# Из меню приложений PATH короче, чем в терминале, поэтому запоминаем, где
# сейчас находятся claude и opencode, и добавляем эти папки в команду запуска.
EXTRA_PATH=""
for tool in claude opencode; do
  if p="$(command -v "$tool" 2>/dev/null)"; then
    d="$(dirname "$(readlink -f "$p")")"; d2="$(dirname "$p")"
    EXTRA_PATH="$EXTRA_PATH:$d2:$d"
  else
    warn "$tool не найден в PATH — агенты этой среды будут недоступны, пока его не установите"
  fi
done
for d in "$HOME/.local/bin" "$HOME/.opencode/bin" "$HOME/.npm-global/bin" "$HOME/.bun/bin" "$HOME/.claude/local"; do
  EXTRA_PATH="$EXTRA_PATH:$d"
done
EXTRA_PATH="${EXTRA_PATH#:}"

# --- команда orkestr -----------------------------------------------------
mkdir -p "$BIN_DIR" "$DATA_DIR" "$STATE_DIR"
cat > "$BIN_DIR/orkestr" <<EOF
#!/usr/bin/env bash
# Запуск «Оркестра». Создано install.sh — перезапишется при обновлении.
#   orkestr          запустить (если ещё не запущен) и открыть в браузере
#   orkestr --demo   то же, с демо-командой агентов
#   orkestr stop     остановить
#   orkestr status   запущен ли
#   orkestr log      журнал сервера
#   orkestr update   обновить программу
set -u
DIR="$DIR"; PORT="\${ORKESTR_PORT:-$PORT}"
DATA_DIR="$DATA_DIR"; STATE_DIR="$STATE_DIR"
URL="http://127.0.0.1:\$PORT"
PIDFILE="\$STATE_DIR/server.pid"; LOG="\$STATE_DIR/server.log"
export PATH="$EXTRA_PATH:\$PATH"

alive() { curl -fs --noproxy '*' -m 2 "\$URL/api/v1/health" >/dev/null 2>&1; }
open_ui() {
  for o in xdg-open gio sensible-browser; do
    if command -v "\$o" >/dev/null; then
      if [ "\$o" = gio ]; then gio open "\$URL" >/dev/null 2>&1 & else "\$o" "\$URL" >/dev/null 2>&1 & fi
      return
    fi
  done
  echo "Откройте в браузере: \$URL"
}
stop() {
  if [ -f "\$PIDFILE" ] && kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null; then
    kill "\$(cat "\$PIDFILE")"
    for _ in \$(seq 1 50); do kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null || break; sleep 0.1; done
    rm -f "\$PIDFILE"; echo "Оркестр остановлен"
  else
    rm -f "\$PIDFILE"; echo "Оркестр не запущен"
  fi
}

case "\${1:-}" in
  stop)   stop; exit 0 ;;
  status) if alive; then echo "Работает: \$URL"; else echo "Не запущен"; exit 1; fi; exit 0 ;;
  log)    exec tail -n 100 -f "\$LOG" ;;
  update) exec bash "\$DIR/install.sh" ;;
  -h|--help) sed -n '3,9p' "\$0"; exit 0 ;;
esac

if alive; then
  [ "\${1:-}" = "--demo" ] && echo "Уже запущен без демо. Сначала: orkestr stop"
  open_ui; exit 0
fi

# Папка проектов по умолчанию для агентов — та, откуда запущена команда,
# а из меню приложений — ~/projects (или домашняя).
WORKDIR="\$PWD"
[ "\$WORKDIR" = "/" ] && WORKDIR="\$HOME"
if [ -z "\${TERM:-}" ] || [ "\$WORKDIR" = "\$HOME" ]; then
  [ -d "\$HOME/projects" ] && WORKDIR="\$HOME/projects"
fi
cd "\$WORKDIR"

nohup "\$DIR/.venv/bin/python" -m orkestr --port "\$PORT" --db "\$DATA_DIR/orkestr.db" "\$@" >>"\$LOG" 2>&1 &
echo \$! > "\$PIDFILE"
for _ in \$(seq 1 60); do
  alive && break
  kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null || { echo "Сервер не запустился. Журнал: \$LOG"; tail -n 20 "\$LOG"; exit 1; }
  sleep 0.25
done
alive || { echo "Сервер долго не отвечает. Журнал: \$LOG"; exit 1; }
echo "Оркестр работает: \$URL  (остановить: orkestr stop)"
open_ui
EOF
chmod +x "$BIN_DIR/orkestr"

# --- значок в меню приложений -------------------------------------------
mkdir -p "$APPS_DIR"
ICON="$DIR/web/assets/mark.svg"
[ -f "$ICON" ] || ICON="utilities-system-monitor"
cat > "$APPS_DIR/orkestr.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Оркестр
Comment=Пульт управления ИИ-агентами Claude Code и OpenCode
Exec=$BIN_DIR/orkestr
Icon=$ICON
Terminal=false
Categories=Development;Monitor;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

# --- итог ----------------------------------------------------------------
say "Готово. Версия: $("$DIR/.venv/bin/python" -c 'import orkestr; print(orkestr.__version__)')"
echo "  Запуск: orkestr        (или «Оркестр» в меню приложений)"
echo "  Демо:   orkestr --demo"
echo "  Стоп:   orkestr stop"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "Папки $BIN_DIR нет в PATH. Добавьте в ~/.bashrc строку: export PATH=\"\$HOME/.local/bin:\$PATH\"
  или запускайте полным путём: $BIN_DIR/orkestr" ;;
esac
