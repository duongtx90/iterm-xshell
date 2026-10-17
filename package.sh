#!/bin/bash
# Đóng gói để chia sẻ: tạo dist/iterm-xshell-<version>.tar.gz (chỉ gồm file cần để cài).
set -euo pipefail
cd "$(dirname "$0")"
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' xshell_panels/__init__.py)"
NAME="iterm-xshell-$VER"
OUT="dist/$NAME.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME" dist
cp install.sh XshellPanels.py requirements.txt README.md "$STAGE/$NAME/"
find xshell_panels -type f \( -name '*.py' -o -name '*.html' \) -print0 |
  while IFS= read -r -d '' f; do mkdir -p "$STAGE/$NAME/$(dirname "$f")"; cp "$f" "$STAGE/$NAME/$f"; done
COPYFILE_DISABLE=1 tar -czf "$OUT" -C "$STAGE" "$NAME"
echo "Đã tạo $OUT"
