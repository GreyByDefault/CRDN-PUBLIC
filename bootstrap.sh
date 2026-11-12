#!/bin/sh
# Keep the entry call last: a failed/truncated pipe must not execute partial code.
crdn_main() {
  set -eu
  operation=${1:-}
  case "$operation" in install|update|uninstall) shift;; *) echo 'Usage: bootstrap.sh install|update|uninstall [--release VERSION]' >&2; return 2;; esac
  version=
  if [ "$#" -ne 0 ]; then
    [ "$#" -eq 2 ] && [ "$1" = --release ] || return 2
    printf '%s\n' "$2" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[1-9][0-9]*)?$' || return 2
    version=$2
  fi
  # The shell program may arrive on stdin; interactive prompts use the SSH TTY.
  [ -r /dev/tty ] && [ -w /dev/tty ] || { echo 'CRDN requires an interactive terminal (/dev/tty)' >&2; return 1; }
  crdn_tmp=$(mktemp -d)
  trap 'rm -rf -- "$crdn_tmp"' EXIT
  cd "$crdn_tmp"
  if [ -z "$version" ]; then
    curl --silent --show-error --fail --location --connect-timeout 4 --max-time 10 --proto '=https' --proto-redir '=https' -o stable.json https://neuroflo.ru/crdn-files/stable.json
    version=$(python3 -c 'import json; v=json.load(open("stable.json")); assert not v.get("prerelease") and v.get("tag_name"), "Стабильного выпуска CRDN пока нет; предварительный выпуск требует --release"; print(v["tag_name"])')
    printf '%s\n' "$version" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || return 2
    set -- --release "$version"
  fi
  printf '%s\n' 'Скачивание загрузчика CRDN…'
  primary=https://github.com/GreyByDefault/CRDN-PUBLIC/releases/download/$version
  mirror=https://neuroflo.ru/crdn-files/releases/$version
  crdn_fetch() { curl --silent --show-error --fail --location --connect-timeout 4 --max-time 10 --proto '=https' --proto-redir '=https' -o "$2" "$1"; }
  if ! { crdn_fetch "$primary/crdn-bootstrap.py" crdn-bootstrap.py && crdn_fetch "$primary/crdn-bootstrap.py.sha256" crdn-bootstrap.py.sha256; }; then
    printf '%s\n' 'GitHub недоступен. Переключение на зеркало NEUROFLO…'
    crdn_fetch "$mirror/crdn-bootstrap.py" crdn-bootstrap.py || { echo "Не удалось скачать загрузчик с зеркала. Операция не запущена." >&2; return 1; }
    crdn_fetch "$mirror/crdn-bootstrap.py.sha256" crdn-bootstrap.py.sha256 || { echo "Не удалось получить контрольную сумму. Операция не запущена." >&2; return 1; }
  fi
  printf '%s\n' 'Проверка файлов загрузчика…'
  sha256sum --status -c crdn-bootstrap.py.sha256 || { echo 'Ошибка целостности загрузчика. Операция не запущена.' >&2; return 1; }
  python3 ./crdn-bootstrap.py "$operation" "$@" < /dev/tty
}
crdn_main "$@"
