#!/usr/bin/env bash
# Build the pathscope onedir bundle with PyInstaller and zip it for
# release (macOS/Linux). Run from anywhere; this script resolves the
# repo root itself.
#
# Output: dist/pathscope/ (the onedir bundle, executable at
# dist/pathscope/pathscope) with targets/ copied in beside it - never
# inside the PyInstaller-collected tree, per spec section 3 - plus
# dist/pathscope-<version>-<os>.zip containing both.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VENV_BIN=".venv/bin"
if [ ! -x "$VENV_BIN/python" ]; then
    echo "error: $VENV_BIN/python not found - create .venv first" >&2
    exit 1
fi

echo "== installing build extra =="
"$VENV_BIN/pip" install -e ".[ui,build]"

echo "== cleaning previous build output =="
rm -rf build dist

echo "== running pyinstaller =="
"$VENV_BIN/pyinstaller" --noconfirm --clean packaging/pathscope.spec

echo "== copying targets/ next to the executable =="
cp -R targets dist/pathscope/targets

echo "== zipping release archive =="
VERSION="$("$VENV_BIN/python" -c '
import re
text = open("pyproject.toml").read()
print(re.search(r"version = \"([^\"]+)\"", text).group(1))
')"
case "$(uname -s)" in
    Darwin) OS_TAG="macos" ;;
    Linux) OS_TAG="linux" ;;
    *) OS_TAG="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
ZIP_NAME="pathscope-${VERSION}-${OS_TAG}.zip"

( cd dist && rm -f "$ZIP_NAME" && zip -r -q "$ZIP_NAME" pathscope )
echo "== built dist/$ZIP_NAME =="
