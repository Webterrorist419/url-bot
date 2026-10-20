#!/usr/bin/env bash
# Ubuntu/Debian installer for the Linux binary or bot.py, and url-bot-admin.
set -Eeuo pipefail
umask 077

APP_DIR="/opt/url-bot-source"
DATA_DIR="/var/lib/url-bot-source"
BACKUP_DIR="/var/backups/url-bot-source"
CONFIG="$APP_DIR/config.env"
SERVICE="url-bot-source.service"
ADMIN_BIN="/usr/local/bin/url-bot-admin"
REPO_FILE="$DATA_DIR/repo-url"
REPO_URL="${BOT_REPO_URL:-}"
PYTHON="/usr/bin/python3"
LANGUAGE="ru"
SOURCE_DIR=""
BOT_MODE=""
TEMP_DIRS=()

trn() { if [[ "$LANGUAGE" == "ru" ]]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
pause() { read -r -p "$(trn 'Нажмите Enter для продолжения...' 'Press Enter to continue...')" _ || true; }
fail() { printf '%s\n' "$*" >&2; return 1; }
need_root() { [[ "$(id -u)" -eq 0 ]] || fail "$(trn 'Запустите скрипт через sudo.' 'Run this script with sudo.')"; }
cleanup() {
  local directory
  for directory in "${TEMP_DIRS[@]}"; do
    [[ "$directory" == /tmp/url-bot-source.* && -d "$directory" ]] && rm -rf -- "$directory"
  done
  return 0
}
trap cleanup EXIT

choose_language() {
  local choice
  echo "1) Русский / Russian"
  echo "2) English / Английский"
  read -r -p "Language [1/2]: " choice || choice=1
  [[ "$choice" == "2" ]] && LANGUAGE="en" || LANGUAGE="ru"
}

install_system_packages() {
  command -v apt-get >/dev/null || fail "$(trn 'Нужна Ubuntu или Debian с apt-get.' 'Ubuntu or Debian with apt-get is required.')"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 ca-certificates git
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || {
    fail "$(trn 'Установщику нужен Python 3.8 или новее.' 'The installer requires Python 3.8 or newer.')"
  }
}

has_bot() {
  [[ -f "$1/bot-linux-x86_64" || -f "$1/bot.py" ]]
}

ensure_directories() {
  [[ ! -L "$APP_DIR" && ! -L "$DATA_DIR" && ! -L "$BACKUP_DIR" ]] || {
    fail "$(trn 'Каталоги установки, данных и копий не должны быть символическими ссылками.' 'Application, data and backup directories must not be symbolic links.')"
  }
  getent passwd urlbotsource >/dev/null || useradd --system --user-group --home-dir "$DATA_DIR" --shell /usr/sbin/nologin urlbotsource
  install -d -o root -g root -m 755 "$APP_DIR"
  install -d -o urlbotsource -g urlbotsource -m 750 "$DATA_DIR"
  install -d -o root -g root -m 700 "$BACKUP_DIR"
}

resolve_repo() {
  local directory
  if [[ -z "$REPO_URL" && -f "$REPO_FILE" ]]; then IFS= read -r REPO_URL < "$REPO_FILE" || true; fi
  if [[ -z "$REPO_URL" ]]; then
    for directory in "$(dirname "$(realpath "$0")")" "$APP_DIR"; do
      if [[ -d "$directory/.git" ]]; then
        REPO_URL="$(git -C "$directory" remote get-url origin 2>/dev/null || true)"
        [[ -z "$REPO_URL" ]] || break
      fi
    done
  fi
}

download_source() {
  local temporary
  [[ -n "$REPO_URL" ]] || fail "$(trn 'Укажите BOT_REPO_URL с адресом вашего репозитория или настройте origin в локальном клоне.' 'Set BOT_REPO_URL to your repository URL or configure origin in a local clone.')"
  temporary="$(mktemp -d /tmp/url-bot-source.XXXXXX)"
  TEMP_DIRS+=("$temporary")
  # Avoid exposing credentials embedded in a repository URL in Git error output.
  if ! git clone --quiet --depth 1 -- "$REPO_URL" "$temporary/source" 2>"$temporary/git-error"; then
    fail "$(trn 'Не удалось скачать репозиторий. Проверьте BOT_REPO_URL и доступ к Git.' 'Could not download the repository. Check BOT_REPO_URL and Git access.')"
  fi
  SOURCE_DIR="$temporary/source"
}

validate_source() {
  [[ -f "$SOURCE_DIR/install.sh" ]] && has_bot "$SOURCE_DIR" || {
    fail "$(trn 'Рядом с install.sh нужен bot-linux-x86_64 или bot.py.' 'Place bot-linux-x86_64 or bot.py next to install.sh.')"
  }
  if [[ -f "$SOURCE_DIR/bot-linux-x86_64" ]]; then
    BOT_MODE="binary"
    [[ "$(uname -m)" == "x86_64" ]] || fail "$(trn 'Этот бинарник собран для Linux x86_64.' 'This binary requires Linux x86_64.')"
    "$PYTHON" - "$SOURCE_DIR/bot-linux-x86_64" <<'PY'
import sys
from pathlib import Path
with Path(sys.argv[1]).open("rb") as stream:
    header = stream.read(20)
if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
    raise SystemExit("Invalid Linux x86_64 executable")
PY
  else
    BOT_MODE="python"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
      fail "$(trn 'Для запуска bot.py нужен Python 3.10 или новее.' 'Running bot.py requires Python 3.10 or newer.')"
    }
    "$PYTHON" - "$SOURCE_DIR/bot.py" <<'PY'
import ast
import sys
from pathlib import Path
ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8-sig"), filename="bot.py")
PY
  fi
  bash -n "$SOURCE_DIR/install.sh"
}

