#!/bin/bash
# Tạo dist/iterm-xshell-<version>.dmg: cửa sổ chứa thư mục cài + "Cài đặt.command" (bấm đúp để cài).
# Không phải .app đã ký — cài bằng install.sh; lần đầu mở có thể cần chuột phải > Open.
set -euo pipefail
cd "$(dirname "$0")"
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' xshell_panels/__init__.py)"
NAME="iterm-xshell-$VER"
./package.sh >/dev/null
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/vol"
tar -xzf "dist/$NAME.tar.gz" -C "$STAGE/vol"
mv "$STAGE/vol/$NAME" "$STAGE/vol/iterm-xshell"
cat > "$STAGE/vol/Cài đặt.command" <<'CMD'
#!/bin/bash
cd "$(dirname "$0")/iterm-xshell" && ./install.sh
echo; echo "Xong. Đóng cửa sổ này."; read -r -n1
CMD
chmod +x "$STAGE/vol/Cài đặt.command" "$STAGE/vol/iterm-xshell/install.sh"
OUT="dist/$NAME.dmg"
rm -f "$OUT"
hdiutil create -quiet -volname "iterm-xshell $VER" -srcfolder "$STAGE/vol" -fs HFS+ -format UDZO "$OUT"
echo "Đã tạo $OUT"
