"""Unprivileged, bounded client of the root-owned local update controller."""
import json
from pathlib import Path
import re
import socket

SOCKET = '/run/crdn-updater/control.sock'


def installed_version():
    try:
        value = (Path(__file__).resolve().parents[1] / 'release-version').read_text().strip()
        if re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?', value):
            return value
    except OSError:
        pass
    return None


def request(operation='status', target=None):
    if operation not in ('status', 'check', 'update'):
        raise ValueError('Unknown update operation')
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2)
            connection.connect(SOCKET)
            if operation == 'update':
                if not isinstance(target, str) or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?', target) or len(target) > 64:
                    raise ValueError('Invalid expected version')
                operation += ' ' + target
            connection.sendall((operation + '\n').encode())
            stream = connection.makefile('rb')
            raw = stream.readline(8193)
            if len(raw) > 8192:
                raise ValueError('Oversized controller response')
            result = json.loads(raw)
    except (OSError, ValueError):
        result = dict(available=None, enabled=False, operation=None,
                      message='Веб-обновление недоступно. Требуется ручное обновление установщиком.')
    result['current'] = installed_version()
    return result
