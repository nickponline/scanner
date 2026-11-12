#!/usr/bin/env bash
# Compiles the pure Swift helpers standalone on macOS, emits a sample capture package and
# decodes it in Python. Usage: bash modules/scanner-capture/tests/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="${TMPDIR:-/tmp}/scanner-capture-tests"
mkdir -p "$work"
swiftc -O "$here"/../ios/Core/*.swift "$here/main.swift" -o "$work/capture-tests"
"$work/capture-tests" "$work" | tee "$work/log.txt"
pkg="$(tail -1 "$work/log.txt")"
python3 -I "$here/verify.py" "$pkg"
if command -v uv >/dev/null 2>&1; then
  uv run --quiet --no-project --with google-crc32c python -I "$here/verify.py" "$pkg" | tail -2
fi
