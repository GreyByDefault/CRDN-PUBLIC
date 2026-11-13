#!/usr/bin/env python3
"""Fetch verified release tools and delegate to the existing CRDN lifecycle."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'https://github.com/GreyByDefault/CRDN-PUBLIC'
MIRROR = 'https://neuroflo.ru/crdn-files'
API = 'https://api.github.com/repos/GreyByDefault/CRDN-PUBLIC/releases/'
VERSION = r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?'
BUNDLE_FILES = ('install.sh', 'update.sh', 'uninstall.sh', 'ops/lifecycle.py',
                'ops/package.py', 'ops/build_release.py', 'README.md', 'README.en.md',
                'LICENSE', 'ops/web_update.py', 'bootstrap.py', 'docs/dashboard.png', 'bootstrap.sh')
LIMIT = 8 * 1024 * 1024


class HTTPSOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise ValueError('Refusing non-HTTPS redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    if urlsplit(url).scheme != 'https':
        raise ValueError('HTTPS required')
    with build_opener(HTTPSHandler(), HTTPSOnly()).open(
            Request(url, headers={'User-Agent': 'CRDN-bootstrap'}), timeout=8) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Release file exceeds size limit')
    return data


def release_tag(selected):
    if selected and not re.fullmatch(VERSION, selected):
        raise ValueError('Expected an exact stable or rc.N release tag')
    try:
        metadata = json.loads(fetch(API + ('tags/' + selected if selected else 'latest')))
    except (URLError, OSError, TimeoutError):
        print('GitHub недоступен. Получение сведений о выпуске с зеркала NEUROFLO…', flush=True)
        metadata = json.loads(fetch(MIRROR + ('/releases/' + selected + '/release.json' if selected else '/stable.json')))
    if not metadata.get('tag_name'):
        raise ValueError('Стабильного выпуска CRDN пока нет. Для предварительного выпуска явно укажите --release.')
    tag = metadata.get('tag_name', '')
    if metadata.get('draft') or not re.fullmatch(VERSION, tag) or (selected and tag != selected):
        raise ValueError('Invalid release metadata')
    if not selected and (metadata.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag)):
        raise ValueError('Обычный режим допускает только стабильные выпуски.')
    name = 'crdn-installer-' + tag + '.tar.gz'
    assets = {item['name']: item['browser_download_url'] for item in metadata['assets']}
    prefix = REPOSITORY + '/releases/download/' + tag + '/'
    for asset in (name, name + '.sha256'):
        if assets.get(asset, prefix + asset) != prefix + asset:
            raise ValueError('Missing or nonofficial installer asset: ' + asset)
    return tag, prefix + name, prefix + name + '.sha256'


def release_pair(tag, name, fetcher=None):
    """Only transport/missing-file failures switch source; integrity never does."""
    fetcher = fetcher or fetch
    primary = REPOSITORY + '/releases/download/' + tag + '/'
    mirror = MIRROR + '/releases/' + tag + '/'
    print('Скачивание файлов выпуска ' + tag + '…', flush=True)
    try:
        raw, checksum = fetcher(primary + name), fetcher(primary + name + '.sha256')
    except (URLError, OSError, TimeoutError):
        print('GitHub недоступен. Переключение на зеркало NEUROFLO…', flush=True)
        raw, checksum = fetcher(mirror + name), fetcher(mirror + name + '.sha256')
    print('Проверка файлов выпуска…', flush=True)
    match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(name) + r'\n?', checksum.decode('ascii'))
    if not match or hashlib.sha256(raw).hexdigest() != match[1]:
        raise ValueError('Ошибка целостности файла: ' + name + '. Выполнение остановлено без переключения источника.')
    return raw, checksum


def unpack(raw, checksum, tag, destination):
    name = 'crdn-installer-' + tag + '.tar.gz'
    match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(name) + r'\n?', checksum.decode('ascii'))
    if not match or hashlib.sha256(raw).hexdigest() != match[1]:
        raise ValueError('Контрольная сумма SHA-256 установочного комплекта не совпала.')
    expected = {'crdn-' + tag + '/' + path for path in BUNDLE_FILES}
    content = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for member in archive:
            total += member.size
            if member.name not in expected or member.name in content or not member.isfile() or member.size < 0 or total > LIMIT:
                raise ValueError('Unsafe installer archive entry')
            content[member.name] = archive.extractfile(member).read()
    if set(content) != expected:
        raise ValueError('Incomplete installer bundle')
    for path, data in content.items():
        if path.endswith('.py'):
            compile(data, path, 'exec')
        target = Path(destination) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return Path(destination) / ('crdn-' + tag)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('install', 'update', 'uninstall'))
    parser.add_argument('--release', help='Exact tool/runtime release; required to opt into a Pre-release')
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument('--enable-rc-updates', action='store_true')
    channel.add_argument('--disable-rc-updates', action='store_true')
    parser.add_argument('--domain', help='Optional; otherwise installer asks interactively')
    args = parser.parse_args(argv)
    if args.release and not re.fullmatch(VERSION, args.release):
        parser.error('Invalid exact release tag')
    if os.geteuid() != 0:
        # File-based invocation: sudo and lifecycle inherit the original terminal stdin.
        return subprocess.call(['sudo', '--', sys.executable, str(Path(__file__).resolve()),
                                *(sys.argv[1:] if argv is None else argv)])
    tag, url, checksum_url = release_tag(args.release)
    raw, checksum = release_pair(tag, 'crdn-installer-' + tag + '.tar.gz')
    with tempfile.TemporaryDirectory(prefix='crdn-tools-') as directory:
        os.chmod(directory, 0o700)
        root = unpack(raw, checksum, tag, directory)
        command = [sys.executable, str(root / 'ops/lifecycle.py'), args.operation]
        if args.operation != 'uninstall':
            # Pin the runtime to the release whose complete tool bundle was verified.
            command += ['--release', tag]
        if args.enable_rc_updates:
            command += ['--enable-rc-updates']
        elif args.disable_rc_updates:
            command += ['--disable-rc-updates']
        if args.domain:
            command += ['--domain', args.domain]
        return subprocess.call(command)


def parse_release_notes(tag, body):
    """Derive bounded plain-text sections from the exact published Markdown body."""
    if not isinstance(body, str) or len(body.encode('utf-8')) > 32768:
        return None
    sections = {'added': [], 'fixed': []}
    headings = {'Добавлено': 'added', 'Исправлено': 'fixed', 'Added': 'added', 'Fixed': 'fixed'}
    section = None
    remaining = 1200
    for line in body.splitlines():
        title = re.fullmatch(r'#{1,6}\s+(.+?)\s*', line)
        if title:
            section = headings.get(title[1])
        elif section and re.match(r'^\s*[-*]\s+', line) and remaining > 0:
            text = ' '.join(re.sub(r'^\s*[-*]\s+', '', line).split())
            text = ''.join(c for c in text if c.isprintable())[:min(240, remaining)]
            if text and len(sections[section]) < 12:
                sections[section].append(text)
                remaining -= len(text)
    return dict(version=tag, **sections) if any(sections.values()) else None


def valid_release_notes(value, tag):
    if not isinstance(value, dict) or set(value) != {'version', 'added', 'fixed'} or value['version'] != tag:
        return None
    if any(not isinstance(value[k], list) or len(value[k]) > 12 or any(not isinstance(t, str) or not t or len(t) > 240 or not t.isprintable() for t in value[k]) for k in ('added', 'fixed')):
        return None
    if sum(len(t) for k in ('added', 'fixed') for t in value[k]) > 1200 or not (value['added'] or value['fixed']):
        return None
    return value


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('CRDN: файлы не подготовлены. Операция не запущена.', file=sys.stderr)
        raise SystemExit('Причина: ' + str(error))
