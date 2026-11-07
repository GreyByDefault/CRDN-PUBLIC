#!/usr/bin/env python3
"""Build a stable-release runtime asset and SHA-256 from committed runtime files."""
import argparse
import hashlib
from pathlib import Path
import re
from package import package


def build(version, destination):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise ValueError('Expected vMAJOR.MINOR.PATCH')
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / ('crdn-' + version + '.tar')
    if path.exists() or path.with_suffix('.tar.sha256').exists():
        raise ValueError('Output already exists')
    package(path)
    path.with_suffix('.tar.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(build(args.version, args.destination))
