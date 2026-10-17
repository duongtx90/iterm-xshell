#!/bin/bash
# Cài iterm-xshell (panel kênh SSH + panel duyệt file SFTP) cho iTerm2.
#
#   ./install.sh                 cài/cập nhật rồi khởi động ngay
#   ./install.sh --python PATH   chọn Python (>= 3.9) để tạo venv
#   ./install.sh --no-start      chỉ cài, không khởi động
#   ./install.sh --dev           cài thêm pytest để chạy test
#   ./install.sh --uninstall     gỡ (giữ dữ liệu ~/.config/iterm-xshell)
#   ./install.sh --purge         gỡ và xoá cả dữ liệu
#
# Bố cục sau khi cài:
#   ~/Library/Application Support/iterm-xshell/app/        mã nguồn
#   ~/Library/Application Support/iterm-xshell/venv/       Python venv + phụ thuộc
#   ~/Library/Application Support/iterm-xshell/run.sh      khởi chạy daemon (thay instance cũ)
#   ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/XshellPanels.scpt
#       iTerm2 chạy file này khi khởi động: xin cookie Python API rồi gọi run.sh.
#
# Vì sao không dùng script Python "full environment" của iTerm2: iTerm2 3.7 đòi venv do chính
# nó tạo (iterm2env-metadata.json, runtime tải về); venv tự dựng bị chặn bằng hộp thoại
# "Upgrade Python Runtime?". Một .scpt nhỏ gọi venv riêng thì chạy được trên máy sạch.
set -eo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/iterm-xshell"
ITERM_SCRIPTS="$HOME/Library/Application Support/iTerm2/Scripts"
AUTOLAUNCH="$ITERM_SCRIPTS/AutoLaunch"
SCPT="$AUTOLAUNCH/XshellPanels.scpt"
TOGGLE_SCPT="$ITERM_SCRIPTS/Toggle XShell.scpt"   # hiện ở menu Scripts
DATA_DIR="$HOME/.config/iterm-xshell"
PID_FILE="$DATA_DIR/daemon.pid"

PYTHON="" START=1 DEV=0 MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift ;;
    --no-start) START=0 ;;
    --dev) DEV=1 ;;
    --uninstall) MODE=uninstall ;;
    --purge) MODE=purge ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || die "Chỉ hỗ trợ macOS."

# Trên Apple Silicon, Python universal2 bị gọi từ process x86 (vd. coreutils Homebrew cũ)
# sẽ chạy dưới Rosetta và không load được wheel arm64 -> luôn ép arm64.
ARCH=()
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then ARCH=(/usr/bin/arch -arm64); fi

stop_daemon() {
  if [ -f "$PID_FILE" ]; then
    local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q XshellPanels.py; then
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    fi
    rm -f "$PID_FILE"
  fi
}

if [ "$MODE" != install ]; then
  say "Dừng daemon"
  stop_daemon
  rm -f "$SCPT" "$TOGGLE_SCPT"
  rm -rf "$APP_DIR"
  if [ "$MODE" = purge ]; then
    rm -rf "$DATA_DIR"
    say "Đã xoá dữ liệu $DATA_DIR (mật khẩu trong Keychain, nếu có, nằm ở service 'iterm-xshell')"
  else
    say "Giữ lại danh sách kênh ở $DATA_DIR"
  fi
  say "Đã gỡ. Khởi động lại iTerm2 để tool biến khỏi Toolbelt."
  exit 0
fi

# ------------------------------------------------------------ chọn Python
py_ok() { "${ARCH[@]}" "$1" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }
if [ -n "$PYTHON" ]; then
  py_ok "$PYTHON" || die "$PYTHON không dùng được (cần Python >= 3.9 có module venv)."
