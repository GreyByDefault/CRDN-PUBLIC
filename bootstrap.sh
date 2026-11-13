#!/bin/sh
# Keep the entry call last: a failed/truncated pipe must not execute partial code.
crdn_main() {
  set -eu
  operation=${1:-}
  case "$operation" in install|update|uninstall) shift;; *) echo 'Usage: bootstrap.sh install|update|uninstall [--release VERSION]' >&2; return 2;; esac
  version=
  rc_option=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --release)
        [ -z "$version" ] && [ "$#" -ge 2 ] || return 2
        printf '%s\n' "$2" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[1-9][0-9]*)?$' || return 2
        version=$2; shift 2;;
      --enable-rc-updates)
        [ -z "$rc_option" ] || return 2
        rc_option=--enable-rc-updates; shift;;
      *) echo 'Неизвестный параметр загрузчика.' >&2; return 2;;
    esac
  done
  # The shell program may arrive on stdin; interactive prompts use the SSH TTY.
  [ -r /dev/tty ] && [ -w /dev/tty ] || { echo 'CRDN requires an interactive terminal (/dev/tty)' >&2; return 1; }
  crdn_tmp=$(mktemp -d)
  trap 'rm -rf -- "$crdn_tmp"' EXIT
  cd "$crdn_tmp"
  if [ -z "$version" ]; then
    if ! curl --silent --show-error --fail --location --connect-timeout 4 --max-time 10 --max-filesize 8388608 --proto '=https' --proto-redir '=https' -o stable.json https://api.github.com/repos/GreyByDefault/CRDN-PUBLIC/releases/latest; then
      printf '%s\n' 'GitHub недоступен. Получение stable с зеркала NEUROFLO…'
      curl --silent --show-error --fail --location --connect-timeout 4 --max-time 10 --max-filesize 8388608 --proto '=https' --proto-redir '=https' -o stable.json https://neuroflo.ru/crdn-files/stable.json
    fi
    version=$(python3 -c 'import json,sys; v=json.load(open("stable.json")); (isinstance(v,dict) and not v.get("draft") and not v.get("prerelease") and v.get("tag_name")) or sys.exit("Стабильного выпуска CRDN пока нет; предварительный выпуск требует --release"); print(v["tag_name"])')
    printf '%s\n' "$version" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || return 2
  fi
  set -- --release "$version"
  [ -z "$rc_option" ] || set -- "$@" "$rc_option"
  printf '%s\n' 'Скачивание загрузчика CRDN…'
  primary=https://github.com/GreyByDefault/CRDN-PUBLIC/releases/download/$version
  mirror=https://neuroflo.ru/crdn-files/releases/$version
  crdn_fetch() { curl --silent --show-error --fail --location --connect-timeout 4 --max-time 10 --max-filesize 8388608 --proto '=https' --proto-redir '=https' -o "$2" "$1"; }
  if ! { crdn_fetch "$primary/crdn-bootstrap.py" crdn-bootstrap.py && crdn_fetch "$primary/crdn-bootstrap.py.sha256" crdn-bootstrap.py.sha256; }; then
    printf '%s\n' 'GitHub недоступен. Переключение на зеркало NEUROFLO…'
    crdn_fetch "$mirror/crdn-bootstrap.py" crdn-bootstrap.py || { echo "Не удалось скачать загрузчик с зеркала. Операция не запущена." >&2; return 1; }
    crdn_fetch "$mirror/crdn-bootstrap.py.sha256" crdn-bootstrap.py.sha256 || { echo "Не удалось получить контрольную сумму. Операция не запущена." >&2; return 1; }
  fi
  printf '%s\n' 'Проверка файлов загрузчика…'
  python3 -c 'import hashlib,pathlib,re,sys; checksum=pathlib.Path("crdn-bootstrap.py.sha256").read_text(); match=re.fullmatch(r"([0-9a-f]{64})  crdn-bootstrap.py\n?",checksum); sys.exit(0 if match and hashlib.sha256(pathlib.Path("crdn-bootstrap.py").read_bytes()).hexdigest()==match[1] else "Ошибка целостности загрузчика. Операция не запущена.")' || return 1
  python3 ./crdn-bootstrap.py "$operation" "$@" < /dev/tty
}
crdn_main "$@"