prepare_config_and_database() {
  # Treat dotenv as data. SQLite's backup API also copies uncheckpointed WAL data.
  "$PYTHON" - "$CONFIG" "$SOURCE_DIR/config.env" "$DATA_DIR" "$BACKUP_DIR" <<'PY'
import os
import shlex
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

config, source_config, data, backups = map(Path, sys.argv[1:])
values = {}
input_config = config if config.is_file() else source_config
if input_config.is_file():
    for line in input_config.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value.strip(), comments=True)
        except ValueError:
            raise SystemExit("Invalid quotation in config.env") from None
        values[key.strip()] = " ".join(parts)

target = data / "bot.sqlite3"
previous = Path(values.get("DATABASE_PATH", "bot.sqlite3") or "bot.sqlite3")
if not previous.is_absolute():
    previous = input_config.parent / previous
if not previous.is_file():
    previous = target if target.is_file() else config.parent / "bot.sqlite3"

def copy_database(source, destination):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite integrity check failed")
            dst.execute("PRAGMA journal_mode=DELETE")
    os.chmod(destination, 0o600)

if previous.is_file() and previous.resolve() != target.resolve():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if target.is_file():
        copy_database(target, backups / f"before-migration-{stamp}.sqlite3")
    descriptor, temporary = tempfile.mkstemp(prefix=".migration-", dir=data)
    os.close(descriptor)
    try:
        copy_database(previous, temporary)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)

descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=config.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        for key, value in (
            ("BOT_TOKEN", values.get("BOT_TOKEN", "")),
            ("ADMIN_ID", values.get("ADMIN_ID", "")),
            ("DATABASE_PATH", str(target)),
        ):
            if any(character.isspace() for character in value):
                raise ValueError(f"Whitespace is not allowed in {key}")
            stream.write(f"{key}={value}\n")
    os.replace(temporary, config)
finally:
    Path(temporary).unlink(missing_ok=True)
PY
  chown root:urlbotsource "$CONFIG"
  chmod 640 "$CONFIG"
  if [[ -f "$DATA_DIR/bot.sqlite3" ]]; then
    chown urlbotsource:urlbotsource "$DATA_DIR/bot.sqlite3"
    chmod 600 "$DATA_DIR/bot.sqlite3"
  fi
}

