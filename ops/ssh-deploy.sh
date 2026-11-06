#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in check|upload|deploy|rollback) ;; *) exit 2 ;; esac
test "$#" -eq 1
: "${CRDN_DEPLOY_SSH_KEY:?Missing CRDN deployment key}"
: "${CRDN_DEPLOY_HOST:?Missing CRDN host}"
: "${CRDN_DEPLOY_USER:?Missing CRDN user}"
: "${CRDN_DEPLOY_KNOWN_HOSTS:?Missing pinned host key}"
test "$CRDN_DEPLOY_USER" = deploy-crdn
secure_dir="$(mktemp -d)"
trap 'rm -rf -- "$secure_dir"' EXIT
umask 077
printf '%s\n' "$CRDN_DEPLOY_SSH_KEY" > "$secure_dir/key"
printf '%s\n' "$CRDN_DEPLOY_KNOWN_HOSTS" > "$secure_dir/known_hosts"
ssh -4 -T -i "$secure_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null \
  -o UserKnownHostsFile="$secure_dir/known_hosts" -o ConnectTimeout=15 \
  "${CRDN_DEPLOY_USER}@${CRDN_DEPLOY_HOST}" "$1"
