#!/bin/zsh
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
build_dir="$repo_root/build/tests"
module_cache_dir="$build_dir/ModuleCache"
tmp_root="/tmp/vibe-island-swift-tests"
binary_path="$build_dir/ViewModelTests"

mkdir -p "$build_dir" "$module_cache_dir" "$tmp_root"

export TMPDIR="$tmp_root/"

swiftc \
  -D VIBE_ISLAND_UNIT_TESTS \
  -module-cache-path "$module_cache_dir" \
  -o "$binary_path" \
  "$repo_root/VibeIsland.swift" \
  "$repo_root/tests/ViewModelTests.swift"

"$binary_path"