copy_if_different() {
  local source="$1" destination="$2" mode="$3"
  if [[ "$(realpath "$source")" != "$(realpath -m "$destination")" ]]; then
    install -m "$mode" "$source" "$destination"
  else
    chmod "$mode" "$destination"
  fi
}

stop_if_present() {
  # A missing unit is normal on first install; an actual stop failure must abort.
  if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    systemctl stop "$SERVICE"
  fi
}

write_service() {
  local exec_start
  if [[ "$BOT_MODE" == "binary" ]]; then
    exec_start="$APP_DIR/bot-linux-x86_64"
  else
    exec_start="/usr/bin/python3 -B $APP_DIR/bot.py"
  fi
  cat > "/etc/systemd/system/$SERVICE" <<EOF
[Unit]
Description=URL Bot (source installation)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=urlbotsource
Group=urlbotsource
WorkingDirectory=$APP_DIR
EnvironmentFile=$CONFIG
ExecStart=$exec_start
Restart=on-failure
RestartSec=5
TimeoutStopSec=40
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null
}

deploy_source() {
  validate_source
  ensure_directories
  stop_if_present
  prepare_config_and_database
  if [[ "$BOT_MODE" == "binary" ]]; then
    copy_if_different "$SOURCE_DIR/bot-linux-x86_64" "$APP_DIR/bot-linux-x86_64" 755
    rm -f -- "$APP_DIR/bot.py"
  else
    copy_if_different "$SOURCE_DIR/bot.py" "$APP_DIR/bot.py" 644
    rm -f -- "$APP_DIR/bot-linux-x86_64"
  fi
  copy_if_different "$SOURCE_DIR/install.sh" "$APP_DIR/install.sh" 755
  copy_if_different "$APP_DIR/install.sh" "$ADMIN_BIN" 755
  printf '%s\n' "$REPO_URL" > "$REPO_FILE"
  chmod 600 "$REPO_FILE"
  write_service
  # Delete only known Rust artifacts within the fixed deployment directory.
  [[ "$(realpath "$APP_DIR")" == "/opt/url-bot-source" ]] || fail "Unexpected application path"
  rm -f -- "$APP_DIR/bot.rs" "$APP_DIR/Cargo.toml" "$APP_DIR/Cargo.lock" \
    "$APP_DIR/README.md" "$APP_DIR/config.env.example" "$APP_DIR/.gitignore" "$DATA_DIR/build-dir"
  rm -rf -- "$APP_DIR/target" "$APP_DIR/__pycache__"
}

credentials_ready() {
  "$PYTHON" - "$CONFIG" <<'PY'
import re
import sys
from pathlib import Path
values = dict(line.split("=", 1) for line in Path(sys.argv[1]).read_text().splitlines() if "=" in line)
valid = re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", values.get("BOT_TOKEN", "")) and re.fullmatch(r"[1-9][0-9]*", values.get("ADMIN_ID", ""))
sys.exit(0 if valid else 1)
PY
}

restart_bot() {
  need_root
  systemctl restart "$SERVICE"
  systemctl --no-pager --full status "$SERVICE" || true
}

configure_credentials() {
  need_root
  has_bot "$APP_DIR" || fail "$(trn 'Сначала установите бота.' 'Install the bot first.')"
  local token admin temporary
  read -r -p "$(trn 'Токен Telegram бота (ввод отображается): ' 'Telegram bot token (input is visible): ')" token || return 1
  read -r -p "$(trn 'Telegram ID администратора: ' 'Administrator Telegram ID: ')" admin || return 1
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ && "$admin" =~ ^[1-9][0-9]*$ ]] || {
    fail "$(trn 'Нужны корректный токен и положительный числовой Telegram ID.' 'A valid token and positive numeric Telegram ID are required.')"
  }
  temporary="$(mktemp "$APP_DIR/.config-XXXXXX")"
  printf 'BOT_TOKEN=%s\nADMIN_ID=%s\nDATABASE_PATH=%s/bot.sqlite3\n' "$token" "$admin" "$DATA_DIR" > "$temporary"
  unset token
  chown root:urlbotsource "$temporary"
  chmod 640 "$temporary"
  mv -f -- "$temporary" "$CONFIG"
  restart_bot
}

