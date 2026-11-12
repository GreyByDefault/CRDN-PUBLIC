#!/bin/sh
# Keep the entry call last: a failed/truncated pipe must not execute partial code.
crdn_main() {
  set -eu
  operation=${1:-}
  case "$operation" in install|update|uninstall) shift;; *) echo 'Usage: bootstrap.sh install|update|uninstall [--release VERSION]' >&2; return 2;; esac
  base=https://github.com/GreyByDefault/CRDN-PUBLIC/releases/latest/download
  if [ "$#" -ne 0 ]; then
    [ "$#" -eq 2 ] && [ "$1" = --release ] || return 2
    printf '%s\n' "$2" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[1-9][0-9]*)?$' || return 2
    base=https://github.com/GreyByDefault/CRDN-PUBLIC/releases/download/$2
  fi
  # The shell program may arrive on stdin; interactive prompts use the SSH TTY.
  [ -r /dev/tty ] && [ -w /dev/tty ] || { echo 'CRDN requires an interactive terminal (/dev/tty)' >&2; return 1; }
  crdn_tmp=$(mktemp -d)
  trap 'rm -rf -- "$crdn_tmp"' EXIT
  cd "$crdn_tmp"
  curl --fail --location --proto '=https' --proto-redir '=https' -o crdn-bootstrap.py "$base/crdn-bootstrap.py"
  curl --fail --location --proto '=https' --proto-redir '=https' -o crdn-bootstrap.py.sha256 "$base/crdn-bootstrap.py.sha256"
  sha256sum -c crdn-bootstrap.py.sha256
  python3 ./crdn-bootstrap.py "$operation" "$@" < /dev/tty
}
crdn_main "$@"
