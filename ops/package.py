#!/usr/bin/env python3
"""Build an exact, dependency-free runtime archive; never include host config."""
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile

FILES = ('crdn/web/theme.js', 'crdn/settings.py', 'crdn/web/shell.js', 'crdn/web/sw.js', 'crdn/web/icon-192.png', 'crdn/web/icon-512.png', 'crdn/web/apple-touch-icon.png', 'crdn/__init__.py', 'crdn/api.py', 'crdn/metrics.py',
         'crdn/web/favicon.ico', 'crdn/web/index.html', 'crdn/web/style.css', 'crdn/web/app.js',
         'crdn/updates.py', 'crdn/web/updates.js', 'crdn/auth.py', 'crdn/web/login.html', 'crdn/web/auth.css', 'crdn/web/auth.js')


def package(destination, version=None):
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Invalid commit')
    content = {}
    for name in FILES:
        path = Path(name)
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid runtime file')
        data = path.read_bytes()
        if data != subprocess.check_output(['git', 'show', f'{sha}:{name}']):
            raise ValueError('Runtime must match the committed source')
        if name.endswith('.py'):
            compile(data, name, 'exec')
        content[name] = data
    content['release-id'] = (sha + '\n').encode()
    if version:
        if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?', version): raise ValueError('Invalid release version')
        content['release-version'] = (version + '\n').encode()
    with tarfile.open(destination, 'w:') as archive:
        for name, data in content.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o644
            archive.addfile(item, io.BytesIO(data))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: package.py OUTPUT.tar')
    package(sys.argv[1])
