#!/usr/bin/env bash
# GIT_ASKPASS helper: token-as-password authentication from the environment.
#
# NOT used by the app's own git operations any more: LocalRepo / _do_clone
# pass the credential as an Authorization header through GIT_CONFIG_COUNT /
# GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n env vars for that one subprocess
# (app/git/git_local.py:git_auth_env), so nothing is ever written to
# .git/config. Kept for operators who drive git by hand inside the pod:
#
#   WORKSPACE_GIT_USERNAME=oauth2 WORKSPACE_GIT_TOKEN=... \
#     GIT_ASKPASS=/usr/local/bin/git-credential-helper.sh git fetch
#
# Usernames: github x-access-token, gitlab oauth2, bitbucket x-token-auth,
# azure_devops any non-empty value. The token is read from the environment
# only and never echoed anywhere but git's stdin pipe.

set -euo pipefail

case "${1:-}" in
  Username*)
    echo "${WORKSPACE_GIT_USERNAME:-x-access-token}"
    ;;
  Password*)
    echo "${WORKSPACE_GIT_TOKEN:-}"
    ;;
  *)
    echo ""
    ;;
esac
