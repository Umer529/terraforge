#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$ROOT/TerraForge.xcodeproj"
SCHEME="TerraForge"
ARCHIVE="$ROOT/build/TerraForge.xcarchive"
TEAM_ID="${1:-${APPLE_TEAM_ID:-}}"

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "Xcode command-line tools are required. Install Xcode, open it once, then rerun this script." >&2
  exit 1
fi

if [[ -z "$TEAM_ID" ]]; then
  echo "Usage: ./build-iphone.sh APPLE_TEAM_ID" >&2
  echo "The Apple Developer team must already be signed in to Xcode on this Mac." >&2
  exit 2
fi

mkdir -p "$ROOT/build"
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE" \
  -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CODE_SIGN_STYLE=Automatic \
  archive

echo
echo "Signed archive created at: $ARCHIVE"
echo "Use Xcode Organizer to export/upload this archive for TestFlight or registered-device installation."
