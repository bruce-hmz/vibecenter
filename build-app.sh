#!/bin/zsh
# build-app.sh — compile + assemble a .app bundle for Vibe Island.
#
# Usage:
#   ./build-app.sh           # build to ./build/VibeIsland.app
#   ./build-app.sh --install  # build + copy to /Applications

set -euo pipefail

APP_NAME="VibeIsland"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SRC_DIR/build"
APP_BUNDLE="$BUILD_DIR/${APP_NAME}.app"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

echo "=== 1. 编译 Swift ==="
swiftc -O -parse-as-library \
  -framework SwiftUI -framework AppKit -framework Network -framework Combine \
  -framework CoreText -framework CoreFoundation \
  "$SRC_DIR/VibeIsland.swift" \
  -o "$SRC_DIR/vibe-island-new" 2>&1 | grep -E "error:" || true
[ -x "$SRC_DIR/vibe-island-new" ] || { red "✗ 编译失败"; exit 1; }
green "✓ 编译成功"

echo ""
echo "=== 2. 组装 .app bundle ==="
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/logos"
mkdir -p "$APP_BUNDLE/Contents/Resources/Fonts"

# 二进制
cp "$SRC_DIR/vibe-island-new" "$APP_BUNDLE/Contents/MacOS/vibe-island"
chmod +x "$APP_BUNDLE/Contents/MacOS/vibe-island"

# Info.plist
cp "$SRC_DIR/Info.plist" "$APP_BUNDLE/Contents/Info.plist"

# 字体
cp "$SRC_DIR/DepartureMono-Regular.otf" "$APP_BUNDLE/Contents/Resources/Fonts/"

# Provider logos
cp "$SRC_DIR/logos/"*.png "$APP_BUNDLE/Contents/Resources/logos/" 2>/dev/null

# scan-agents.sh（运行时需要）
cp "$SRC_DIR/scan-agents.sh" "$APP_BUNDLE/Contents/Resources/scan-agents.sh"
chmod +x "$APP_BUNDLE/Contents/Resources/scan-agents.sh"

green "✓ bundle 组装完成: $APP_BUNDLE"

echo ""
echo "=== 3. 验证 bundle 结构 ==="
find "$APP_BUNDLE" -type f | sed "s|$APP_BUNDLE/||" | sort

if [ "${1:-}" = "--install" ]; then
  echo ""
  echo "=== 4. 安装到 /Applications ==="
  rm -rf "/Applications/${APP_NAME}.app"
  cp -R "$APP_BUNDLE" "/Applications/"
  green "✓ 已安装到 /Applications/${APP_NAME}.app"
  echo ""
  bold "启动: open -a '${APP_NAME}'"
else
  echo ""
  bold "完成。启动方式:"
  echo "  open '$APP_BUNDLE'"
  echo "  或: ./build-app.sh --install  → 安装到 /Applications"
fi
