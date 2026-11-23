#!/usr/bin/env sh
# Developer Edition: writes this installation's identity into .env (run once by install.sh;
# safe to run again: existing values are kept).
#
#   DE_INSTALL_ID        UUID for this installation (Plan of Execution: download_events.install_id). Derived from
#                        the machine fingerprint, so the Data and the Agent & Model editions on one machine are
#                        one installation for the Registration service (one active install per account); random
#                        when the machine ID cannot be read.
#   DE_FINGERPRINT_HASH  SHA-256 of hostname + machine ID. Only the hash leaves the machine; the
#                        license token is bound to it, so a copied install folder fails check-in.
#   DE_OS                OS and CPU, e.g. "macOS arm64", reported at sign-in and check-in
#
# Usage: devedition/install-identity.sh [path/to/.env]
set -eu
ENV_FILE=${1:-"$(cd "$(dirname "$0")/.." && pwd)/.env"}
[ -f "$ENV_FILE" ] || { echo "No .env at $ENV_FILE" >&2; exit 1; }

has() { grep -q "^$1=." "$ENV_FILE"; }
put() { has "$1" || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }

uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z'
  elif [ -r /proc/sys/kernel/random/uuid ]; then cat /proc/sys/kernel/random/uuid
  else python3 -c 'import uuid; print(uuid.uuid4())'; fi
}

machine_id() {
  case "$(uname -s)" in
    Darwin) ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/ {print $4}' ;;
    Linux)  cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null || true ;;
    *)      true ;;
  esac
}

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 | cut -d' ' -f1
  else sha256sum | cut -d' ' -f1; fi
}

os_name() {
  case "$(uname -s)" in Darwin) o=macOS ;; Linux) o=Linux ;; *) o=$(uname -s) ;; esac
  case "$(uname -m)" in x86_64|amd64) a=amd64 ;; arm64|aarch64) a=arm64 ;; *) a=$(uname -m) ;; esac
  echo "$o $a"
}

[ -n "$(tail -c1 "$ENV_FILE")" ] && echo >> "$ENV_FILE"
if ! has DE_INSTALL_ID; then
  printf '\n# This installation (devedition/install-identity.sh). Do not copy to another machine.\n' >> "$ENV_FILE"
fi
FP=$(printf '%s:%s' "$(hostname)" "$(machine_id)" | sha256)
install_uuid() {
  if [ -n "$(machine_id)" ]; then
    printf 'inaiera-developer-edition:%s' "$FP" | sha256 \
      | sed -E 's/^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*/\1-\2-5\3-8\4-\5/'
  else
    uuid
  fi
}
put DE_INSTALL_ID "$(install_uuid)"
put DE_FINGERPRINT_HASH "$FP"
put DE_OS "$(os_name)"
echo "Install identity is in $ENV_FILE"
