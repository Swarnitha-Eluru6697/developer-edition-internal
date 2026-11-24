#!/usr/bin/env sh
# Developer Edition: create .env with no questions asked.
#
# Copies .env.example to .env (if .env does not exist yet) and replaces every <generate> with a
# fresh, unique random secret in the format that setting needs, then writes this machine's
# install identity (devedition/install-identity.sh). Values already set are never changed, so
# re-running it on an existing install only fills in what is missing.
#
#   devedition/init-env.sh            # then: docker compose up -d --build
#
# The edition (Data, Agent & Model, or both) is not set here: the user picks it when creating their account.
#
# Usage: devedition/init-env.sh [path/to/.env]   (the .env.example next to it is the template)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${1:-"$HERE/../.env"}
EXAMPLE="$(dirname "$ENV_FILE")/.env.example"
[ -f "$EXAMPLE" ] || EXAMPLE="$HERE/../.env.example"

command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

if [ ! -f "$ENV_FILE" ]; then
  cp "$EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example"
fi

# Format per setting: database/MinIO passwords go into connection URLs (hex only); the
# encryption keys are 32-byte url-safe base64 (Fernet); everything else is a 64-char hex secret.
secret_for() {
  case "$1" in
    DE_POSTGRES_PASSWORD|DE_DB_PASSWORD|DE_MINIO_PASSWORD) openssl rand -hex 24 ;;
    AGENTS_SECRETS_ENCRYPTION_KEY|DATACONNECT_SECRET_ENCRYPTION_KEY)
      openssl rand -base64 32 | tr '+/' '-_' ;;
    *) openssl rand -hex 32 ;;
  esac
}

tmp="$ENV_FILE.tmp.$$"
filled=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    [A-Z]*=\<generate\>)
      key=${line%%=*}
      printf '%s=%s\n' "$key" "$(secret_for "$key")"
      filled=$((filled + 1)) ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$ENV_FILE" > "$tmp"
mv "$tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Generated $filled secret(s)"

sh "$HERE/install-identity.sh" "$ENV_FILE"
sh "$HERE/prepare-vendor.sh"