clear_credentials() {
  need_root
  has_bot "$APP_DIR" || fail "$(trn 'Бот не установлен.' 'Bot is not installed.')"
  stop_if_present
  printf 'BOT_TOKEN=\nADMIN_ID=\nDATABASE_PATH=%s/bot.sqlite3\n' "$DATA_DIR" > "$CONFIG"
  chown root:urlbotsource "$CONFIG"
  chmod 640 "$CONFIG"
  echo "$(trn 'Токен и ID очищены. Сервис остановлен до ввода новых данных.' 'Token and ID cleared. Service is stopped until new credentials are entered.')"
}

install_bot() {
  need_root
  install_system_packages
  resolve_repo
  SOURCE_DIR="$(dirname "$(realpath "$0")")"
  if ! has_bot "$SOURCE_DIR"; then
    if has_bot "$APP_DIR"; then SOURCE_DIR="$APP_DIR"; else download_source; fi
  fi
  deploy_source
  echo "$(trn 'Бот установлен. Введите токен и Telegram ID администратора для запуска.' 'The bot is installed. Enter the token and administrator Telegram ID to start it.')"
  configure_credentials
  echo "$(trn 'Управление ботом: sudo url-bot-admin' 'Manage the bot with: sudo url-bot-admin')"
}

update_bot() {
  need_root
  has_bot "$APP_DIR" || [[ -f "$APP_DIR/bot.rs" ]] || fail "$(trn 'Сначала установите бота.' 'Install the bot first.')"
  install_system_packages
  resolve_repo
  download_source
  deploy_source
  if credentials_ready; then
    restart_bot
  else
    echo "$(trn 'Обновлено. Введите токен и ID в пункте 2 для запуска.' 'Updated. Enter the token and ID using option 2 to start the bot.')"
  fi
}

backup_db() {
  need_root
  [[ -f "$DATA_DIR/bot.sqlite3" ]] || { echo "$(trn 'База данных ещё не создана.' 'Database has not been created yet.')"; return; }
  install -d -o root -g root -m 700 "$BACKUP_DIR"
  local file
  file="$(mktemp --suffix=.sqlite3 "$BACKUP_DIR/bot-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
  "$PYTHON" - "$DATA_DIR/bot.sqlite3" "$file" <<'PY'
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect(sys.argv[2])) as destination:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE")
os.chmod(sys.argv[2], 0o600)
PY
  echo "$(trn 'Резервная копия создана: ' 'Backup created: ')$file"
}

import_db() {
  need_root
  has_bot "$APP_DIR" || fail "$(trn 'Сначала установите бота.' 'Install the bot first.')"
  local file staged
  read -r -p "$(trn 'Полный путь к файлу SQLite для импорта: ' 'Full path to the SQLite file to import: ')" file || return 1
  [[ -f "$file" ]] || fail "$(trn 'Файл не найден.' 'File not found.')"
  [[ "$(realpath "$file")" != "$(realpath -m "$DATA_DIR/bot.sqlite3")" ]] || {
    fail "$(trn 'Выбран текущий файл базы.' 'The current database file was selected.')"
  }
  staged="$(mktemp "$DATA_DIR/.import-XXXXXX")"
  if ! "$PYTHON" - "$file" "$staged" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
try:
    with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("SQLite integrity check failed")
        required = {
            "users": {"telegram_id", "lang", "started_at"},
            "settings": {"key", "value"},
        }
        for table, columns in required.items():
            actual = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise ValueError(f"Incompatible database table: {table}")
        with closing(sqlite3.connect(sys.argv[2])) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
except (sqlite3.Error, ValueError) as error:
    print(error, file=sys.stderr)
    sys.exit(1)
PY
  then
    rm -f -- "$staged"
    fail "$(trn 'База не прошла проверку; текущие данные не изменены.' 'Database validation failed; current data has not been changed.')"
  fi
  stop_if_present
  backup_db
  # WAL/SHM sidecars from the old database cannot be used with its replacement.
  rm -f -- "$DATA_DIR/bot.sqlite3-wal" "$DATA_DIR/bot.sqlite3-shm"
  chown urlbotsource:urlbotsource "$staged"
  chmod 600 "$staged"
  mv -f -- "$staged" "$DATA_DIR/bot.sqlite3"
  if credentials_ready; then restart_bot; fi
  echo "$(trn 'База данных импортирована.' 'Database imported.')"
}

