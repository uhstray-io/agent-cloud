#!/usr/bin/env bash
# Build the Windows seed ISO that provision-template.yml attaches next to the installer.
# usage: build-seed-iso.sh <src-dir> <out-iso>
# <src-dir> holds Autounattend.xml, first-logon.ps1 and clone-unattend.xml; Windows Setup
# reads Autounattend.xml from the root of any attached drive. The volume is labeled WINSEED.
# Works on Linux (genisoimage, mkisofs or xorriso) and macOS (mkisofs or hdiutil).
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <src-dir> <out-iso>" >&2
  exit 2
fi
SRC="$1"
OUTPUT="$2"
if [[ ! -d "$SRC" ]]; then
  echo "ERROR: source directory not found: $SRC" >&2
  exit 1
fi

if command -v genisoimage &>/dev/null; then
  genisoimage -output "$OUTPUT" -volid WINSEED -joliet -rock "$SRC"
elif command -v mkisofs &>/dev/null; then
  mkisofs -output "$OUTPUT" -volid WINSEED -joliet -rock "$SRC"
elif command -v xorriso &>/dev/null; then
  xorriso -as mkisofs -output "$OUTPUT" -volid WINSEED -joliet -rock "$SRC"
elif command -v hdiutil &>/dev/null; then
  hdiutil makehybrid -o "$OUTPUT" -iso -joliet -default-volume-name WINSEED "$SRC"
  # hdiutil appends .iso if not present
  [[ -f "${OUTPUT}.iso" ]] && mv "${OUTPUT}.iso" "$OUTPUT"
else
  echo "ERROR: No ISO creation tool found." >&2
  echo "  Install one of:" >&2
  echo "    Linux:  apt install genisoimage (or xorriso)" >&2
  echo "    macOS:  brew install cdrtools (or hdiutil is built-in)" >&2
  exit 1
fi

echo "Seed ISO created: $OUTPUT"
