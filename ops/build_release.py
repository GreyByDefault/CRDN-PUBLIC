#!/usr/bin/env python3
"""Build runtime and self-contained installer assets from committed release files."""
import argparse
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path
import re
from package import package


def build(version, destination):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?', version):
        raise ValueError('Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N')
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / ('crdn-' + version + '.tar')
    if path.exists() or path.with_suffix('.tar.sha256').exists():
        raise ValueError('Output already exists')
    bundle = destination / ('crdn-installer-' + version + '.tar.gz')
    if bundle.exists() or bundle.with_suffix('.gz.sha256').exists():
        raise ValueError('Installer output already exists')
    # Every executable helper is packaged from committed HEAD, never loose files.
    helpers = ('install.sh', 'update.sh', 'uninstall.sh', 'ops/lifecycle.py',
               'ops/package.py', 'ops/build_release.py', 'README.md', 'README.en.md', 'LICENSE')
    content = {}
    for name in helpers:
        data = subprocess.check_output(['git', 'show', 'HEAD:' + name])
        if Path(name).read_bytes() != data:
            raise ValueError('Installer must match committed source: ' + name)
        content[name] = data
    package(path)
    with tarfile.open(bundle, 'w:gz') as archive:
        for name, data in content.items():
            member = tarfile.TarInfo('crdn-' + version + '/' + name)
            member.mode = 0o755 if name.endswith('.sh') else 0o644
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    bundle.with_suffix('.gz.sha256').write_text(hashlib.sha256(bundle.read_bytes()).hexdigest() + '  ' + bundle.name + '\n')
    path.with_suffix('.tar.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(build(args.version, args.destination))
