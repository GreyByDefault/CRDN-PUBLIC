#!/usr/bin/env python3
"""Root-only CRDN lifecycle. No network/server configuration outside owned files."""
import argparse
import fcntl
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import pwd
import grp
import re
import shutil
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from package import FILES

ROOT = Path('/opt/crdn-managed')
META = Path('/etc/crdn-managed')
STATE = META / 'installation.json'
JOURNAL = META / 'transaction.json'
DATA = Path('/var/lib/crdn-managed')
UNIT = 'crdn-managed.service'
ACME_WEBROOT = Path('/var/lib/crdn-managed-acme')
HELPER_DIR = Path('/usr/local/lib/crdn-managed')
CADDY_SITE_DIR = Path('/etc/caddy/crdn-managed')
STAGING = 'https://acme-staging-v02.api.letsencrypt.org/directory'
PRODUCTION = 'https://acme-v02.api.letsencrypt.org/directory'
LIMIT = 4 * 1024 * 1024


def run(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(' '.join(args) + ': ' + result.stderr[-3000:])
    return result.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.crdn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def save(path, value):
    atomic(path, (json.dumps(value, indent=2) + '\n').encode())


class Transaction:
    """Write-ahead backups; rollback uses compare-and-swap, never blind restore."""
    def __init__(self):
        self.entries = []

    def write(self, path, data, mode=0o644):
        path = Path(path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Not a regular file: ' + str(path))
        old = path.read_bytes() if path.exists() else None
        entry = dict(path=str(path), before=old.hex() if old is not None else None,
                     mode=path.stat().st_mode & 0o777 if old is not None else mode,
                     after=digest(data) if data is not None else None)
        self.entries.append(entry)
        save(JOURNAL, self.entries)
        # Recheck after persisting the journal.
        if (path.read_bytes() if path.exists() else None) != old:
            raise RuntimeError('Concurrent change: ' + str(path))
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, data, mode)

    def rollback(self):
        conflicts = []
        for entry in reversed(self.entries):
            path = Path(entry['path'])
            current = digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
            before = bytes.fromhex(entry['before']) if entry['before'] is not None else None
            if current == (digest(before) if before is not None else None):
                continue
            if path.is_symlink() or current != entry['after']:
                conflicts.append(str(path))
                continue
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, before, entry['mode'])
        if conflicts:
            raise RuntimeError('Parallel edits preserved; journal retained for recovery: ' + ', '.join(conflicts))
        JOURNAL.unlink(missing_ok=True)

    def commit(self):
        JOURNAL.unlink(missing_ok=True)


def download(url):
    if not url.startswith('https://'):
        raise ValueError('HTTPS source required')
    with urlopen(Request(url, headers={'User-Agent': 'CRDN-installer'}), timeout=30) as response:
        if not response.url.startswith('https://'):
            raise ValueError('Insecure redirect')
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Download exceeds limit')
    return data


def source(args):
    if args.test_archive:
        if not args.test_sha256:
            raise ValueError('--test-archive requires --test-sha256')
        raw = Path(args.test_archive).read_bytes()
        expected = args.test_sha256
    else:
        # Shared source selection is tooling-only; the installed runtime imports none of it.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import bootstrap
        selected = getattr(args, 'release', None)
        tag, _, _ = bootstrap.release_tag(selected)
        raw, checksum = bootstrap.release_pair(tag, 'crdn-' + tag + '.tar')
        expected = checksum.decode('ascii').split()[0]
        args.release = tag
    content = validate_archive(raw, expected)
    if not args.test_archive and content.get('release-version') != (args.release + '\n').encode():
        raise ValueError('Runtime version differs from selected release')
    return content


def validate_archive(raw, expected):
    if len(raw) > LIMIT or not re.fullmatch('[0-9a-f]{64}', expected) or digest(raw) != expected:
        raise ValueError('Контрольная сумма SHA-256 архива не совпала либо превышен допустимый размер.')
    content = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        total = 0
        for member in archive:
            total += member.size
            if member.name not in {*FILES, 'release-id', 'release-version'} or member.name in content or not member.isfile() or member.size < 0 or total > LIMIT:
                raise ValueError('Unsafe archive entry')
            content[member.name] = archive.extractfile(member).read()
    if set(content) - {'release-version'} != {*FILES, 'release-id'} or not re.fullmatch(rb'[0-9a-f]{40}\n', content['release-id']):
        raise ValueError('Incomplete/invalid runtime')
    if 'release-version' in content and not re.fullmatch(rb'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?\n', content['release-version']):
        raise ValueError('Invalid installed version')
    for name, data in content.items():
        if name.endswith('.py'):
            compile(data, name, 'exec')
    return content


