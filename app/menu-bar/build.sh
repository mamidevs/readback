#!/bin/sh
# Build a local, ad-hoc signed status item app using the macOS command line tools.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if [ "$#" -gt 1 ]; then
    echo "Usage: $0 [path/to/Readback.app]" >&2
    exit 2
fi
output=${1:-"$root/app/menu-bar/build/Readback.app"}
case "$output" in
    *.app) ;;
    *) echo "Choose an output path ending in .app." >&2; exit 2 ;;
esac
parent=$(dirname -- "$output")
mkdir -p "$parent"
if [ -e "$output" ] || [ -L "$output" ]; then
    echo "Output already exists. Choose a new path: $output" >&2
    exit 1
fi
stage=$(mktemp -d "$parent/.readback-build.XXXXXX")
trap 'rm -rf "$stage"' EXIT
app="$stage/Readback.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/python"
arch=$(uname -m)
xcrun swiftc -parse-as-library -swift-version 5 -O -target "$arch-apple-macos14.0" \
    -module-cache-path "$stage/modules" "$root"/app/menu-bar/*.swift -o "$app/Contents/MacOS/Readback"
cp "$root/app/menu-bar/Info.plist" "$app/Contents/"
for helper in readback_settings project_registry outside_judge; do
    cp "$root/scripts/$helper.py" "$app/Contents/Resources/python/"
done
cp "$root/app/menu-bar/menu_settings.py" "$root/app/menu-bar/menu_sessions.py" "$app/Contents/Resources/python/"
plutil -lint "$app/Contents/Info.plist" >/dev/null
codesign --force --sign - "$app"
codesign --verify --strict "$app"
# Publish only after compilation, signing and verification have all succeeded.
/usr/bin/python3 - "$app" "$output" <<'PY'
import ctypes
import os
import sys

libc = ctypes.CDLL(None, use_errno=True)
if libc.renamex_np(os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2]), 4):  # RENAME_EXCL
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code), sys.argv[2])
PY
printf '%s\n' "$output"
