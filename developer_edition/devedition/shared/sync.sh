#!/usr/bin/env sh
# Copies the shared Developer Edition account menu (with the shell theme sync) and the shared dark palette
# into every product frontend. Run after editing devedition/shared/DeveloperUserMenu.tsx or pralai-dark.css,
# then rebuild the *-web images.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SRC="$ROOT/devedition/shared/DeveloperUserMenu.tsx"
for dest in \
  Pralai_DataConnect_Frontend/src/components \
  Pralai_workspace_frontend/src/app \
  Pralai_Agent_frontend/src/components \
  Pralai_AIMLStudio_frontend/src/components
do
  cp "$SRC" "$ROOT/$dest/DeveloperUserMenu.tsx"
  cp "$ROOT/devedition/shared/pralai-dark.css" "$ROOT/$dest/pralai-dark.css"
  echo "→ $dest/DeveloperUserMenu.tsx, pralai-dark.css"
done
