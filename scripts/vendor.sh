#!/usr/bin/env bash
# Download a browser library into static/vendor/ so it can be served from this
# app instead of a CDN. See static/vendor/README.md for why.
#
# Usage: ./scripts/vendor.sh <url> [filename]
set -euo pipefail
cd "$( dirname "${BASH_SOURCE[0]}" )/.."

if [ $# -lt 1 ]; then
  echo "Usage: $0 <url> [filename]" >&2
  exit 1
fi

URL="$1"
FILENAME="${2:-$( basename "${URL%%\?*}" )}"
DESTINATION="static/vendor/${FILENAME}"

if [ -e "$DESTINATION" ]; then
  echo "Refusing to overwrite existing $DESTINATION" >&2
  echo "Delete it first if you really mean to replace it." >&2
  exit 1
fi

mkdir -p static/vendor
echo "Downloading $URL"
curl -fsSL --proto '=https' --tlsv1.2 -o "$DESTINATION" "$URL"

echo "Saved $DESTINATION ($( wc -c < "$DESTINATION" | tr -d ' ' ) bytes)"
echo "sha256: $( shasum -a 256 "$DESTINATION" | cut -d' ' -f1 )"
echo ""
echo "Reference it from your HTML with:"
case "$FILENAME" in
  *.css) echo "  <link rel=\"stylesheet\" href=\"/vendor/${FILENAME}\" />" ;;
  *)     echo "  <script src=\"/vendor/${FILENAME}\"></script>" ;;
esac
echo ""
echo "Commit the file -- it is part of the app, not a build artifact."
