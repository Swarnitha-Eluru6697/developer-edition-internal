#!/usr/bin/env sh
# Developer Edition: make devedition/vendor/gateway-auth a git repo with the pinned tags.
#
# The Agents, Workspace, DataConnect and ML Studio Dockerfiles install the private gateway-auth
# library from this folder only when it contains a .git directory with the pinned tags
# (Python v1.1.0-rc.1, Node node-v1.1.0-rc.1); otherwise they fall back to GitHub and need a
# token. A nested .git is never committed to (or cloned from) the outer repo, and zips often
# drop it, so recreate it here from the shipped files. Safe to run again: existing tags are kept.
#
# Usage: devedition/prepare-vendor.sh [path/to/gateway-auth]
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=${1:-"$HERE/vendor/gateway-auth"}
TAGS="v1.1.0-rc.1 node-v1.1.0-rc.1"

[ -d "$DIR/python" ] && [ -d "$DIR/node" ] || { echo "gateway-auth sources missing at $DIR" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }

g() { git -C "$DIR" -c user.name="Developer Edition" -c user.email="devedition@localhost" \
        -c commit.gpgsign=false -c tag.gpgsign=false "$@"; }

if [ ! -d "$DIR/.git" ]; then
  g init -q
  g add -A
  g commit -q -m "gateway-auth (vendored)"
  echo "Created git repo in $DIR"
fi
for t in $TAGS; do
  g rev-parse -q --verify "refs/tags/$t" >/dev/null || g tag "$t"
done
echo "gateway-auth tags: $(g tag | tr '\n' ' ')"