else
  CANDIDATES=()
  for v in 3.14 3.13 3.12 3.11 3.10; do
    CANDIDATES+=("/opt/homebrew/bin/python$v" "/usr/local/bin/python$v"
                 "/Library/Frameworks/Python.framework/Versions/$v/bin/python3")
  done
  # Runtime Python mà iTerm2 tải về (nếu đã có)
  for p in "$HOME/Library/Application Support/iTerm2"/iterm2env*/versions/*/bin/python3; do
    [ -x "$p" ] && CANDIDATES+=("$p")
  done
  CANDIDATES+=(/opt/homebrew/bin/python3 /usr/local/bin/python3)
  # /usr/bin/python3 là stub của Command Line Tools: chỉ dùng khi CLT đã cài (tránh bật hộp thoại cài).
  if xcode-select -p >/dev/null 2>&1; then CANDIDATES+=(/usr/bin/python3); fi
  for p in "${CANDIDATES[@]}"; do
    if [ -x "$p" ] && py_ok "$p"; then PYTHON="$p"; break; fi
  done
  [ -n "$PYTHON" ] || die "Không tìm thấy Python >= 3.9. Cài bằng 'xcode-select --install', Homebrew ('brew install python') hoặc python.org, rồi chạy lại (hoặc --python PATH)."
fi
PYVER="$("${ARCH[@]}" "$PYTHON" -c 'import platform; print(platform.python_version())')"
say "Dùng Python $PYVER: $PYTHON"

# ------------------------------------------------------------------ venv
VENV="$APP_DIR/venv"
mkdir -p "$APP_DIR"
if [ -x "$VENV/bin/python" ]; then
  CUR="$("${ARCH[@]}" "$VENV/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo none)"
  if [ "$CUR" != "$PYVER" ]; then
    say "Tạo lại venv (đang là Python $CUR)"
    rm -rf "$VENV"
  fi
fi
if [ ! -x "$VENV/bin/python" ]; then
  say "Tạo venv ở $VENV"
  "${ARCH[@]}" "$PYTHON" -m venv "$VENV"
fi
say "Cài phụ thuộc (iterm2, aiohttp, asyncssh, pyobjc)…"
"${ARCH[@]}" "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
REQ="$SRC/requirements.txt"
[ "$DEV" = 1 ] && REQ="$SRC/requirements-dev.txt"
"${ARCH[@]}" "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REQ"
"${ARCH[@]}" "$VENV/bin/python" -c 'import iterm2, aiohttp, asyncssh, AppKit, WebKit, Quartz' \
  || die "Phụ thuộc chưa import được, xem lỗi ở trên."

# ------------------------------------------------------------- mã nguồn
say "Copy mã nguồn vào $APP_DIR/app"
rm -rf "$APP_DIR/app.new"
mkdir -p "$APP_DIR/app.new"
cp "$SRC/XshellPanels.py" "$SRC/requirements.txt" "$APP_DIR/app.new/"
(cd "$SRC" && find xshell_panels -type f \( -name '*.py' -o -name '*.html' \) -print0 \
  | while IFS= read -r -d '' f; do mkdir -p "$APP_DIR/app.new/$(dirname "$f")"; cp "$f" "$APP_DIR/app.new/$f"; done)
rm -rf "$APP_DIR/app"
mv "$APP_DIR/app.new" "$APP_DIR/app"

# ---------------------------------------------------------------- run.sh
cat > "$APP_DIR/run.sh" <<'RUN'
#!/bin/bash
# Khởi chạy daemon iterm-xshell (thay instance cũ). Được gọi bởi AutoLaunch/XshellPanels.scpt.
APP_DIR="$HOME/Library/Application Support/iterm-xshell"
PID_FILE="$HOME/.config/iterm-xshell/daemon.pid"
LOG="$HOME/Library/Logs/iterm-xshell.out"
mkdir -p "$HOME/Library/Logs" "$HOME/.config/iterm-xshell"
if [ -f "$PID_FILE" ]; then
  old="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null && ps -p "$old" -o command= | grep -q XshellPanels.py; then
    kill "$old" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$old" 2>/dev/null || break; sleep 0.2; done
  fi
fi
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
[ -n "$SSH_AUTH_SOCK" ] || export SSH_AUTH_SOCK="$(launchctl getenv SSH_AUTH_SOCK 2>/dev/null)"
ARCH=()
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then ARCH=(/usr/bin/arch -arm64); fi
cd "$APP_DIR/app" || exit 1
echo "---- $(date '+%F %T') start" >> "$LOG"
nohup "${ARCH[@]}" "$APP_DIR/venv/bin/python" "$APP_DIR/app/XshellPanels.py" >> "$LOG" 2>&1 < /dev/null &
RUN
chmod 755 "$APP_DIR/run.sh"

# ------------------------------------------------------- AutoLaunch .scpt
say "Cài AutoLaunch: $SCPT"
mkdir -p "$AUTOLAUNCH"
TMP_AS="$(mktemp -t xshell).applescript"
cat > "$TMP_AS" <<AS
-- iterm-xshell: khởi chạy daemon panel kênh SSH + Xftp (xem install.sh)
set ck to ""
try
	tell application id "com.googlecode.iterm2" to set ck to request cookie and key for app named "XshellPanels"
end try
set envs to ""
if ck is not "" then
	set AppleScript's text item delimiters to " "
	set envs to "ITERM2_COOKIE=" & quoted form of (text item 1 of ck) & " ITERM2_KEY=" & quoted form of (text item 2 of ck) & " "
	set AppleScript's text item delimiters to ""
end if
do shell script envs & quoted form of "$APP_DIR/run.sh" & " >/dev/null 2>&1 &"
AS
osacompile -o "$SCPT" "$TMP_AS"
rm -f "$TMP_AS"

say "Cài mục menu Scripts > Toggle XShell"
TMP_AS="$(mktemp -t xshelltoggle).applescript"
cat > "$TMP_AS" <<AS
-- iterm-xshell: bật/tắt Toolbelt (chứa panel Xshell)
set ck to request_cookie()
set AppleScript's text item delimiters to " "
set envs to "ITERM2_COOKIE=" & quoted form of (text item 1 of ck) & " ITERM2_KEY=" & quoted form of (text item 2 of ck) & " "
set AppleScript's text item delimiters to ""
do shell script "cd " & quoted form of "$APP_DIR/app" & " && " & envs & "/usr/bin/arch -arm64 " & quoted form of "$APP_DIR/venv/bin/python" & " -m xshell_panels.toggle_toolbelt >/dev/null 2>&1 || true"
on request_cookie()
	tell application id "com.googlecode.iterm2" to return request cookie and key for app named "XshellToggle"
end request_cookie
AS
osacompile -o "$TOGGLE_SCPT" "$TMP_AS"
rm -f "$TMP_AS"

if [ -f "$AUTOLAUNCH/XftpPanel.py" ]; then
  warn "Còn bản cũ $AUTOLAUNCH/XftpPanel.py — nên xoá để tránh hai panel Xftp."
fi
if [ "$(defaults read com.googlecode.iterm2 EnableAPIServer 2>/dev/null)" != 1 ]; then
  warn "Python API của iTerm2 đang tắt: bật ở iTerm2 > Settings > General > Magic > Enable Python API."
fi

if [ "$START" = 1 ] && [ "$(osascript -e 'application id "com.googlecode.iterm2" is running' 2>/dev/null)" = true ]; then
  say "Khởi động daemon"
  osascript "$SCPT" || warn "Không chạy được $SCPT — hãy khởi động lại iTerm2."
  sleep 2
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    say "Đang chạy (pid $(cat "$PID_FILE")). Log: ~/Library/Logs/iterm-xshell.log"
  else
    warn "Daemon chưa chạy, xem ~/Library/Logs/iterm-xshell.out"
  fi
fi
say "Xong. Panel kênh SSH là cửa sổ riêng bám bên trái cửa sổ iTerm2."
say "Panel file: View > Toolbelt > Show Toolbelt, rồi tick 'Xftp' trong View > Toolbelt."