def platform_check():
    if os.geteuid() != 0:
        raise ValueError('Запустите команду от root или через sudo.')
    info = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if info.get('ID', '').strip('"') not in ('ubuntu', 'debian'):
        raise ValueError('Поддерживаются только Ubuntu Server и Debian.')
    versions = {'ubuntu': ('22.04', '24.04', '26.04'), 'debian': ('12', '13')}
    if info.get('VERSION_ID', '').strip('"') not in versions[info['ID'].strip('"')]:
        raise ValueError('Эта версия ОС не поддерживается установщиком.')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('Требуется запущенная система systemd.')
    for command in ('systemctl', 'ss', 'ip', 'apt-get', 'runuser', 'useradd'):
        if not shutil.which(command):
            raise ValueError('Не найдена необходимая системная команда: ' + command)
    if shutil.disk_usage('/opt').free < 1024**3:
        raise ValueError('В /opt требуется не менее 1 ГиБ свободного места.')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def validate(web):
    if web == 'caddy':
        run('caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
    elif web == 'nginx':
        run('nginx', '-t')
    else:
        run('apache2ctl', 'configtest')


def reload(web):
    validate(web)
    run('systemctl', 'reload', web)


def caddy_command(value, operation):
    """Parse one effective systemd Exec command; reject wrappers/extra commands."""
    match = re.fullmatch(r'\{ path=/usr/bin/caddy ; argv\[\]=([^;]+) ; ignore_errors=no ; [^{}]*\}', value.strip())
    if not match:
        raise ValueError('Unsupported effective Caddy Exec' + operation)
    argv = shlex.split(match[1])
    expected = 'run' if operation == 'Start' else 'reload'
    if argv[:2] != ['/usr/bin/caddy', expected]:
        raise ValueError('Caddy must use its direct run/reload command')
    args = argv[2:]
    if args.count('--config') != 1:
        raise ValueError('Caddy needs one explicit Caddyfile')
    index = args.index('--config')
    if args[index:index+2] != ['--config', '/etc/caddy/Caddyfile']:
        raise ValueError('Caddy must use /etc/caddy/Caddyfile')
    del args[index:index+2]
    if '--adapter' in args:
        index = args.index('--adapter')
        if args[index:index+2] != ['--adapter', 'caddyfile']:
            raise ValueError('Only the Caddyfile adapter is supported')
        del args[index:index+2]
    allowed = {'--environ'} if operation == 'Start' else {'--force'}
    if len(args) != len(set(args)) or set(args) - allowed:
        raise ValueError('Unsupported Caddy flags: ' + ' '.join(args))
    return argv


def compatible_caddy_service(pid):
    start = caddy_command(run('systemctl', 'show', 'caddy', '-p', 'ExecStart', '--value'), 'Start')
    caddy_command(run('systemctl', 'show', 'caddy', '-p', 'ExecReload', '--value'), 'Reload')
    actual = Path('/proc/' + pid + '/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    if actual != start:
        raise ValueError('Running Caddy differs from effective systemd ExecStart')
    if Path('/proc/' + pid + '/exe').resolve() != Path('/usr/bin/caddy').resolve():
        raise ValueError('Running Caddy executable differs from validation executable')
    # Environment/drop-ins are not rejected by name. Effective commands plus
    # native validation and comparison with the live admin config establish support.


def survey(domain):
    active = [w for w in ('caddy', 'nginx', 'apache2') if run('systemctl', 'is-active', w, check=False).strip() == 'active']
    listeners = run('ss', '-H', '-ltnp', '( sport = :80 or sport = :443 )')
    if len(active) > 1 or (listeners and not active):
        raise ValueError('Ambiguous 80/443 listeners or multiple web servers: ' + listeners)
    if not active:
        for w, folder in (('caddy', '/etc/caddy'), ('nginx', '/etc/nginx'), ('apache2', '/etc/apache2')):
            if Path(folder).exists() or shutil.which(w):
                raise ValueError('Inactive/existing web-server configuration requires manual review: ' + folder)
        return 'caddy', True
    web = active[0]
    if any(name != web for name in re.findall(r'\(\("([^"]+)"', listeners)):
        raise ValueError('Unknown listener process: ' + listeners)
    unit = run('systemctl', 'cat', web)
    if web != 'caddy' and ('/etc/systemd/system/' in unit or 'EnvironmentFile=' in unit):
        raise ValueError('Custom web-server unit/environment requires manual review')
    pid = run('systemctl', 'show', web, '-p', 'MainPID', '--value').strip()
    command = Path('/proc/' + pid + '/cmdline').read_bytes().replace(b'\0', b' ').decode()
    if re.search(r'(?:^|\s)-(?:c|p|f)\s', command) and web != 'caddy':
        raise ValueError('Custom web-server config arguments: ' + command)
    validate(web)
    if web == 'caddy':
        compatible_caddy_service(pid)
        text = run('caddy', 'adapt', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
        try:
            with urlopen('http://127.0.0.1:2019/config/', timeout=3) as response:
                effective = json.load(response)
        except (OSError, ValueError) as error:
            raise ValueError('Cannot verify effective Caddy admin configuration on loopback:2019') from error
        if effective != json.loads(text):
            raise ValueError('Caddy running configuration differs from Caddyfile; manual reconciliation required')
        if 'load' in text or 'admin' in json.loads(text) and json.loads(text)['admin'].get('disabled'):
            raise ValueError('Custom Caddy administration requires manual review')
    elif web == 'nginx':
        text = run('nginx', '-T')
        if 'include /etc/nginx/conf.d/*.conf;' not in text:
            raise ValueError('No standard nginx conf.d include')
    else:
        text = run('apache2ctl', '-t', '-D', 'DUMP_VHOSTS') + run('apache2ctl', '-t', '-D', 'DUMP_INCLUDES')
        modules = run('apache2ctl', '-M')
        if any(m + '_module' not in modules for m in ('proxy', 'proxy_http', 'headers', 'ssl', 'alias')):
            raise ValueError('Apache requires proxy, proxy_http, headers, ssl and alias modules enabled by administrator')
        text += '\n'.join(p.read_text(errors='replace') for p in Path('/etc/apache2').rglob('*.conf') if p.is_file())
        if 'sites-enabled' not in text:
            raise ValueError('No standard Apache sites-enabled include')
    if re.search(r'(?i)plesk|cpanel|ispconfig|hestia|webmin|cloudpanel|trusted_proxies|set_real_ip_from|RemoteIPHeader', text):
        raise ValueError('Managed configuration or upstream proxy detected')
    if web == 'apache2' and re.search(r'ServerAlias\s+[^\n]*[?*]', text):
        raise ValueError('Apache wildcard aliases require manual review')
    if domain in text or ('*.' + domain.split('.', 1)[1]) in text or re.search(r'server_name\s+[^;]*[~*]', text):
        raise ValueError('Домен уже занят либо используется неоднозначная маршрутизация wildcard/regex. Нужна ручная проверка конфигурации.')
    return web, False


def site(web, domain, port, tls, staging=False):
    if web == 'caddy':
        acme = '\n tls {\n ca ' + STAGING + '\n }' if staging else ''
        return f'{domain} {{\n reverse_proxy 127.0.0.1:{port} {{\n header_up X-CRDN-Client-IP {{remote_host}}\n }}{acme}\n}}\n'
    root = '/var/lib/crdn-managed-acme'
    cert = '/etc/crdn-managed/acme/config/live/crdn/fullchain.pem'
    key = '/etc/crdn-managed/acme/config/live/crdn/privkey.pem'
    if web == 'nginx':
        http = f'server {{\n listen 80; listen [::]:80; server_name {domain};\n location /.well-known/acme-challenge/ {{ root {root}; }}\n location / {{ return 301 https://{domain}$request_uri; }}\n}}\n'
        return http + (f'server {{\n listen 443 ssl; listen [::]:443 ssl; server_name {domain};\n ssl_certificate {cert}; ssl_certificate_key {key};\n location / {{ proxy_pass http://127.0.0.1:{port}; proxy_set_header Host $host; proxy_set_header X-CRDN-Client-IP $remote_addr; }}\n}}\n' if tls else '')
    http = f'<VirtualHost *:80>\n ServerName {domain}\n Alias /.well-known/acme-challenge/ {root}/.well-known/acme-challenge/\n <Directory {root}>\n Require all granted\n </Directory>\n RedirectMatch 301 ^/(?!\\.well-known/acme-challenge/)(.*)$ https://{domain}/$1\n</VirtualHost>\n'
    return http + (f'<VirtualHost *:443>\n ServerName {domain}\n SSLEngine on\n SSLCertificateFile {cert}\n SSLCertificateKeyFile {key}\n ProxyPreserveHost On\n RequestHeader set X-CRDN-Client-IP "expr=%{{REMOTE_ADDR}}"\n ProxyPass / http://127.0.0.1:{port}/\n ProxyPassReverse / http://127.0.0.1:{port}/\n</VirtualHost>\n' if tls else '')


def unit(domain, port):
    return f'''[Unit]
Description=CRDN Server Monitor
After=network.target
[Service]
User=crdn-managed
Group=crdn-managed
WorkingDirectory={ROOT}/current
ExecStart=/usr/bin/python3 -B -m crdn.api --port {port}
Environment=CRDN_STATE_DIR={DATA}
Environment=CRDN_ORIGIN=https://{domain}
StateDirectory=crdn-managed
StateDirectoryMode=0700
UMask=0077
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
[Install]
WantedBy=multi-user.target
'''


def health(port, process=None):
    for _ in range(60):
        if process is not None and process.poll() is not None:
            raise RuntimeError('Новая версия CRDN не запустилась.')
        try:
            with urlopen(f'http://127.0.0.1:{port}/login', timeout=1) as response:
                if b'login-form' not in response.read():
                    raise ValueError('Проверка страницы входа CRDN не пройдена.')
            try:
                urlopen(f'http://127.0.0.1:{port}/api/metrics', timeout=1)
                raise ValueError('Проверка защиты метрик входом не пройдена.')
            except HTTPError as error:
                if error.code != 401:
                    raise
            return
        except OSError:
            time.sleep(.25)
    raise RuntimeError('CRDN не ответил на проверку работоспособности за отведённое время.')


def probe(candidate, domain):
    user = pwd.getpwnam('crdn-managed')
    with tempfile.TemporaryDirectory(prefix='crdn-probe-') as folder:
        os.chown(folder, user.pw_uid, user.pw_gid)
        port = free_port()
        process = subprocess.Popen(['runuser', '-u', 'crdn-managed', '--', '/usr/bin/python3', '-B', '-m', 'crdn.api', '--port', str(port)], cwd=candidate,
                                   env=dict(os.environ, CRDN_STATE_DIR=folder, CRDN_ORIGIN='https://' + domain),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            health(port, process)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def switch(target):
    link = ROOT / 'current.next'
    link.unlink(missing_ok=True)
    link.symlink_to(target)
    os.replace(link, ROOT / 'current')


def activate(content, state):
    candidate = ROOT / 'releases' / (content['release-id'].decode().strip() + ('-' + content['release-version'].decode().strip() if 'release-version' in content else ''))
    if candidate.exists():
        if any(not (candidate / n).is_file() or (candidate / n).read_bytes() != d for n, d in content.items()):
            raise ValueError('Existing release differs')
    else:
        for name, data in content.items():
            atomic(candidate / name, data, 0o644)
        for folder in [candidate, *[p for p in candidate.rglob('*') if p.is_dir()]]:
            folder.chmod(0o755)
    state.setdefault('releases', {})[str(candidate)] = {name: digest(data) for name, data in content.items()}
    save(STATE, state)
    print('Проверка новой версии с временными данными…', flush=True)
    probe(candidate, state['domain'])
    old = str((ROOT / 'current').resolve()) if (ROOT / 'current').is_symlink() else None
    # Persist activation intent before switching for recovery after SIGKILL/power loss.
    state['pending'] = dict(old=old, new=str(candidate), version=state.get('version'))
    save(STATE, state)
    try:
        switch(candidate)
        print('Настройка службы и проверка результата…', flush=True)
        run('systemctl', 'restart', UNIT)
        health(state['port'])
        run('systemctl', 'is-active', '--quiet', UNIT)
        state['active'] = str(candidate)
        if 'release-version' in content: state['version'] = content['release-version'].decode().strip()
        state.pop('pending')
        save(STATE, state)
    except BaseException:
        before_version = state.get('pending', {}).get('version')
        if old:
            switch(old)
            run('systemctl', 'restart', UNIT)
            health(state['port'])
        else:
            run('systemctl', 'stop', UNIT, check=False)
            (ROOT / 'current').unlink(missing_ok=True)
        state['active'] = old
        state['version'] = before_version
        state.pop('pending', None)
        save(STATE, state)
        raise


def owned_check(state):
    for name, expected in state['files'].items():
        p = Path(name)
        if p.is_symlink() or not p.is_file() or digest(p.read_bytes()) != expected:
            raise ValueError('Owned file was changed; manual reconciliation required: ' + name)


UPDATER_CODE = Path('/usr/local/lib/crdn-managed-updater')
UPDATER_DATA = Path('/var/lib/crdn-updater')
UPDATER_RUN = Path('/run/crdn-updater')


def updater_resources(enable_rc=False):
    tools = Path(__file__).resolve().parents[1]
    resources = {str(UPDATER_CODE / name): (tools / name).read_bytes() for name in
                 ('bootstrap.py', 'ops/web_update.py')}
    for name in ('lifecycle.py', 'package.py'):
        resources[str(UPDATER_CODE / 'ops' / name)] = (tools / 'ops' / name).read_bytes()
    # A single root controller accepts bounded fixed requests over a local socket;
    # a separate oneshot performs updates independently of controller/API restarts.
    for unit_name, worker in (('crdn-updater.service', False), ('crdn-managed-update.service', True)):
        text = f'''[Unit]
Description=CRDN {'verified update worker' if worker else 'local update controller'}
After=network.target
[Service]
Type={'oneshot' if worker else 'simple'}
User=root
Group=crdn-managed
UMask=0077
ExecStart=/usr/bin/python3 -B {UPDATER_CODE}/ops/web_update.py{' --run' if worker else ''}
Environment=PYTHONPATH={UPDATER_CODE}
StateDirectory=crdn-updater
StateDirectoryMode=0700
NoNewPrivileges=yes
PrivateTmp=yes
{'ProtectSystem=strict' if not worker else ''}
{'ProtectHome=yes' if not worker else ''}
RuntimeDirectory=crdn-updater
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=yes
TimeoutStartSec={'600' if worker else '30'}
TimeoutStopSec=20
KillMode=control-group
{'Restart=on-failure' if not worker else ''}
[Install]
WantedBy=multi-user.target
'''
        resources['/etc/systemd/system/' + unit_name] = text.encode()
    config = META / 'update-channel.json'
    if enable_rc or not config.exists():
        resources[str(config)] = json.dumps({'channel': 'rc' if enable_rc else 'stable'}).encode()
    return resources


def managed_update(content, state, args):
    import copy
    previous = copy.deepcopy(state)
    tx = Transaction()
    had_updater = '/etc/systemd/system/crdn-updater.service' in state['files']
    state['updater_previous_files'] = dict(state['files'])
    state['updater_staging'] = True
    save(STATE, state)
    try:
        for name, data in updater_resources(args.enable_rc_updates).items():
            path = Path(name)
            if path.exists() and name not in state['files']:
                raise ValueError('Неучтённый ресурс веб-обновления: ' + name)
            tx.write(path, data, 0o600 if path.parent == META else 0o644)
            state['files'][name] = digest(data)
        # Parent directories are executable/readable but never writable by the web user.
        for folder in (UPDATER_CODE, UPDATER_CODE / 'ops'):
            folder.chmod(0o755)
        state['updater_staging'] = False
        save(STATE, state)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'crdn-updater.service')
        run('systemctl', 'restart', 'crdn-updater.service')
        activate(content, state)
        state.pop('updater_previous_files', None); state.pop('updater_staging', None)
        save(STATE, state)
        tx.commit()
    except BaseException:
        # activate handles its own application rollback. Preserve manifests of
        # candidates for safe removal, restore only the resources we wrote.
        if previous.get('active') and str((ROOT / 'current').resolve()) != previous['active']:
            switch(previous['active'])
            run('systemctl', 'restart', UNIT)
            health(previous['port'])
        tx.rollback()
        manifests = state.get('releases', {})
        state.clear(); state.update(previous); state['releases'] = manifests
        save(STATE, state)
        run('systemctl', 'daemon-reload')
        if had_updater: run('systemctl', 'restart', 'crdn-updater.service', check=False)
        else: run('systemctl', 'disable', '--now', 'crdn-updater.service', check=False)
        raise


def installation_result(domain, new_account):
    record = json.loads((DATA / 'owner.json').read_text())
    standard = new_account and record.get('default') is True and record.get('username') == 'admin'
    print('\n' + '=' * 48)
    print('Установка CRDN завершена.\nОткройте панель в браузере:')
    print('https://' + domain)
    if standard:
        print('Логин: admin\nПароль: admin\nПосле входа рекомендуем изменить данные учётной записи.')
    else:
        print('Используйте действующие данные входа. Пароль не изменён и не выводится.')
    print('=' * 48)


def update_result(state):
    print('\nОбновление CRDN завершено')
    print('Установленная версия: ' + state['version'])
    print('Адрес панели: https://' + state['domain'])
    print('Данные входа и настройки сохранены.')


def install(args, content):
    domain = (args.domain or input('Введите домен или поддомен для панели, без https://: ')).strip().lower()
    while True:
        server_name = input('Название сервера (до 24 символов, включая пробелы): ')
        if server_name.strip() and len(server_name) <= 24 and not any(unicodedata.category(c).startswith('C') for c in server_name):
            server_name = server_name.strip(); break
        print('Введите от 1 до 24 символов без управляющих знаков. Название не обрезается.')
    new_account = not (DATA / 'owner.json').exists()
    print('Проверка сервера, домена и веб-сервера…', flush=True)
    # DNS ownership first; fresh hosts cannot yet accept port 80/443 connections.
    web, fresh = survey(domain)
    check_dns(domain)
    port = free_port()
    if any(p.exists() for p in (ROOT, STATE, Path('/etc/systemd/system/' + UNIT))):
        raise ValueError('Найдены ресурсы CRDN, не зарегистрированные установщиком. Требуется ручная проверка.')
    try:
        pwd.getpwnam('crdn-managed')
    except KeyError:
        pass
    else:
        account = pwd.getpwnam('crdn-managed')
        if not (META / 'account.json').exists() or json.loads((META / 'account.json').read_text()) != {'uid': account.pw_uid}:
            raise ValueError('Системная учётная запись crdn-managed уже существует без записи установки. Требуется ручная проверка.')
    for path in (Path('/etc/crdn-managed/acme'), Path('/var/lib/crdn-managed-acme'), Path('/etc/systemd/system/crdn-managed-renew.service'), Path('/usr/local/lib/crdn-managed')):
        if path.exists():
            raise ValueError('Untracked lifecycle resource: ' + str(path))
    download(STAGING if args.acme_staging else PRODUCTION)
    packages = (['caddy'] if fresh else []) + (['certbot'] if web != 'caddy' else [])
    META.mkdir(mode=0o700, exist_ok=True)
    planned = dict(domain=domain, port=port, web=web, files={}, phase='preparing', staging=args.acme_staging)
    save(STATE, planned)
    if packages:
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', *packages)
    try:
        pwd.getpwnam('crdn-managed')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', str(DATA), '--no-create-home', '--shell', '/usr/sbin/nologin', 'crdn-managed')
    save(META / 'account.json', {'uid': pwd.getpwnam('crdn-managed').pw_uid})
    state = dict(domain=domain, port=port, web=web, files={}, phase='installing', staging=args.acme_staging, version=getattr(args,'release',None))
    save(STATE, state)
    DATA.mkdir(mode=0o700, exist_ok=True)
    service_account = pwd.getpwnam('crdn-managed')
    os.chown(DATA, service_account.pw_uid, service_account.pw_gid)
    if not (DATA / 'settings.json').exists():
        save(DATA / 'settings.json', {'server_name': server_name})
        os.chown(DATA / 'settings.json', service_account.pw_uid, service_account.pw_gid)
    tx = Transaction()
    try:
        def write(path, text):
            tx.write(path, text.encode())
            state['files'][str(path)] = digest(text.encode())
            save(STATE, state)
        print('Настройка службы и HTTPS…', flush=True)
        if web == 'caddy':
            main = Path('/etc/caddy/Caddyfile')
            original = main.read_text()
            if 'import /etc/caddy/crdn-managed/*.caddy' not in original:
                write(main, original + '\nimport /etc/caddy/crdn-managed/*.caddy\n')
        path = {'caddy': '/etc/caddy/crdn-managed/crdn.caddy', 'nginx': '/etc/nginx/conf.d/crdn-managed.conf', 'apache2': '/etc/apache2/sites-enabled/crdn-managed.conf'}[web]
        if Path(path).exists():
            raise ValueError('Файл конфигурации сайта уже занят.')
        write(path, site(web, domain, port, False, args.acme_staging))
        if fresh:
            validate(web)
            run('systemctl', 'enable', '--now', web)
        reload(web)
        if web != 'caddy':
            Path('/var/lib/crdn-managed-acme/.well-known/acme-challenge').mkdir(parents=True, exist_ok=True)
            acme = META / 'acme'
            command = ['certbot', 'certonly', '--non-interactive', '--agree-tos', '--register-unsafely-without-email', '--webroot', '-w', '/var/lib/crdn-managed-acme', '--cert-name', 'crdn', '-d', domain,
                       '--config-dir', str(acme / 'config'), '--work-dir', str(acme / 'work'), '--logs-dir', str(acme / 'logs'), '--server', STAGING if args.acme_staging else PRODUCTION]
            run(*command)
            write(path, site(web, domain, port, True))
            reload(web)
            renew = f'''[Unit]
Description=Renew CRDN certificate
[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --config-dir {acme}/config --work-dir {acme}/work --logs-dir {acme}/logs --deploy-hook "/usr/local/lib/crdn-managed/renew-hook"
'''
            write('/usr/local/lib/crdn-managed/renew-hook', '#!/bin/sh\nset -eu\n' + {'nginx': '/usr/sbin/nginx -t', 'apache2': '/usr/sbin/apache2ctl configtest'}[web] + '\n/bin/systemctl reload ' + web + '\n')
            Path('/usr/local/lib/crdn-managed/renew-hook').chmod(0o755)
            write('/etc/systemd/system/crdn-managed-renew.service', renew)
            write('/etc/systemd/system/crdn-managed-renew.timer', '[Unit]\nDescription=CRDN certificate renewal\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=3600\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
        write('/etc/systemd/system/' + UNIT, unit(domain, port))
        for resource, data in updater_resources(args.enable_rc_updates).items():
            if Path(resource).exists(): raise ValueError('Ресурс веб-обновления уже занят: ' + resource)
            write(resource, data.decode())
        for folder in (UPDATER_CODE, UPDATER_CODE / 'ops'):
            folder.chmod(0o755)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'crdn-updater.service')
        run('systemctl', 'daemon-reload')
        ROOT.mkdir(exist_ok=True)
        activate(content, state)
        run('systemctl', 'enable', UNIT)
        if web != 'caddy':
            run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer')
        print('Проверка результата установки и HTTPS…', flush=True)
        # Verify HTTPS, allowing staging's intentionally untrusted chain only in explicit tests.
        context = ssl._create_unverified_context() if args.acme_staging else ssl.create_default_context()
        for attempt in range(30):
            try:
                with urlopen('https://' + domain + '/login', context=context, timeout=5) as response:
                    if b'login-form' not in response.read():
                        raise ValueError('По публичному HTTPS-адресу не получена страница входа CRDN.')
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(2)
        state['backups'] = tx.entries
        state['phase'] = 'installed'
        save(STATE, state)
        tx.commit()
    except BaseException:
        run('systemctl', 'disable', '--now', 'crdn-updater.service', check=False)
        run('systemctl', 'disable', '--now', UNIT, check=False)
        run('systemctl', 'disable', '--now', 'crdn-managed-renew.timer', check=False)
        state['files'] = {}
        save(STATE, state)
        tx.rollback()
        run('systemctl', 'daemon-reload')
        reload(web)
        # Keep tracked data/releases/account for explicit recovery/removal.
        raise
    if args.acme_staging: print('Внимание: тестовый ACME staging, сертификат недоверенный.')
    installation_result(domain, new_account)


def check_dns(domain):
    # Check every DNS family; no assumption that a public echo IP proves ownership.
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        raise ValueError('Введите домен или поддомен без https:// и пути.')
    addresses = {str(ipaddress.ip_address(a[4][0])) for a in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    local = {str(ipaddress.ip_address(a['local'])) for dev in json.loads(run('ip', '-j', 'address')) for a in dev['addr_info']}
    if not addresses or not addresses <= local or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Все DNS-записи A/AAAA должны указывать на публичные адреса этого сервера. NAT/CDN требуют ручной настройки.')


def uninstall(state):
    owned_check(state)
    allowed = {str(ROOT / 'current')}
    for folder, files in state.get('releases', {}).items():
        allowed.update(str(Path(folder) / name) for name in files)
        for name, expected in files.items():
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != expected:
                raise ValueError('Release file changed; manual reconciliation required: ' + str(path))
    for path in ROOT.rglob('*') if ROOT.exists() else []:
        if (path.is_file() or path.is_symlink()) and str(path) not in allowed:
            raise ValueError('Untracked release resource preserved: ' + str(path))
    if DATA.is_symlink() or META.is_symlink() or ROOT.is_symlink():
        raise ValueError('Refusing symlinked installation data/configuration')
    for folder in (UPDATER_CODE, UPDATER_DATA, UPDATER_RUN):
        if folder.is_symlink(): raise ValueError('Вспомогательный каталог заменён ссылкой; требуется ручная проверка.')
    if UPDATER_CODE.exists():
        for path in UPDATER_CODE.rglob('*'):
            if path.is_symlink() or (path.is_file() and str(path) not in state['files']):
                raise ValueError('Неучтённый файл веб-обновления сохранён: ' + str(path))
    account = None
    if (META / 'account.json').exists():
        account = pwd.getpwnam('crdn-managed')
        if json.loads((META / 'account.json').read_text()) != {'uid': account.pw_uid} or account.pw_dir != str(DATA) or account.pw_shell != '/usr/sbin/nologin':
            raise ValueError('Service account changed; manual reconciliation required')
        group = grp.getgrnam('crdn-managed')
        if group.gr_gid != account.pw_gid or set(group.gr_mem) - {'crdn-managed'} or any(u.pw_gid == group.gr_gid and u.pw_name != 'crdn-managed' for u in pwd.getpwall()):
            raise ValueError('Service group is shared; manual reconciliation required')
    run('systemctl', 'disable', '--now', 'crdn-updater.service', check=False)
    run('systemctl', 'stop', 'crdn-managed-update.service', check=False)
    print('Удаление CRDN, конфигурации и всех его пользовательских данных, включая учётную запись…', flush=True)
    if state['phase'] != 'preparing':
        validate(state['web'])
    previous_phase = state['phase']
    state['phase'] = 'removing'
    save(STATE, state)
    tx = Transaction()
    try:
        run('systemctl', 'disable', '--now', UNIT, check=False)
        if run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value', check=False).strip() not in ('', '0'):
            raise RuntimeError('Служба CRDN ещё работает. Удаление остановлено.')
        run('systemctl', 'disable', '--now', 'crdn-managed-renew.timer', check=False)
        for name in state['files']:
            if name == '/etc/caddy/Caddyfile':
                backups = [e for e in state.get('backups', []) if e['path'] == name]
                if backups:
                    tx.write(name, bytes.fromhex(backups[0]['before']) if backups[0]['before'] is not None else None, backups[0]['mode'])
            else:
                tx.write(name, None)
        if previous_phase != 'preparing':
            reload(state['web'])
        run('systemctl', 'daemon-reload')
        state['phase'] = 'removed'
        state['files'] = {}
        tx.write(STATE, (json.dumps(state, indent=2) + '\n').encode(), 0o600)
        tx.commit()
    except BaseException:
        tx.rollback()
        reload(state['web'])
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', UNIT, check=False)
        if '/etc/systemd/system/crdn-managed-renew.timer' in state['files']:
            run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer', check=False)
        state['phase'] = 'installed'
        save(STATE, state)
        raise
    if ROOT.exists(): shutil.rmtree(ROOT)
    # Isolated Certbot config belongs exclusively to this installation. Shared Caddy storage is preserved.
    shutil.rmtree(META / 'acme', ignore_errors=True)
    if ACME_WEBROOT.exists(): shutil.rmtree(ACME_WEBROOT)
    for folder in (HELPER_DIR, CADDY_SITE_DIR):
        if folder.exists():
            try:
                folder.rmdir()
            except OSError:
                pass
    for folder in (UPDATER_CODE, UPDATER_DATA, UPDATER_RUN):
        if folder.exists(): shutil.rmtree(folder)
    if DATA.exists(): shutil.rmtree(DATA)
    if account is not None:
        run('userdel', 'crdn-managed')
        try: grp.getgrnam('crdn-managed')
        except KeyError: pass
        else: run('groupdel', 'crdn-managed')
    # Includes account ownership marker, release manifests and internal backups;
    # external backups and shared Caddy certificate storage are outside META.
    if META.exists(): shutil.rmtree(META)
    print('Проверка результата удаления…', flush=True)
    if any(p.exists() for p in (ROOT, DATA, META)):
        raise RuntimeError('Удаление завершено не полностью; остались файлы CRDN.')
    print('CRDN удалён. Приложение и его учётные данные удалены')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('install', 'update', 'uninstall', 'recover'))
    parser.add_argument('--domain')
    parser.add_argument('--enable-rc-updates', action='store_true', help='Explicitly opt into the RC web-update test channel')
    parser.add_argument('--release', help='Explicit GitHub Release tag; permits this exact Pre-release only')
    parser.add_argument('--recover', action='store_true', help='Recover interrupted operation before retry')
    parser.add_argument('--test-archive', help='Explicit local runtime fixture; never selected automatically')
    parser.add_argument('--test-sha256')
    parser.add_argument('--acme-staging', action='store_true')
    args = parser.parse_args()
    if args.release and args.test_archive:
        parser.error('--release cannot be combined with --test-archive')
    if args.recover:
        args.operation = 'recover'
    print('Проверка сервера…', flush=True)
    platform_check()
    with open('/run/lock/crdn-managed.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if JOURNAL.exists():
            if args.operation != 'recover':
                raise ValueError('Изменение файлов прервано. Сначала выполните install.sh --recover из полного комплекта выпуска.')
            tx = Transaction()
            tx.entries = json.loads(JOURNAL.read_text())
            completed = json.loads(STATE.read_text()) if STATE.exists() else None
            if completed and completed['phase'] == 'installed' and not completed.get('pending') and not completed.get('updater_staging'):
                owned_check(completed)
                tx.commit()
            else:
                tx.rollback()
                if completed and 'updater_previous_files' in completed:
                    completed['files'] = completed.pop('updater_previous_files')
                    completed.pop('updater_staging', None)
                    save(STATE, completed)
            if STATE.exists():
                recovered = json.loads(STATE.read_text())
                if recovered['phase'] == 'removing':
                    recovered['phase'] = 'installed'
                    run('systemctl', 'daemon-reload')
                    run('systemctl', 'enable', '--now', UNIT)
                    if recovered['web'] != 'caddy' and '/etc/systemd/system/crdn-managed-renew.timer' in recovered['files']:
                        run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer')
                elif recovered['phase'] != 'installed':
                    recovered['files'] = {}
                save(STATE, recovered)
        state = json.loads(STATE.read_text()) if STATE.exists() else None
        if args.operation == 'recover':
            if state and state.get('pending'):
                old = state['pending']['old']
                if old:
                    switch(old)
                    run('systemctl', 'restart', UNIT)
                    health(state['port'])
                else:
                    run('systemctl', 'stop', UNIT, check=False)
                    (ROOT / 'current').unlink(missing_ok=True)
                state['active'] = old
                state['version'] = state['pending'].get('version', state.get('version'))
                state.pop('pending')
                save(STATE, state)
            if state:
                state.pop('updater_previous_files', None); state.pop('updater_staging', None)
                save(STATE, state)
                run('systemctl', 'daemon-reload')
                if '/etc/systemd/system/crdn-updater.service' in state['files']:
                    run('systemctl', 'restart', 'crdn-updater.service')
                else:
                    run('systemctl', 'disable', '--now', 'crdn-updater.service', check=False)
                if state['phase'] != 'preparing':
                    reload(state['web'])
            print('Recovery finished; incomplete installations require removal before reinstalling.')
            return
        if state and state.get('pending'):
            raise ValueError('Переключение версии прервано. Требуется install.sh --recover из полного комплекта выпуска.')
        if args.operation == 'uninstall':
            if state:
                uninstall(state)
            else:
                print('CRDN не установлен. Удалять нечего.')
            return
        if state:
            if state['phase'] != 'installed':
                raise ValueError('Найдена незавершённая установка. Перед повтором требуется восстановление и удаление её ресурсов.')
            owned_check(state)
            if args.operation == 'install':
                if args.domain and args.domain != state['domain']:
                    raise ValueError('У существующей установки другой домен.')
                print('CRDN уже установлен. Адрес панели: https://' + state['domain'])
                return
            run('systemctl', 'is-active', UNIT)
            content = source(args)
            managed_update(content, state, args)
            state['version'] = args.release
            save(STATE, state)
            update_result(state)
        elif args.operation == 'update':
            raise ValueError('CRDN не установлен. Сначала выполните установку.')
        else:
            install(args, source(args))


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Операция не завершена. Успех не подтверждён.', file=sys.stderr)
        if JOURNAL.exists(): print('Есть журнал незавершённой операции; требуется восстановление перед повтором.', file=sys.stderr)
        elif STATE.exists():
            try: saved = json.loads(STATE.read_text())
            except (OSError, ValueError): saved = {}
            if saved.get('pending'): print('Переключение версии не завершено; требуется восстановление.', file=sys.stderr)
            elif saved.get('phase') == 'installed': print('Состояние установки сохранено. Завершение операции не подтверждено; данные входа не сбрасывались.', file=sys.stderr)
            elif saved.get('phase') == 'removed': print('Служба отключена; удаление ресурсов не завершено. Повторите удаление.', file=sys.stderr)
            else: print('Остались ресурсы незавершённой установки; их состояние сохранено для восстановления/удаления.', file=sys.stderr)
        else: print('Активная установка этой операцией не подтверждена.', file=sys.stderr)
        raise SystemExit('Причина: ' + str(error))