uninstall_bot() {
  need_root
  local confirm
  echo "$(trn 'Это удалит сервис, установленный бот, базу данных, настройки и все резервные копии.' 'This removes the service, installed bot, database, settings and all backups.')"
  read -r -p "$(trn 'Для подтверждения введите DELETE: ' 'Type DELETE to confirm: ')" confirm || return
  [[ "$confirm" == "DELETE" ]] || { echo "$(trn 'Удаление отменено.' 'Uninstall cancelled.')"; return; }
  [[ ! -L "$APP_DIR" && ! -L "$DATA_DIR" && ! -L "$BACKUP_DIR" ]] || fail "Refusing to remove symbolic-link installation directories"
  stop_if_present
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f -- "/etc/systemd/system/$SERVICE" "$ADMIN_BIN"
  systemctl daemon-reload
  # Recursive deletion targets are fixed absolute deployment paths.
  rm -rf -- /opt/url-bot-source /var/backups/url-bot-source /var/lib/url-bot-source
  userdel urlbotsource 2>/dev/null || true
  echo "$(trn 'Бот полностью удалён.' 'Bot fully removed.')"
}

menu() {
  local action
  while true; do
    echo
    echo "=== url-bot-admin ==="
    echo "1) $(trn 'Установить / переустановить бота' 'Install / reinstall bot')"
    echo "2) $(trn 'Настроить токен и ID (ввод токена отображается)' 'Set token and ID (token input is visible)')"
    echo "3) $(trn 'Очистить токен и ID' 'Clear token and ID')"
    echo "4) $(trn 'Обновить бота' 'Update bot')"
    echo "5) $(trn 'Экспорт базы данных / бэкап' 'Export database / backup')"
    echo "6) $(trn 'Импорт базы данных' 'Import database')"
    echo "7) $(trn 'Удалить бота начисто' 'Completely uninstall bot')"
    echo "0) $(trn 'Выход' 'Exit')"
    read -r -p "$(trn 'Выберите пункт: ' 'Choose an option: ')" action || return
    case "$action" in
      1) install_bot; pause;;
      2) configure_credentials; pause;;
      3) clear_credentials; pause;;
      4) update_bot; pause;;
      5) backup_db; pause;;
      6) import_db; pause;;
      7) uninstall_bot; return;;
      0) return;;
      *) echo "$(trn 'Неверный пункт.' 'Invalid option.')";;
    esac
  done
}

main() {
  case "${1:-}" in
    --help|-h)
      echo "sudo bash install.sh [--install|--update|--menu]"
      echo "BOT_REPO_URL overrides the saved/default Git repository."
      return;;
    ""|--install|--update|--menu) ;;
    *) fail "Unknown option. Use --help.";;
  esac
  need_root
  choose_language
  case "${1:-}" in
    --install) install_bot;;
    --update) update_bot;;
    --menu) menu;;
    *)
      local self
      self="$(realpath "$0")"
      if [[ "$self" == "$(realpath -m "$ADMIN_BIN")" || "$self" == "$(realpath -m "$APP_DIR/install.sh")" ]]; then
        menu
      else
        install_bot
      fi;;
  esac
}

main "$@"
