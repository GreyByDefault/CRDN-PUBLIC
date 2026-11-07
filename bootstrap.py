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
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'https://github.com/GreyByDefault/CRDN-PUBLIC'
API = 'https://api.github.com/repos/GreyByDefault/CRDN-PUBLIC/releases/'
VERSION = r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?'
BUNDLE_FILES = ('install.sh', 'update.sh', 'uninstall.sh', 'ops/lifecycle.py',
                'ops/package.py', 'ops/build_release.py', 'README.md', 'README.en.md',
                'LICENSE', 'bootstrap.py', 'docs/dashboard.png')
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
            Request(url, headers={'User-Agent': 'CRDN-bootstrap'}), timeout=30) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Release file exceeds size limit')
    return data


def release_tag(selected):
    if selected and not re.fullmatch(VERSION, selected):
        raise ValueError('Expected an exact stable or rc.N release tag')
    try:
        metadata = json.loads(fetch(API + ('tags/' + selected if selected else 'latest')))
    except HTTPError as error:
        if error.code == 404:
            raise ValueError('Requested release not found' if selected else
                             'No stable CRDN release exists; select an RC explicitly with --release') from error
        raise
    tag = metadata.get('tag_name', '')
    if metadata.get('draft') or not re.fullmatch(VERSION, tag) or (selected and tag != selected):
        raise ValueError('Invalid release metadata')
    if not selected and (metadata.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag)):
        raise ValueError('Default mode accepts stable releases only')
    name = 'crdn-installer-' + tag + '.tar.gz'
    assets = {item['name']: item['browser_download_url'] for item in metadata['assets']}
    prefix = REPOSITORY + '/releases/download/' + tag + '/'
    for asset in (name, name + '.sha256'):
        if assets.get(asset) != prefix + asset:
            raise ValueError('Missing or nonofficial installer asset: ' + asset)
    return tag, assets[name], assets[name + '.sha256']


def unpack(raw, checksum, tag, destination):
    name = 'crdn-installer-' + tag + '.tar.gz'
    match = re.fullmatch(r'([0-9a-f]{64})  ' + re.escape(name) + r'\n?', checksum.decode('ascii'))
    if not match or hashlib.sha256(raw).hexdigest() != match[1]:
        raise ValueError('Installer SHA-256 mismatch')
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
    parser.add_argument('--domain', help='Optional; otherwise installer asks interactively')
    args = parser.parse_args(argv)
    if args.release and not re.fullmatch(VERSION, args.release):
        parser.error('Invalid exact release tag')
    if os.geteuid() != 0:
        # File-based invocation: sudo and lifecycle inherit the original terminal stdin.
        return subprocess.call(['sudo', '--', sys.executable, str(Path(__file__).resolve()),
                                *(sys.argv[1:] if argv is None else argv)])
    tag, url, checksum_url = release_tag(args.release)
    raw, checksum = fetch(url), fetch(checksum_url)
    with tempfile.TemporaryDirectory(prefix='crdn-tools-') as directory:
        os.chmod(directory, 0o700)
        root = unpack(raw, checksum, tag, directory)
        command = [sys.executable, str(root / 'ops/lifecycle.py'), args.operation]
        if args.operation != 'uninstall':
            # Pin the runtime to the release whose complete tool bundle was verified.
            command += ['--release', tag]
        if args.domain:
            command += ['--domain', args.domain]
        return subprocess.call(command)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit('CRDN: ' + str(error))
