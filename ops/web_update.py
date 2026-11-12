#!/usr/bin/env python3
"""Root-only fixed-operation controller and independent systemd update worker."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import bootstrap

BASE = Path('/var/lib/crdn-updater')
META = Path('/etc/crdn-managed')
ROOT = Path('/opt/crdn-managed')
SOCKET = Path('/run/crdn-updater/control.sock')
WORKER = 'crdn-managed-update.service'
LOCK = threading.Lock()
CHECKING = False
BUSY = ('queued', 'download', 'verify', 'server', 'probe', 'activate', 'health')


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(value) + '\n').encode()
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def version(tag):
    match = re.fullmatch(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?', tag or '')
    if not match:
        raise ValueError('Invalid version')
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), rc is None, int(rc or 0)


def current():
    try: return (ROOT / 'current/release-version').read_text().strip()
    except OSError: return read(META / 'installation.json', {}).get('version')


def channel():
    value = read(META / 'update-channel.json', {'channel': 'stable'})
    if value not in ({'channel': 'stable'}, {'channel': 'rc'}):
        raise ValueError('Invalid root update channel')
    return value['channel']


def latest():
    rc = channel() == 'rc'
    try:
        data = json.loads(bootstrap.fetch(bootstrap.API.rstrip('/') + ('?per_page=100' if rc else '/latest')))
    except (OSError, TimeoutError):
        data = json.loads(bootstrap.fetch(bootstrap.MIRROR + '/stable.json'))
        if rc:
            data = [data, json.loads(bootstrap.fetch(bootstrap.MIRROR + '/prerelease.json'))]
    rows = data if isinstance(data, list) else [data]
    allowed = []
    for item in rows:
        tag = item.get('tag_name')
        if not tag or item.get('draft'):
            continue
        key = version(tag)
        if not rc and (not key[3] or item.get('prerelease')):
            continue
        if item.get('prerelease', False) != (not key[3]):
            raise ValueError('Inconsistent release channel')
        allowed.append(tag)
    return max(allowed, key=version) if allowed else None


def check(manual=False):
    global CHECKING
    with LOCK:
        old = read(BASE / 'check.json', {})
        if CHECKING or time.time() - old.get('checked_at', 0) < (60 if manual else 86400):
            return
        CHECKING = True
        save(BASE / 'check.json', dict(old, checked_at=time.time()))
    def work():
        global CHECKING
        try:
            tag = latest()
            result = dict(checked_at=time.time(), tag=tag, error=False)
        except Exception:
            result = dict(checked_at=time.time(), tag=None, error=True)
        with LOCK:
            save(BASE / 'check.json', result)
            CHECKING = False
    threading.Thread(target=work, daemon=True).start()


def operation():
    data = read(BASE / 'operation.json')
    if data and data.get('phase') in BUSY:
        # Worker survives panel/controller restarts. A reboot cannot imply success.
        active = subprocess.check_output(['systemctl', 'show', WORKER, '-p', 'MainPID', '--value'], text=True).strip() not in ('', '0')
        if not active and time.time() - data.get('started', 0) > 30:
            data['phase'] = 'interrupted'
            save(BASE / 'operation.json', data)
    return data


def status():
    checked = read(BASE / 'check.json', {})
    running = operation()
    tag = checked.get('tag')
    try: available = tag if tag and version(tag) > version(current()) else None
    except ValueError: available = None
    return dict(enabled=True, available=available, channel=channel(), checking=CHECKING,
                checked_at=checked.get('checked_at'), check_error=checked.get('error', False),
                operation=running)


def dispatch(command):
    expected = None
    if command.startswith('update '):
        expected = command[7:]; version(expected)
        command = 'update'
    if command == 'check':
        check(manual=True)
    elif command == 'update':
        with LOCK:
            previous = operation()
            if not previous or previous.get('phase') not in BUSY:
                cached = read(BASE / 'check.json', {})
                tag = cached.get('tag')
                if expected != tag:
                    return dict(error='Доступный выпуск изменился. Проверьте версию и подтвердите обновление заново.')
                if cached.get('error') or time.time() - cached.get('checked_at', 0) > 86400:
                    return dict(error='Сначала проверьте наличие обновлений.')
                if not tag or version(tag) <= version(current()):
                    return dict(error='Более новый выпуск не найден.')
                if channel() == 'stable' and not version(tag)[3]:
                    return dict(error='Предварительные выпуски запрещены.')
                save(BASE / 'operation.json', dict(target=tag, previous=current(), phase='queued', started=time.time()))
                result = subprocess.run(['systemctl', 'start', '--no-block', WORKER])
                if result.returncode:
                    data = read(BASE / 'operation.json'); data['phase'] = 'failed'; save(BASE / 'operation.json', data)
    elif command != 'status':
        return dict(error='Недопустимая операция.')
    return status()


def worker():
    # Fixed root state; no version, path, source or command arguments from IPC.
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(BASE / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(BASE / 'operation.json', {})
        if state.get('phase') != 'queued':
            return
        def stage(phase):
            state['phase'] = phase; save(BASE / 'operation.json', state)
        try:
            tag = state['target']; version(tag)
            if version(tag) <= version(current()) or (channel() == 'stable' and not version(tag)[3]):
                raise ValueError('Version/channel refused')
            stage('download')
            bootstrap.release_tag(tag)
            raw, checksum = bootstrap.release_pair(tag, 'crdn-installer-' + tag + '.tar.gz')
            stage('verify')
            with tempfile.TemporaryDirectory(prefix='crdn-web-tools-') as folder:
                os.chmod(folder, 0o700)
                tools = bootstrap.unpack(raw, checksum, tag, folder)
                stage('server')
                command = [sys.executable, str(tools / 'ops/lifecycle.py'), 'update', '--release', tag]
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
                    for line in child.stdout:
                        if 'Проверка новой версии с временными данными' in line: stage('probe')
                        elif 'Настройка службы и проверка результата' in line: stage('activate')
                        elif 'Обновление CRDN завершено' in line: stage('health')
                    if child.wait(): raise RuntimeError('Lifecycle refused or rolled back')
            installed = read(META / 'installation.json', {})
            if installed.get('version') != tag or current() != tag or subprocess.run(['systemctl', 'is-active', '--quiet', 'crdn-managed.service']).returncode:
                raise RuntimeError('Final installed state differs')
            stage('success')
        except Exception as error:
            state['failed_at'] = state.get('phase')
            state['reason'] = 'integrity' if isinstance(error, ValueError) and ('целостности' in str(error) or 'SHA-256' in str(error)) else None
            # No downloaded error text, secrets, filesystem paths or subprocess logs in public status.
            stage('failed_rolled_back' if current() == state.get('previous') else 'failed')


def serve():
    SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(SOCKET)); os.chmod(SOCKET, 0o660); server.listen(8)
        check()
        server.settimeout(30)
        while True:
            try: connection, _ = server.accept()
            except socket.timeout:
                check(); continue
            with connection:
                connection.settimeout(2)
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in (0, pwd.getpwnam('crdn-managed').pw_uid): continue
                try:
                    raw = connection.makefile('rb').readline(81)
                    command = raw.decode('ascii').rstrip('\n')
                    if raw != (command + '\n').encode() or len(raw) > 80: raise ValueError('Invalid request')
                    result = dispatch(command)
                    connection.sendall((json.dumps(result) + '\n').encode())
                except (OSError, ValueError, KeyError):
                    connection.sendall(b'{"error":"Invalid update request"}\n')
            check()


if __name__ == '__main__':
    if os.geteuid() != 0: raise SystemExit('Root required')
    if sys.argv[1:] == ['--run']: worker()
    elif sys.argv[1:] == []: serve()
    else: raise SystemExit('Unsupported arguments')
