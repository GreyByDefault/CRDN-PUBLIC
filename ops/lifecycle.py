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
import uuid
import base64
import importlib.util
import marshal

# All entry points, including historical ACME hook commands without -B, import
# package.py below. Suppress new cache writes before that first local import.
sys.dont_write_bytecode = True
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
HOST_SCHEMA = 1
ACME_HOME = Path('/root/.acme.sh')
ROOT_CRON = Path('/var/spool/cron/crontabs/root')
SYSTEMD_DIR = Path('/etc/systemd/system')
PROXY_RUN = Path('/run/crdn-managed-proxy')
POLICY_RC = Path('/usr/sbin/policy-rc.d')
SITE_PATHS = {'caddy': '/etc/caddy/crdn-managed/crdn.caddy', 'nginx': '/etc/nginx/conf.d/crdn-managed.conf',
              'apache2': '/etc/apache2/sites-enabled/crdn-managed.conf'}


def run(*args, check=True, env=None):
    environment = dict(os.environ, LC_ALL='C')
    environment.update(env or {})
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=environment)
    if check and result.returncode:
        # Validator diagnostics may quote a foreign config line with credentials.
        raise RuntimeError(f'{Path(args[0]).name}: системная команда не выполнена (код {result.returncode}); проверьте конфигурацию и журнал службы.')
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

    def write(self, path, data, mode=0o644, expected_hash=None):
        path = Path(path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Not a regular file: ' + str(path))
        old = path.read_bytes() if path.exists() else None
        if expected_hash is not None and (old is None or digest(old) != expected_hash):
            raise ValueError('Owned resource changed before transaction: ' + str(path))
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
    if web == 'isolated':
        run('/usr/sbin/nginx', '-t', '-c', str(META / 'nginx.conf'), '-p', str(PROXY_RUN) + '/')
        return
    if web == 'caddy':
        run('caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
    elif web == 'nginx':
        run('nginx', '-t')
    else:
        run('apache2ctl', 'configtest')


def reload(web):
    validate(web)
    run('systemctl', 'reload', 'crdn-managed-proxy' if web == 'isolated' else web)


def public_origin(state):
    port = state.get('https_port', 443)
    return 'https://' + state['domain'] + (':' + str(port) if port != 443 else '')


def trusted_file(path):
    """Only inspect metadata. In particular, never read a private key."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Ожидался обычный root-owned файл: ' + str(path))
    for p in (path, *path.parents):
        s = p.stat()
        # Debian's root crontab lives below root-owned sticky 1730 crontabs/.
        sticky_directory = p.is_dir() and bool(s.st_mode & 0o1000)
        if s.st_uid != 0 or (s.st_mode & 0o022 and not sticky_directory):
            raise ValueError('Небезопасные права ресурса: ' + str(path))


def ipv6_enabled():
    path = Path('/proc/sys/net/ipv6/conf/all/disable_ipv6')
    return socket.has_ipv6 and path.exists() and path.read_text().strip() == '0'


def public_ipv6():
    path = Path('/proc/net/if_inet6')
    return path.exists() and any(ipaddress.IPv6Address(int(line.split()[0], 16)).is_global
                                 for line in path.read_text().splitlines())


def quiet_command(args, timeout=180, allowed=(0,)):
    """Client output can contain account URLs, signed tokens and hooks."""
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill only our process group, including an HTTP-01 standalone child.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError('Превышено время операции ACME; состояние сохранено для восстановления.') from None
        code = process.returncode
    if code not in allowed:
        operation = next((a for a in args[1:] if a in ('--issue','--renew','--install-cert','certonly','verify','x509')), 'operation')
        raise RuntimeError(f'{Path(args[0]).name} {operation}: операция не завершена; чужие службы не останавливались.')
    return code


def acme_settings(path):
    """Parse literal assignments, never source a shell config in the installer."""
    trusted_file(path)
    result = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        match = re.fullmatch(r'([A-Za-z_][A-Za-z_0-9]*)=(.*)', line)
        if not match or '$(' in match[2] or '`' in match[2]:
            raise ValueError('Неподдерживаемые исполняемые выражения в ACME config.')
        if match:
            try: value = shlex.split(match[2], comments=True)
            except ValueError: continue
            if len(value) <= 1:
                result[match[1]] = value[0] if value else ''
    return result


def scheduled_acme_jobs():
    timers = run('systemctl', 'list-timers', '--all', '--no-legend', check=False).lower()
    pattern = r'acme|certbot|dehydrated|lego'
    if re.search(pattern, timers): return True
    paths = {ROOT_CRON, *Path('/etc/cron.d').glob('*'), *Path('/var/spool/cron/crontabs').glob('*')}
    for path in paths:
        if path.is_file() and re.search(r'(?im)^[^#\n]*(?:' + pattern + ')', path.read_text()):
            return True
    return False


def certificate_plan(domain, standalone, staging=False):
    """Recognize a small, verifiable root scheduler contract, not just a binary."""
    home = ACME_HOME
    client = home / 'acme.sh'
    if client.exists():
        trusted_file(client)
        settings = acme_settings(home / 'account.conf')
        if settings.get('DEFAULT_ACME_SERVER') != PRODUCTION:
            raise ValueError('Не подтверждён существующий аккаунт Let’s Encrypt acme.sh.')
        account = home / 'ca/acme-v02.api.letsencrypt.org/directory'
        for name in ('account.json', 'account.key'):
            trusted_file(account / name)  # metadata only, including for account.key
        if any(settings.get(k) for k in ('CERT_HOME', 'LE_CONFIG_HOME', 'LE_WORKING_DIR', 'ACME_PACKAGED', 'CA_HOME', 'ACCOUNT_KEY_PATH', 'ACCOUNT_JSON_PATH', 'CA_CONF')) or any(k.startswith('Le_') for k in settings):
            raise ValueError('Нестандартное хранилище acme.sh: автоматическое подключение не поддерживается.')
        cron = ROOT_CRON
        jobs = []
        if cron.exists(): trusted_file(cron)
        for line in (cron.read_text().splitlines() if cron.exists() else []):
            if line.lstrip().startswith('#') or 'acme.sh' not in line: continue
            parts = shlex.split(line)
            if len(parts) < 7: continue
            tail = parts[5:]
            # Recognize the installer-created all-domain cron, optionally redirected.
            expected = [str(client), '--cron', '--home', str(home)]
            if tail[:4] == expected and tail[4:] in ([], ['>', '/dev/null'], ['>', '/dev/null', '2>&1']):
                jobs.append(' '.join(parts[:5]))
            else:
                raise ValueError('Неизвестное расписание acme.sh; требуется ручная проверка.')
        if len(jobs) > 1:
            raise ValueError('Не подтверждено единственное root cron продления всех сертификатов acme.sh.')
        timers = run('systemctl', 'list-timers', '--all', '--no-legend', check=False).lower()
        if 'acme' in timers:
            raise ValueError('Обнаружен дополнительный ACME timer; требуется ручная проверка расписания.')
        # A distinct primary domain also gives a distinct per-domain hook.
        if any((home / (domain + suffix)).exists() for suffix in ('', '_ecc')):
            raise ValueError('Сертификат этого домена уже существует в acme.sh; он не будет изменён.')
        return dict(client='acme.sh', executable=str(client), home=str(home), scheduler='shared-cron' if jobs else 'owned-timer',
                    schedule=jobs[0] if jobs else None, domain_dir=str(home / (domain + '_ecc')), standalone=standalone,
                    ca=STAGING if staging else PRODUCTION)
    certbot = shutil.which('certbot')
    if certbot:
        if certbot != '/usr/bin/certbot':
            raise ValueError('Поддерживается пакетный /usr/bin/certbot; неизвестная установка клиента не изменяется.')
        trusted_file(certbot)
        # Shared renewal hooks can restart unrelated services for any certificate.
        hooks = Path('/etc/letsencrypt/renewal-hooks')
        global_hooks = hooks.exists() and any(p.is_file() and os.access(p, os.X_OK) for p in hooks.rglob('*'))
        cli = Path('/etc/letsencrypt/cli.ini')
        if cli.exists():
            trusted_file(cli)
            global_hooks |= any(line.strip() and not line.lstrip().startswith(('#', ';')) for line in cli.read_text().splitlines())
        command = run('systemctl', 'show', 'certbot.service', '-p', 'ExecStart', '--value', check=False)
        scheduled = run('systemctl', 'is-enabled', 'certbot.timer', check=False).strip() == 'enabled'
        if not global_hooks and scheduled and re.search(r'argv\[\]=/usr/bin/certbot (?:-q |--quiet )?renew ;', command):
            return dict(client='certbot', executable=certbot, scheduler='shared-timer',
                        config='/etc/letsencrypt', name='crdn-managed-' + domain,
                        standalone=standalone, ca=STAGING if staging else PRODUCTION)
        # Reuse the installed executable with a distinct config; global hooks and
        # unknown schedulers must never receive this new lineage.
        return dict(client='certbot', executable=certbot, scheduler='owned-timer',
                    config=str(META / 'acme/config'), name='crdn', standalone=standalone,
                    ca=STAGING if staging else PRODUCTION)
    # Refuse unknown ACME jobs rather than installing a competing scheduler.
    if scheduled_acme_jobs():
        raise ValueError('Обнаружено неподдерживаемое ACME задание; требуется ручная проверка.')
    return dict(client='certbot', executable='/usr/bin/certbot', scheduler='owned-timer',
                config=str(META / 'acme/config'), name='crdn', install=True,
                standalone=standalone, ca=STAGING if staging else PRODUCTION)


def certificate_paths(state):
    plan = state.get('certificate')
    if plan and plan['client'] == 'certbot':
        live = Path(plan['config']) / 'live' / plan['name']
    else:
        live = META / 'acme/config/live/crdn'
    return str(live / 'fullchain.pem'), str(live / 'privkey.pem')


def reserve_https_port():
    """Hold both wildcard families; release only immediately before nginx start.

    Bind remains authoritative: another process can win after release, in which
    case systemd start fails and we roll back, never stop that process.
    """
    ephemeral = Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split()
    low, high = map(int, ephemeral)
    reserved = set()
    for item in Path('/proc/sys/net/ipv4/ip_local_reserved_ports').read_text().strip().split(','):
        if not item: continue
        bounds = list(map(int, item.split('-')))
        reserved.update(range(bounds[0], bounds[-1] + 1))
    preferred = [8443, 9443, 10443, 11443, 12443]
    for port in [*preferred, *(p for p in range(1024, 65536) if p not in preferred)]:
        if port in reserved or low <= port <= high: continue
        sockets = []
        try:
            for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
                if family == socket.AF_INET6 and not ipv6_enabled(): continue
                sock = socket.socket(family, socket.SOCK_STREAM)
                sockets.append(sock)
                if family == socket.AF_INET6: sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind((address, port)); sock.listen(1)
            return port, sockets
        except OSError:
            for sock in sockets: sock.close()
    raise ValueError('Не найден свободный HTTPS TCP-порт вне системных резервов.')


def http_port_available():
    sockets = []
    try:
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            if family == socket.AF_INET6 and not ipv6_enabled(): continue
            sock = socket.socket(family, socket.SOCK_STREAM); sockets.append(sock)
            if family == socket.AF_INET6: sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((address, 80))
        return True
    except OSError:
        return False
    finally:
        for sock in sockets: sock.close()


def certificate_command(state, renew=False):
    plan = state['certificate']; domain = state['domain']
    hook = '/usr/bin/python3 ' + str(UPDATER_CODE / 'ops/lifecycle.py') + ' certificate-hook'
    if plan['client'] == 'acme.sh':
        return [plan['executable'], '--renew' if renew else '--issue', '--ecc', '-d', domain,
                '--server', plan['ca'], *(['--standalone', '--httpport', '80', *(['--listen-v6'] if ipv6_enabled() else [])] if plan['standalone'] else ['--webroot', str(ACME_WEBROOT)])]
    args = [plan['executable'], 'certonly', '--config', '/dev/null', '--non-interactive', '--agree-tos', '--register-unsafely-without-email',
            '--cert-name', plan['name'], '-d', domain, '--server', plan['ca'], '--deploy-hook', hook,
            *(['--standalone', '--preferred-challenges', 'http'] if plan['standalone'] else ['--webroot', '-w', str(ACME_WEBROOT)]),
            '--config-dir', plan['config']]
    if plan['scheduler'] == 'owned-timer':
        args += ['--work-dir', str(META / 'acme/work'), '--logs-dir', str(META / 'acme/logs')]
    return args


def obtain_certificate(state, renew=False):
    plan = state['certificate']
    for attempt in range(3):
        if not plan['standalone'] or http_port_available():
            try:
                code = quiet_command(certificate_command(state, renew), timeout=180,
                                     allowed=(0, 2) if renew and plan['client'] == 'acme.sh' else (0,))
                if code == 2: return False  # acme.sh: certificate not due yet
                break
            except RuntimeError as error:
                # Do not blindly retry CA/format failures. Retry only a port race.
                if not plan['standalone'] or http_port_available():
                    header = Path(plan.get('home', '/nonexistent')) / 'http.header'
                    statuses = re.findall(r'(?m)^HTTP/\S+ (\d{3})', header.read_text()) if header.is_file() else []
                    if statuses:
                        raise RuntimeError(str(error) + '; последний HTTP-статус ACME: ' + statuses[-1]) from None
                    raise
        if attempt == 2:
            raise RuntimeError('TCP 80 занят; ACME отложен до следующего автоматического запуска.')
        time.sleep(10)
    if plan['client'] == 'acme.sh' and not renew:
        cert, key = certificate_paths(state)
        Path(cert).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        quiet_command([plan['executable'], '--install-cert', '--ecc', '-d', state['domain'],
                       '--key-file', key, '--fullchain-file', cert,
                       '--reloadcmd', '/usr/bin/python3 ' + str(UPDATER_CODE / 'ops/lifecycle.py') + ' certificate-hook'])
    return True


def check_certificate(state):
    cert, _ = certificate_paths(state)
    # openssl inspects only the public certificate. nginx itself validates its key.
    quiet_command(['openssl', 'x509', '-in', cert, '-noout', '-checkend', '0'], timeout=10)
    # x509 -checkhost is informational on some OpenSSL releases (exit 0 even
    # on mismatch). verify -verify_hostname gives a failing exit status.
    quiet_command(['openssl', 'verify', '-verify_hostname', state['domain'], '-purpose', 'sslserver',
                   '-untrusted', cert, *(['-trusted', cert, '-partial_chain'] if state.get('staging') else []), cert], timeout=10)


def certificate_hook():
    if not STATE.exists(): return
    state = json.loads(STATE.read_text())
    if state.get('phase') != 'installed': return  # initial install validates before activation
    owned_check(state)
    check_certificate(state)
    reload(state['web'])


def isolated_config(state):
    cert, key = certificate_paths(state)
    listen = f"listen 0.0.0.0:{state['https_port']} ssl;"
    if ipv6_enabled(): listen += f"\n listen [::]:{state['https_port']} ssl ipv6only=on;"
    return f'''user crdn-managed;
pid /run/crdn-managed-proxy/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
 access_log off;
 client_body_temp_path /run/crdn-managed-proxy/body;
 proxy_temp_path /run/crdn-managed-proxy/proxy;
 fastcgi_temp_path /run/crdn-managed-proxy/fastcgi;
 uwsgi_temp_path /run/crdn-managed-proxy/uwsgi;
 scgi_temp_path /run/crdn-managed-proxy/scgi;
 server {{
 {listen}
 server_name {state['domain']};
 ssl_certificate {cert};
 ssl_certificate_key {key};
 ssl_protocols TLSv1.2 TLSv1.3;
 location / {{
 proxy_pass http://127.0.0.1:{state['port']};
 proxy_set_header Host $http_host;
 proxy_set_header X-CRDN-Client-IP $remote_addr;
 }}
 }}
}}
'''


def isolated_unit():
    return f'''[Unit]
Description=CRDN isolated HTTPS proxy
After=network.target
[Service]
Type=simple
RuntimeDirectory=crdn-managed-proxy
ExecStartPre=/usr/sbin/nginx -t -c {META}/nginx.conf -p /run/crdn-managed-proxy/
ExecStart=/usr/sbin/nginx -c {META}/nginx.conf -p /run/crdn-managed-proxy/ -g "daemon off;"
ExecReload=/bin/kill -HUP $MAINPID
KillSignal=SIGQUIT
TimeoutStopSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/run/crdn-managed-proxy
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN CAP_DAC_OVERRIDE
[Install]
WantedBy=multi-user.target
'''


def auxiliary_fail2ban_chains(objects):
    """Recognize the verified sshd action, never exempt a table by its name alone."""
    candidates = [v['chain'] for v in objects if 'chain' in v and v['chain'].get('hook') == 'input'
                  and v['chain'].get('table') == 'f2b-table']
    if not candidates: return []
    if run('systemctl', 'is-active', 'fail2ban', check=False).strip() != 'active':
        raise ValueError('Владелец вспомогательной цепочки Fail2ban не подтверждён.')
    expected = {'table':'f2b-table','table_family':'inet','chain_hook':'input','chain_priority':'-1','port':'ssh','protocol':'tcp'}
    for key, value in expected.items():
        if run('fail2ban-client','get','sshd','action','nftables',key,check=False).strip() != value:
            raise ValueError('Действующий sshd action Fail2ban не соответствует известной схеме.')
    for chain in candidates:
        if any(chain.get(k) != v for k,v in {'family':'inet','name':'f2b-chain','type':'filter','prio':-1,'policy':'accept'}.items()):
            raise ValueError('Неизвестная структура вспомогательной цепочки Fail2ban.')
        sets = [v['set'] for v in objects if v.get('set',{}).get('table') == chain['table']]
        if not any(s.get('name')=='addr-set-sshd' and s.get('type')=='ipv4_addr' for s in sets):
            raise ValueError('Набор адресов Fail2ban sshd не подтверждён.')
        for item in objects:
            rule=item.get('rule',{})
            if rule.get('table') != chain['table'] or rule.get('chain') != chain['name']: continue
            expr=[e for e in rule.get('expr',[]) if 'counter' not in e]
            matches=[{'match':{'op':'==','left':{'payload':{'protocol':'tcp','field':'dport'}},'right':22}},
                     {'match':{'op':'==','left':{'payload':{'protocol':'ip','field':'saddr'}},'right':'@addr-set-sshd'}}]
            if len(expr)!=3 or expr[:2]!=matches or expr[2] != {'reject':{'type':'icmp','expr':'port-unreachable'}}:
                raise ValueError('Неизвестное правило Fail2ban; запреты не обходятся.')
    return candidates


def firewall_plan(ports):
    """Unknown rule ordering is a refusal, never a reason to flush the firewall."""
    ufw = shutil.which('ufw')
    firewalld = run('systemctl', 'is-active', 'firewalld', check=False).strip() == 'active'
    status = run(ufw, 'status', check=False) if ufw else ''
    rules = json.loads(run('nft', '-j', 'list', 'ruleset'))['nftables'] if shutil.which('nft') else []
    auxiliary = auxiliary_fail2ban_chains(rules)
    chains = [v['chain'] for v in rules if 'chain' in v and v['chain'].get('hook') == 'input' and v['chain'] not in auxiliary]
    if 'Status: active' in status:
        if firewalld: raise ValueError('Одновременно активны UFW и firewalld.')
        if any(c.get('table')!='filter' or c.get('name')!='INPUT' or c.get('family') not in ('ip','ip6') for c in chains):
            raise ValueError('Неизвестная INPUT-цепочка рядом с UFW; firewall не изменён.')
        result = []
        for port in ports:
            rows = [line for line in status.splitlines() if re.search(rf'(?<!\d){port}(?:/tcp)?(?!\d)', line)]
            if rows:
                if not all(re.fullmatch(rf'\s*{port}/tcp(?: \(v6\))?\s+ALLOW\s+Anywhere(?: \(v6\))?\s*(?:#.*)?', r) for r in rows):
                    raise ValueError('UFW: неоднозначное ограничение порта ' + str(port))
                # Need both families when IPv6 is enabled; avoid weakening a v4-only policy.
                if public_ipv6() and not any('(v6)' in r for r in rows):
                    raise ValueError('UFW: не подтверждено разрешение IPv6 для порта ' + str(port))
                continue
            result.append(dict(backend='ufw', port=port, marker='crdn-managed-' + uuid.uuid4().hex))
        return result
    if firewalld:
        zone = run('firewall-cmd', '--get-default-zone').strip()
        zones = run('firewall-cmd', '--get-active-zones').splitlines()
        names = [z.strip() for z in zones if z and not z.startswith(' ')]
        if names != [zone] or not re.fullmatch('[a-zA-Z0-9_-]+', zone):
            raise ValueError('firewalld: требуется одна подтверждённая default zone.')
        if run('firewall-cmd', '--get-policies', check=False).strip() or run('firewall-cmd', '--zone=' + zone, '--list-rich-rules').strip():
            raise ValueError('firewalld: policies/rich rules требуют ручной проверки.')
        return [dict(backend='firewalld', port=p, zone=zone, marker='crdn-' + uuid.uuid4().hex[:16]) for p in ports
                if run('firewall-cmd', '--zone=' + zone, '--query-port=' + str(p) + '/tcp', check=False).strip() != 'yes']
    # Recognize plain native filters only; managed chains/policies are not guessed.
    inspected = bool(shutil.which('nft'))
    if inspected:
        if chains:
            if len(chains) == 1 and all(chains[0].get(k) == v for k, v in
                                         {'family':'inet', 'table':'filter', 'name':'input', 'type':'filter', 'prio':0}.items()):
                for item in rules:
                    rule = item.get('rule', {})
                    if rule.get('table') == 'filter' and rule.get('chain') == 'input':
                        if any(set(e) - {'match', 'counter', 'accept'} for e in rule.get('expr', [])):
                            raise ValueError('nftables INPUT содержит неизвестные ограничения; правила сохранены.')
                existing = [v['rule'].get('expr', []) for v in rules if v.get('rule', {}).get('chain') == 'input']
                def allowed_port(port):
                    expected = [{'match': {'op':'==', 'left':{'payload':{'protocol':'tcp','field':'dport'}}, 'right':port}}, {'accept':None}]
                    return any([e for e in expr if 'counter' not in e] == expected for expr in existing)
                return [dict(backend='nft', port=p, marker='crdn-managed-' + uuid.uuid4().hex) for p in ports if not allowed_port(p)]
            # iptables-nft may own the two standard family-specific INPUT chains.
            if not all(c.get('table') == 'filter' and c.get('name') == 'INPUT' and c.get('family') in ('ip', 'ip6') for c in chains):
                raise ValueError('Неизвестная топология nftables INPUT; firewall не изменён.')
    if shutil.which('iptables'):
        families = ['iptables'] + (['ip6tables'] if ipv6_enabled() else [])
        needed = False
        for cmd in families:
            if not shutil.which(cmd): raise ValueError('Не подтверждён firewall обеих IP-семейств.')
            for line in run(cmd, '-S', 'INPUT').splitlines():
                if line.startswith('-P INPUT '):
                    needed |= line != '-P INPUT ACCEPT'
                    continue
                if not line.startswith('-A INPUT ') or not line.endswith(' -j ACCEPT'):
                    raise ValueError('iptables INPUT содержит неизвестные ограничения; правила сохранены.')
        return [dict(backend='iptables', families=families, port=p, marker='crdn-managed-' + uuid.uuid4().hex) for p in ports] if needed else []
    if not inspected:
        raise ValueError('Нет nft/iptables для чтения firewall; безопасность автоматического изменения не подтверждена.')
    return []


def firewall_change(rule, add):
    backend = rule['backend']; port = str(rule['port']); marker = rule['marker']
    if backend == 'ufw':
        status = run('ufw', 'status', 'numbered')
        matches = [line for line in status.splitlines() if marker in line]
        for line in matches:
            if not re.search(rf'\b{port}/tcp\b', line) or not re.search(r'\bALLOW IN\b', line):
                raise ValueError('Правило UFW CRDN изменено; требуется ручная проверка.')
        if add:
            if not matches:
                # Refuse a concurrently created foreign rule for the selected port.
                if re.search(rf'(?<!\d){port}/tcp\b', status):
                    raise ValueError('Параллельное изменение UFW; порт не будет перехвачен.')
                run('ufw', 'allow', port + '/tcp', 'comment', marker)
        else:
            # Numbered deletion is bound to the exact marker and checked immediately.
            while matches:
                first = matches[0]
                number = re.match(r'\[\s*(\d+)\]', first.strip())
                if not number: raise ValueError('Не удалось определить собственное правило UFW.')
                current = run('ufw', 'status', 'numbered')
                if first not in current.splitlines(): raise ValueError('Параллельное изменение UFW.')
                run('ufw', '--force', 'delete', number[1])
                matches = [line for line in run('ufw', 'status', 'numbered').splitlines() if marker in line]
    elif backend == 'firewalld':
        for permanent in (True, False):
            base = ['firewall-cmd', *(['--permanent'] if permanent else []), '--zone=' + rule['zone']]
            for family in (['ipv4', 'ipv6'] if ipv6_enabled() else ['ipv4']):
                # A limited log prefix gives the owned rule a unique identifier.
                rich = f'rule family="{family}" port port="{port}" protocol="tcp" log prefix="{marker}" limit value="1/m" accept'
                lines = run(*base, '--list-rich-rules').splitlines()
                matches = [l for l in lines if marker in l and f'family="{family}"' in l]
                if any(l != rich for l in matches): raise ValueError('Правило firewalld CRDN изменено.')
                if bool(matches) != add:
                    run(*base, ('--add-rich-rule=' if add else '--remove-rich-rule=') + rich)
    elif backend == 'nft':
        rules = json.loads(run('nft', '-j', 'list', 'chain', 'inet', 'filter', 'input'))['nftables']
        matches = [v['rule'] for v in rules if v.get('rule', {}).get('comment') == marker]
        expected = [{'match': {'op': '==', 'left': {'payload': {'protocol': 'tcp', 'field': 'dport'}}, 'right': int(port)}}, {'accept': None}]
        if len(matches) > 1 or any(r.get('expr') != expected for r in matches):
            raise ValueError('Собственное правило nftables изменено; требуется ручная проверка.')
        if add and not matches:
            payload = {'nftables': [{'add': {'rule': dict(family='inet', table='filter', chain='input', expr=expected, comment=marker)}}]}
            result = subprocess.run(['nft', '-j', '-f', '-'], input=json.dumps(payload), text=True, capture_output=True)
            if result.returncode: raise RuntimeError('Не удалось добавить собственное правило nftables.')
        elif not add and matches:
            run('nft', 'delete', 'rule', 'inet', 'filter', 'input', 'handle', str(matches[0]['handle']))
    elif backend == 'iptables':
        tail = ['-p', 'tcp', '-m', 'tcp', '--dport', port, '-m', 'comment', '--comment', marker, '-j', 'ACCEPT']
        for cmd in rule['families']:
            lines = [l for l in run(cmd, '-S', 'INPUT').splitlines() if marker in l]
            expected = ['-A', 'INPUT', *tail]
            if len(lines) > 1 or any(shlex.split(l) != expected for l in lines):
                raise ValueError('Собственное правило iptables изменено; требуется ручная проверка.')
            if bool(lines) != add:
                run(cmd, '-w', '3', '-A' if add else '-D', 'INPUT', *tail)
    else:
        raise ValueError('Неизвестный firewall backend в состоянии установки.')


def firewall_apply(state, add):
    for rule in state.get('firewall', []):
        firewall_change(rule, add)


def firewall_unit():
    return f'''[Unit]
Description=Restore only CRDN firewall permissions
After=ufw.service firewalld.service nftables.service
Before=crdn-managed-proxy.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {UPDATER_CODE}/ops/lifecycle.py firewall-restore
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
'''


def renewal_units():
    return {
        '/etc/systemd/system/crdn-managed-renew.service': f'''[Unit]
Description=Renew only CRDN certificate
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {UPDATER_CODE}/ops/lifecycle.py renew
TimeoutStartSec=620
''',
        '/etc/systemd/system/crdn-managed-renew.timer': '''[Unit]
Description=Retry CRDN certificate renewal without stopping other services
[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
'''}


def install_packages(packages, tx):
    if not packages: return
    # apt maintainer scripts must not start a newly installed nginx on foreign 80/443.
    policy = POLICY_RC
    if policy.exists():
        raise ValueError('Существующий policy-rc.d требует ручной проверки установки пакетов.')
    tx.write(policy, b'#!/bin/sh\nexit 101\n', 0o755)
    try:
        environment = {'NEEDRESTART_MODE': 'l', 'DEBIAN_FRONTEND': 'noninteractive'}
        run('apt-get', 'update', env=environment)
        run('apt-get', 'install', '-y', '--no-install-recommends', *packages, env=environment)
        if 'nginx-core' in packages:
            run('systemctl', 'disable', 'nginx')  # only a newly installed package
    finally:
        # This also journals removal; interrupted installation can recover the policy.
        tx.write(policy, None)


def prepare_certificate(state):
    plan = state['certificate']
    marker = uuid.uuid4().hex
    plan['owner'] = marker
    if plan['client'] == 'acme.sh':
        folder = Path(plan['domain_dir'])
        if folder.exists() or folder.is_symlink():
            raise ValueError('Каталог сертификата уже существует; чужие данные сохранены.')
        plan['marker'] = str(folder / '.crdn-owner')
    else:
        folder = Path(plan['config'])
        if any((folder / part / (plan['name'] + suffix)).exists()
               for part, suffix in (('live', ''), ('archive', ''), ('renewal', '.conf'))):
            raise ValueError('Certbot lineage CRDN уже существует; он не будет изменён.')
        plan['marker'] = str(folder / ('.crdn-owner-' + plan['name']))
    # Persist the claim before creating any external client resource.
    save(STATE, state)
    Path(plan['marker']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic(Path(plan['marker']), marker.encode())
    if not plan['standalone']:
        (ACME_WEBROOT / '.well-known/acme-challenge').mkdir(parents=True, exist_ok=True)


def client_busy(plan):
    executable = plan['executable'].encode()
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            if executable in path.read_bytes().replace(b'\0', b' '): return True
        except (OSError, ProcessLookupError):
            continue
    return False


def cleanup_certificate(state):
    plan = state.get('certificate')
    if not plan or not plan.get('marker'): return
    if client_busy(plan):
        raise ValueError('ACME-клиент занят; очистка отложена. Повторите uninstall после завершения задания.')
    quarantine = Path(plan['quarantine']) if plan.get('quarantine') else None
    if quarantine and quarantine.exists():
        if plan['client'] == 'acme.sh' and Path(plan['domain_dir']).exists():
            raise ValueError('Каталог ACME появился повторно; ресурсы сохранены для ручной проверки.')
        if plan['client'] == 'certbot' and (Path(plan['config']) / 'renewal' / (plan['name'] + '.conf')).exists():
            raise ValueError('Certbot renewal появился повторно; ресурсы сохранены для ручной проверки.')
        marker = quarantine / '.crdn-owner' if plan['client'] == 'acme.sh' else Path(plan['marker'])
    else:
        marker = Path(plan['marker'])
    if not marker.exists():
        # A claim persisted immediately before a crash need not have created files.
        if plan['client'] == 'acme.sh' and Path(plan['domain_dir']).exists():
            folder = Path(plan['domain_dir'])
            if folder.is_symlink() or any(folder.iterdir()):
                raise ValueError('Нет маркера принадлежности каталога acme.sh; очистка остановлена.')
            folder.rmdir()  # interrupted between mkdir and writing the marker
        if plan['client'] == 'certbot' and plan['scheduler'] != 'owned-timer' and (Path(plan['config']) / 'renewal' / (plan['name'] + '.conf')).exists():
            raise ValueError('Нет маркера принадлежности Certbot lineage; очистка остановлена.')
        return
    trusted_file(marker)
    if marker.read_bytes() != plan['owner'].encode():
        raise ValueError('Маркер сертификата изменён; очистка остановлена.')
    if plan['client'] == 'acme.sh':
        folder = quarantine if quarantine and quarantine.exists() else Path(plan['domain_dir'])
        conf = folder / (state['domain'] + '.conf')
        if conf.exists():
            settings = acme_settings(conf)
            if settings.get('Le_Domain', state['domain']) != state['domain']:
                raise ValueError('Домен acme.sh изменён; сертификат сохранён.')
            hook = settings.get('Le_ReloadCmd', '')
            prefix, suffix = '__ACME_BASE64__START_', '__ACME_BASE64__END_'
            if hook.startswith(prefix) and hook.endswith(suffix):
                hook = base64.b64decode(hook[len(prefix):-len(suffix)], validate=True).decode()
            expected = '/usr/bin/python3 ' + str(UPDATER_CODE / 'ops/lifecycle.py') + ' certificate-hook'
            if hook and hook != expected:
                raise ValueError('Hook acme.sh изменён; сертификат сохранён.')
            cert, key = certificate_paths(state)
            if any(settings.get(k) and settings[k] != value for k, value in
                   (('Le_RealFullChainPath', cert), ('Le_RealKeyPath', key))):
                raise ValueError('Пути сертификата acme.sh изменены; очистка остановлена.')
        allowed = {
            '.crdn-owner', *(state['domain'] + ext for ext in ('.key', '.cer', '.conf', '.csr', '.csr.conf')),
            'ca.cer', 'fullchain.cer', 'backup'}
        for path in folder.rglob('*'):
            if path.is_symlink() or path.stat().st_uid != 0 or (path.parent == folder and path.name not in allowed):
                raise ValueError('Неизвестный ресурс в каталоге сертификата; очистка остановлена.')
            if path.parent != folder and (path.parent != folder / 'backup' or path.name not in {'cert.bak', 'key.bak', 'ca.bak', 'fullchain.bak'}):
                raise ValueError('Неизвестная резервная копия сертификата; очистка остановлена.')
        if folder == Path(plan['domain_dir']):
            quarantine = folder.parent / ('.crdn-removing-' + plan['owner'])
            plan['quarantine'] = str(quarantine); save(STATE, state)
            os.rename(folder, quarantine)
            folder = quarantine
        # Hide from renewAll before deleting. A cron that already read config
        # must finish first; new all-domain jobs cannot select the hidden path.
        if client_busy(plan):
            raise ValueError('ACME запущен во время отсоединения; повторите uninstall после его завершения.')
        shutil.rmtree(folder)
        plan.pop('marker', None); save(STATE, state)
    elif plan['scheduler'] != 'owned-timer':
        original_conf = Path(plan['config']) / 'renewal' / (plan['name'] + '.conf')
        conf = quarantine if quarantine and quarantine.exists() else original_conf
        if conf.exists():
            trusted_file(conf)
            text = conf.read_text()
            expected = '/usr/bin/python3 ' + str(UPDATER_CODE / 'ops/lifecycle.py') + ' certificate-hook'
            if not re.search(r'^deploy_hook\s*=\s*' + re.escape(expected) + r'\s*$', text, re.M):
                raise ValueError('Certbot hook изменён; lineage сохранён для ручной проверки.')
        archive = Path(plan['config']) / 'archive' / plan['name']
        live = Path(plan['config']) / 'live' / plan['name']
        for folder in (archive, live):
            if folder.is_symlink(): raise ValueError('Certbot lineage заменён ссылкой.')
            if not folder.exists(): continue
            for path in folder.iterdir():
                if folder == archive:
                    if not re.fullmatch(r'(cert|chain|fullchain|privkey)\d+\.pem', path.name):
                        raise ValueError('Неизвестный файл в archive CRDN; очистка остановлена.')
                    trusted_file(path)  # metadata, never contents of key files
                elif path.name == 'README':
                    trusted_file(path)
                elif path.name in ('cert.pem', 'chain.pem', 'fullchain.pem', 'privkey.pem'):
                    if not path.is_symlink() or path.resolve().parent != archive or not re.fullmatch(r'(cert|chain|fullchain|privkey)\d+\.pem', path.resolve().name):
                        raise ValueError('Certbot live содержит неизвестную ссылку; очистка остановлена.')
                else:
                    raise ValueError('Неизвестный файл в live CRDN; очистка остановлена.')
        # The marker and strict layout also cover interrupted certonly creation.
        if conf.exists() and conf == original_conf:
            quarantine = Path(plan['config']) / ('.crdn-renewal-' + plan['owner'])
            plan['quarantine'] = str(quarantine); save(STATE, state)
            os.rename(conf, quarantine); conf = quarantine
        if client_busy(plan):
            raise ValueError('Certbot занят; отсоединённая lineage сохранена до повторного uninstall.')
        conf.unlink(missing_ok=True)
        for folder in (live, archive):
            if folder.exists(): shutil.rmtree(folder)
        marker.unlink(missing_ok=True)
        plan.pop('marker', None); save(STATE, state)
    # owned-timer storage is inside META and removed by finish_removal.


def install_adaptive(args, content, domain, server_name, web, new_account):
    """One lifecycle, with dedicated resources when a foreign service owns 443."""
    isolated = web == 'isolated'
    plan = certificate_plan(domain, isolated, args.acme_staging)
    if isolated:
        for attempt in range(3):
            if http_port_available(): break
            if attempt == 2:
                raise ValueError('TCP 80 занят: безопасный standalone HTTP-01 сейчас невозможен. Службы и firewall не изменены.')
            time.sleep(10)
    sockets = []
    https_port = 443
    if isolated:
        https_port, sockets = reserve_https_port()
    try:
        # Complete read-only planning before creating resources or apt changes.
        firewall = firewall_plan([https_port, 80])
        if not isolated and firewall:
            raise ValueError('Открытие закрытого общего веб-порта может раскрыть соседние сайты; автоматическое изменение firewall остановлено.')
        state = dict(domain=domain, port=free_port(), https_port=https_port, host_schema=HOST_SCHEMA, mode='isolated' if isolated else 'shared',
                     web=web, certificate=plan, firewall=firewall, files={}, phase='preparing',
                     staging=args.acme_staging, version=getattr(args, 'release', None))
        for path in (META / 'nginx.conf', SYSTEMD_DIR / 'crdn-managed-proxy.service',
                     SYSTEMD_DIR / 'crdn-managed-firewall.service'):
            if path.exists(): raise ValueError('Неучтённый ресурс CRDN: ' + str(path))
        for name in (UNIT, 'crdn-managed-proxy.service', 'crdn-managed-firewall.service',
                     'crdn-managed-renew.service', 'crdn-managed-renew.timer',
                     'crdn-updater.service', 'crdn-managed-update.service'):
            if (SYSTEMD_DIR / (name + '.d')).exists() or run('systemctl', 'cat', name, check=False).strip():
                raise ValueError('Неучтённый unit/drop-in CRDN; существующая служба не изменена: ' + name)
        packages = []
        if isolated and not Path('/usr/sbin/nginx').exists():
            if Path('/etc/nginx').exists() or run('systemctl', 'cat', 'nginx', check=False).strip():
                raise ValueError('Неполная существующая установка Nginx; пакеты и unit не изменены.')
            packages.append('nginx-core')
        if plan.get('install'): packages.append('certbot')
        if not shutil.which('openssl'): packages.append('openssl')
        META.mkdir(mode=0o700, parents=True, exist_ok=True)
        save(STATE, state)
        tx = Transaction()
        def write(path, data, mode=0o644):
            if isinstance(data, str): data = data.encode()
            if Path(path).exists(): raise ValueError('Ресурс CRDN уже существует: ' + str(path))
            tx.write(path, data, mode)
            state['files'][str(path)] = digest(data)
            save(STATE, state)
        try:
            install_packages(packages, tx)
            if isolated: trusted_file('/usr/sbin/nginx')
            try: account = pwd.getpwnam('crdn-managed')
            except KeyError:
                run('useradd', '--system', '--user-group', '--home-dir', str(DATA), '--no-create-home', '--shell', '/usr/sbin/nologin', 'crdn-managed')
                account = pwd.getpwnam('crdn-managed')
            save(META / 'account.json', {'uid': account.pw_uid})
            DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(DATA, account.pw_uid, account.pw_gid)
            if not (DATA / 'settings.json').exists():
                save(DATA / 'settings.json', {'server_name': server_name})
                os.chown(DATA / 'settings.json', account.pw_uid, account.pw_gid)
            state['phase'] = 'installing'; save(STATE, state)
            for name, data in updater_resources(args.enable_rc_updates, getattr(args, 'disable_rc_updates', False)).items():
                write(name, data, 0o600 if Path(name).parent == META else 0o644)
            for folder in (UPDATER_CODE, UPDATER_CODE / 'ops'): folder.chmod(0o755)
            write(SYSTEMD_DIR / UNIT, unit(domain if https_port == 443 else domain + ':' + str(https_port), state['port']))
            if firewall:
                write(SYSTEMD_DIR / 'crdn-managed-firewall.service', firewall_unit())
                firewall_apply(state, True)
            if not isolated:
                path = str(SITE_PATHS[web])
                write(path, site(web, domain, state['port'], False))
                reload(web)
            prepare_certificate(state)
            obtain_certificate(state)
            check_certificate(state)
            if isolated:
                write(META / 'nginx.conf', isolated_config(state))
                write(SYSTEMD_DIR / 'crdn-managed-proxy.service', isolated_unit())
            else:
                data = site(web, domain, state['port'], True, certificates=certificate_paths(state)).encode()
                tx.write(path, data); state['files'][path] = digest(data); save(STATE, state)
                reload(web)
            if plan['scheduler'] == 'owned-timer':
                for name, data in renewal_units().items(): write(name, data)
            write(META / 'address.txt', public_origin(state) + '\n')
            run('systemctl', 'daemon-reload')
            ROOT.mkdir(exist_ok=True)
            activate(content, state)
            if isolated:
                PROXY_RUN.mkdir(exist_ok=True)
                validate(web)
                for sock in sockets: sock.close()
                sockets.clear()
                run('systemctl', 'enable', '--now', 'crdn-managed-proxy.service')
                run('systemctl', 'is-active', '--quiet', 'crdn-managed-proxy.service')
            check_https(state)
            run('systemctl', 'enable', UNIT)
            run('systemctl', 'enable', '--now', 'crdn-updater.service')
            if plan['scheduler'] == 'owned-timer': run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer')
            if firewall: run('systemctl', 'enable', 'crdn-managed-firewall.service')
            state['backups'] = tx.entries; state['phase'] = 'installed'; save(STATE, state)
            tx.commit()
        except BaseException:
            for service in ('crdn-managed-proxy.service', UNIT, 'crdn-updater.service', 'crdn-managed-renew.timer', 'crdn-managed-firewall.service'):
                run('systemctl', 'disable', '--now', service, check=False)
            # External mutations have write-ahead ownership in installation.json.
            firewall_apply(state, False)
            cleanup_certificate(state)
            tx.rollback()
            state['files'] = {}; state['phase'] = 'preparing'; save(STATE, state)
            run('systemctl', 'daemon-reload')
            if not isolated: reload(web)
            raise
        if args.acme_staging: print('Внимание: тестовый ACME staging, сертификат недоверенный.')
        installation_result(domain, new_account, state)
        print('Внешняя доступность порта из интернета независимо не подтверждена.')
    finally:
        for sock in sockets: sock.close()


def check_https(state):
    context = ssl._create_unverified_context() if state.get('staging') else ssl.create_default_context()
    for attempt in range(30):
        try:
            with urlopen(public_origin(state) + '/login', context=context, timeout=5) as response:
                if b'login-form' not in response.read(): raise ValueError('HTTPS не вернул страницу входа CRDN.')
            return
        except OSError:
            if attempt == 29: raise
            time.sleep(2)


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
    if len(active) > 1:
        raise ValueError('Несколько активных веб-серверов; требуется ручная проверка.')
    if not active:
        if listeners:
            return 'isolated', False
        for w, folder in (('caddy', '/etc/caddy'), ('nginx', '/etc/nginx'), ('apache2', '/etc/apache2')):
            if Path(folder).exists() or shutil.which(w):
                raise ValueError('Inactive/existing web-server configuration requires manual review: ' + folder)
        return 'caddy', True
    web = active[0]
    separate = any(name != web for name in re.findall(r'\(\("([^"]+)"', listeners))
    unit = run('systemctl', 'cat', web)
    if web != 'caddy' and ('/etc/systemd/system/' in unit or 'EnvironmentFile=' in unit):
        raise ValueError('Custom web-server unit/environment requires manual review')
    pid = run('systemctl', 'show', web, '-p', 'MainPID', '--value').strip()
    command = Path('/proc/' + pid + '/cmdline').read_bytes().replace(b'\0', b' ').decode()
    if re.search(r'(?:^|\s)-(?:c|p|f)\s', command) and web != 'caddy':
        raise ValueError('Нестандартные аргументы конфигурации веб-сервера; требуется ручная проверка.')
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
    return ('isolated' if separate else web), False


def site(web, domain, port, tls, staging=False, certificates=None):
    if web == 'caddy':
        acme = '\n tls {\n ca ' + STAGING + '\n }' if staging else ''
        return f'{domain} {{\n reverse_proxy 127.0.0.1:{port} {{\n header_up X-CRDN-Client-IP {{remote_host}}\n }}{acme}\n}}\n'
    root = '/var/lib/crdn-managed-acme'
    cert = '/etc/crdn-managed/acme/config/live/crdn/fullchain.pem'
    key = '/etc/crdn-managed/acme/config/live/crdn/privkey.pem'
    if certificates:
        cert, key = certificates
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
    probe(candidate, state['domain'] + (':' + str(state['https_port']) if state.get('https_port', 443) != 443 else ''))
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
        if p.parent == SYSTEMD_DIR and (SYSTEMD_DIR / (p.name + '.d')).exists():
            raise ValueError('У unit CRDN появился неизвестный drop-in; требуется ручная проверка.')


UPDATER_CODE = Path('/usr/local/lib/crdn-managed-updater')
UPDATER_DATA = Path('/var/lib/crdn-updater')
UPDATER_RUN = Path('/run/crdn-updater')


def updater_resources(enable_rc=False, disable_rc=False):
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
    if enable_rc or disable_rc or not config.exists():
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
        for name, data in updater_resources(args.enable_rc_updates, getattr(args, 'disable_rc_updates', False)).items():
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
        if state.get('host_schema'):
            check_https(state)
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


def installation_result(domain, new_account, state=None):
    record = json.loads((DATA / 'owner.json').read_text())
    standard = new_account and record.get('default') is True and record.get('username') == 'admin'
    print('\n' + '=' * 48)
    print('Установка CRDN завершена.\nОткройте панель в браузере:')
    print(public_origin(state or {'domain': domain}))
    if standard:
        print('Логин: admin\nПароль: admin\nПосле входа рекомендуем изменить данные учётной записи.')
    else:
        print('Используйте действующие данные входа. Пароль не изменён и не выводится.')
    print('=' * 48)


def update_result(state):
    print('\nОбновление CRDN завершено')
    print('Установленная версия: ' + state['version'])
    print('Адрес панели: ' + public_origin(state))
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
    # A temporarily idle standalone renewal job still owns future use of TCP 80.
    if fresh and ((ACME_HOME / 'acme.sh').exists() or scheduled_acme_jobs() or
                  any('authenticator = standalone' in p.read_text() for p in Path('/etc/letsencrypt/renewal').glob('*.conf'))):
        web, fresh = 'isolated', False
    check_dns(domain)
    if any(p.is_symlink() for p in (ROOT, META, DATA)):
        raise ValueError('Каталог CRDN заменён ссылкой; автоматическая установка остановлена.')
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
    if web != 'caddy':
        return install_adaptive(args, content, domain, server_name, web, new_account)
    firewall = firewall_plan([443, 80])
    if not fresh and firewall:
        raise ValueError('Закрытый общий порт Caddy нельзя открыть только для домена CRDN; firewall сохранён.')
    packages = ['caddy'] if fresh else []
    META.mkdir(mode=0o700, exist_ok=True)
    planned = dict(domain=domain, port=port, web=web, files={}, phase='preparing', staging=args.acme_staging, firewall=firewall)
    if firewall: planned['host_schema'] = HOST_SCHEMA
    save(STATE, planned)
    if packages:
        environment = {'NEEDRESTART_MODE': 'l', 'DEBIAN_FRONTEND': 'noninteractive'}
        run('apt-get', 'update', env=environment)
        run('apt-get', 'install', '-y', *packages, env=environment)
    try:
        pwd.getpwnam('crdn-managed')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', str(DATA), '--no-create-home', '--shell', '/usr/sbin/nologin', 'crdn-managed')
    save(META / 'account.json', {'uid': pwd.getpwnam('crdn-managed').pw_uid})
    state = dict(domain=domain, port=port, web=web, files={}, phase='installing', staging=args.acme_staging, version=getattr(args,'release',None), firewall=firewall)
    if firewall: state['host_schema'] = HOST_SCHEMA
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
        if firewall:
            write('/etc/systemd/system/crdn-managed-firewall.service', firewall_unit())
            firewall_apply(state, True)
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
        write('/etc/systemd/system/' + UNIT, unit(domain, port))
        for resource, data in updater_resources(args.enable_rc_updates, getattr(args, 'disable_rc_updates', False)).items():
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
        if firewall: run('systemctl', 'enable', 'crdn-managed-firewall.service')
        tx.commit()
    except BaseException:
        run('systemctl', 'disable', '--now', 'crdn-updater.service', check=False)
        run('systemctl', 'disable', '--now', UNIT, check=False)
        run('systemctl', 'disable', '--now', 'crdn-managed-renew.timer', check=False)
        if str(SYSTEMD_DIR / 'crdn-managed-renew.service') in state['files']:
            run('systemctl', 'stop', 'crdn-managed-renew.service', check=False)
        if firewall:
            run('systemctl', 'disable', '--now', 'crdn-managed-firewall.service', check=False)
            firewall_apply(state, False)
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


def updater_cache_files(state):
    """Validate derived bytecode without unmarshalling or executing its contents.

    Only this interpreter's canonical cache names for hashed owned sources are
    accepted. A stale timestamp is harmless only when the entire code payload
    matches a fresh compilation; updates can replace identical source bytes.
    """
    if UPDATER_CODE.is_symlink(): raise ValueError('Updater code directory is symlinked')
    if UPDATER_CODE.exists() and (UPDATER_CODE.stat().st_uid!=os.geteuid() or UPDATER_CODE.stat().st_mode&0o022):
        raise ValueError('Unsafe updater code directory metadata')
    sources = {Path(n): h for n,h in state['files'].items()
               if Path(n).is_relative_to(UPDATER_CODE) and n.endswith('.py')}
    candidates = {}
    directories = {UPDATER_CODE}
    for name in state['files']:
        path = Path(name)
        if path.is_relative_to(UPDATER_CODE):
            directories.update(p for p in path.parents if p.is_relative_to(UPDATER_CODE))
    for source in sources:
        directories.add(source.parent / '__pycache__')
        for opt in (0,1,2):
            cache=Path(importlib.util.cache_from_source(str(source),optimization='' if opt==0 else str(opt)))
            candidates[cache]=(source,opt)
    accepted={}
    for path in UPDATER_CODE.rglob('*') if UPDATER_CODE.exists() else []:
        if path.is_symlink(): raise ValueError('Updater symlink preserved: ' + str(path))
        if path.is_dir():
            if path not in directories: raise ValueError('Unknown updater directory preserved: ' + str(path))
            if path.stat().st_uid!=os.geteuid() or path.stat().st_mode&0o022:
                raise ValueError('Unsafe updater directory metadata: ' + str(path))
            continue
        if str(path) in state['files']: continue
        if path not in candidates or not path.is_file():
            raise ValueError('Неучтённый файл веб-обновления сохранён: ' + str(path))
        source,opt=candidates[path]
        if source.is_symlink() or not source.is_file(): raise ValueError('Cache source missing or symlinked: ' + str(source))
        data=source.read_bytes()
        if digest(data)!=sources[source]: raise ValueError('Cache source differs from manifest: ' + str(source))
        info=path.stat();owner=source.stat()
        if info.st_uid!=os.geteuid() or owner.st_uid!=os.geteuid() or info.st_mode & 0o022 or info.st_nlink!=1:
            raise ValueError('Unsafe updater cache metadata: ' + str(path))
        raw=path.read_bytes()
        if len(raw)<16 or raw[:4]!=importlib.util.MAGIC_NUMBER:
            raise ValueError('Invalid updater bytecode header: ' + str(path))
        flags=int.from_bytes(raw[4:8],'little')
        if flags not in (0,1,3): raise ValueError('Unknown updater cache flags: ' + str(path))
        if flags==0 and int.from_bytes(raw[12:16],'little')!=(len(data)&0xffffffff):
            raise ValueError('Updater cache size mismatch: ' + str(path))
        if flags in (1,3) and raw[8:16]!=importlib.util.source_hash(data):
            raise ValueError('Updater cache source hash mismatch: ' + str(path))
        code=compile(data,str(source),'exec',dont_inherit=True,optimize=opt)
        expected=marshal.dumps(code)
        if raw[16:]!=expected: raise ValueError('Updater bytecode differs from owned source: ' + str(path))
        accepted[path]=digest(raw)
    return accepted


def removal_check(state, resuming=False):
    allowed = {str(ROOT / 'current')}
    for folder, files in state.get('releases', {}).items():
        allowed.update(str(Path(folder) / name) for name in files)
        for name, expected in files.items():
            path = Path(folder) / name
            if resuming and not path.exists() and not path.is_symlink(): continue
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
        caches = updater_cache_files(state)
        for path in UPDATER_CODE.rglob('*'):
            if path in caches: continue
            if path.is_symlink() or (path.is_file() and str(path) not in state['files']):
                raise ValueError('Неучтённый файл веб-обновления сохранён: ' + str(path))
            if path.is_file() and digest(path.read_bytes()) != state['files'][str(path)]:
                raise ValueError('Updater file changed; manual reconciliation required: ' + str(path))

    if resuming:
        if META.exists():
            allowed_meta = {STATE.name, JOURNAL.name, 'account.json', 'update-channel.json', 'acme'}
            if any(p.name not in allowed_meta or p.is_symlink() for p in META.iterdir()):
                raise ValueError('Unexpected metadata resource preserved')
        for name in state['files']:
            path = Path(name)
            if name != '/etc/caddy/Caddyfile' and not path.is_relative_to(UPDATER_CODE) and (path.exists() or path.is_symlink()):
                raise ValueError('Detached resource reappeared; manual reconciliation required: ' + name)
        if run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value', check=False).strip() not in ('', '0'):
            raise ValueError('CRDN is running; cleanup refused')


def removal_identity(state):
    identity = state.get('cleanup_account')
    marker = META / 'account.json'
    if identity is None and marker.exists():
        uid = json.loads(marker.read_text())['uid']
        try: account = pwd.getpwnam('crdn-managed')
        except KeyError:
            try: grp.getgrnam('crdn-managed')
            except KeyError: return None
            raise ValueError('Cannot establish ownership of remaining service group')
        identity = dict(uid=uid, gid=account.pw_gid)
    if identity is None: return None
    if marker.exists() and json.loads(marker.read_text()) != {'uid': identity['uid']}:
        raise ValueError('Service account marker changed; manual reconciliation required')
    try: account = pwd.getpwnam('crdn-managed')
    except KeyError: account = None
    if account and (account.pw_uid != identity['uid'] or account.pw_gid != identity['gid'] or account.pw_dir != str(DATA) or account.pw_shell != '/usr/sbin/nologin'):
        raise ValueError('Service account changed; manual reconciliation required')
    try: group = grp.getgrnam('crdn-managed')
    except KeyError: group = None
    if group and (group.gr_gid != identity['gid'] or set(group.gr_mem) - {'crdn-managed'} or any(u.pw_gid == group.gr_gid and u.pw_name != 'crdn-managed' for u in pwd.getpwall())):
        raise ValueError('Service group is shared; manual reconciliation required')
    return identity


def finish_removal(state):
    removal_check(state, resuming=True)
    identity = removal_identity(state)
    if identity and state.get('cleanup_account') is None:
        state['cleanup_account'] = identity
        save(STATE, state)
    cleanup_certificate(state)
    firewall_apply(state, False)
    if ROOT.exists(): shutil.rmtree(ROOT)
    if (META / 'acme').exists(): shutil.rmtree(META / 'acme')
    if ACME_WEBROOT.is_symlink(): raise ValueError('ACME webroot replaced by symlink')
    if ACME_WEBROOT.exists(): shutil.rmtree(ACME_WEBROOT)
    for folder in (HELPER_DIR, CADDY_SITE_DIR):
        if folder.is_symlink(): raise ValueError('Owned helper directory replaced by symlink')
        if folder.exists():
            try: folder.rmdir()  # Never recursively remove unexpected neighboring files.
            except OSError:
                if any(folder.iterdir()): raise ValueError('Unexpected files in owned helper directory: ' + str(folder))
                raise
    if UPDATER_CODE.exists():
        # Static files and validated caches were detached transactionally. Only
        # empty known directories may remain; never rmtree an unexpected file.
        for path in sorted(UPDATER_CODE.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if path.is_symlink() or not path.is_dir(): raise ValueError('Unexpected updater remainder: ' + str(path))
            path.rmdir()
        UPDATER_CODE.rmdir()
    for folder in (UPDATER_DATA, UPDATER_RUN):
        if folder.exists(): shutil.rmtree(folder)
    if DATA.exists(): shutil.rmtree(DATA)
    if identity:
        # Recheck before each identity operation; an interrupted retry must not touch a reused name.
        removal_identity(state)
        try: pwd.getpwnam('crdn-managed')
        except KeyError: pass
        else: run('userdel', 'crdn-managed')
        removal_identity(state)
        try: grp.getgrnam('crdn-managed')
        except KeyError: pass
        else: run('groupdel', 'crdn-managed')
    # Keep installation.json until all physical/identity cleanup is complete.
    # A retry never loses the evidence needed to check its remaining resources.
    if META.exists():
        allowed = {STATE.name, JOURNAL.name, 'account.json', 'update-channel.json'}
        for path in META.iterdir():
            if path.name not in allowed or path.is_symlink() or not path.is_file():
                raise ValueError('Unexpected metadata resource preserved: ' + str(path))
        for path in META.iterdir():
            if path != STATE: path.unlink()
        STATE.unlink(missing_ok=True)
        META.rmdir()
    print('Проверка результата удаления…', flush=True)
    if any(p.exists() for p in (ROOT, DATA, META)):
        raise RuntimeError('Удаление завершено не полностью; остались файлы CRDN.')
    print('CRDN удалён. Приложение и его учётные данные удалены')


def uninstall(state):
    if state.get('phase') == 'removing' and not state.get('files') and not state.get('active') and not ROOT.exists():
        # Older tooling could abort cleanup of an incomplete installation here.
        state['phase'] = 'preparing'
        save(STATE, state)
    if state.get('phase') in ('cleaning', 'removed'):
        finish_removal(state)
        return
    owned_check(state)
    removal_check(state)
    identity = removal_identity(state)
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
        if state.get('mode') == 'isolated' and str(SYSTEMD_DIR / 'crdn-managed-proxy.service') in state['files']:
            run('systemctl', 'disable', '--now', 'crdn-managed-proxy.service')
        if str(SYSTEMD_DIR / 'crdn-managed-renew.service') in state['files']:
            run('systemctl', 'stop', 'crdn-managed-renew.service', check=False)
        if state.get('firewall'):
            run('systemctl', 'disable', '--now', 'crdn-managed-firewall.service', check=False)
            firewall_apply(state, False)
        for cache, expected in updater_cache_files(state).items():
            tx.write(cache, None, expected_hash=expected)
        for name in state['files']:
            if name == '/etc/caddy/Caddyfile':
                backups = [e for e in state.get('backups', []) if e['path'] == name]
                if backups:
                    tx.write(name, bytes.fromhex(backups[0]['before']) if backups[0]['before'] is not None else None, backups[0]['mode'])
            else:
                tx.write(name, None)
        if previous_phase != 'preparing' and state['web'] != 'isolated':
            reload(state['web'])
        run('systemctl', 'daemon-reload')
        state['phase'] = 'cleaning'
        if identity: state['cleanup_account'] = identity
        tx.write(STATE, (json.dumps(state, indent=2) + '\n').encode(), 0o600)
        tx.commit()
    except BaseException:
        tx.rollback()
        if previous_phase != 'preparing' and state['web'] != 'isolated': reload(state['web'])
        run('systemctl', 'daemon-reload')
        if previous_phase != 'preparing': firewall_apply(state, True)
        if state.get('mode') == 'isolated' and str(SYSTEMD_DIR / 'crdn-managed-proxy.service') in state['files']:
            run('systemctl', 'enable', '--now', 'crdn-managed-proxy.service')
        if previous_phase != 'preparing': run('systemctl', 'enable', '--now', UNIT, check=False)
        if '/etc/systemd/system/crdn-managed-renew.timer' in state['files']:
            run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer', check=False)
        state['phase'] = previous_phase
        save(STATE, state)
        raise
    finish_removal(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('install', 'update', 'uninstall', 'recover', 'certificate-hook', 'renew', 'firewall-restore'))
    parser.add_argument('--domain')
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument('--enable-rc-updates', action='store_true', help='Explicitly opt into the RC web-update test channel')
    channel.add_argument('--disable-rc-updates', action='store_true', help='Explicitly return web updates to stable-only')
    parser.add_argument('--release', help='Explicit GitHub Release tag; permits this exact Pre-release only')
    parser.add_argument('--recover', action='store_true', help='Recover interrupted operation before retry')
    parser.add_argument('--test-archive', help='Explicit local runtime fixture; never selected automatically')
    parser.add_argument('--test-sha256')
    parser.add_argument('--acme-staging', action='store_true')
    args = parser.parse_args()
    if args.operation == 'certificate-hook':
        if os.geteuid() != 0: raise ValueError('Требуется root.')
        certificate_hook()
        return
    if args.operation == 'firewall-restore':
        if os.geteuid() != 0: raise ValueError('Требуется root.')
        state = json.loads(STATE.read_text())
        if state.get('phase') == 'installed':
            owned_check(state)
            firewall_apply(state, True)
        return
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
                if completed and completed.get('phase') in ('preparing', 'installing'):
                    for name in (UNIT, 'crdn-managed-proxy.service', 'crdn-updater.service', 'crdn-managed-renew.timer', 'crdn-managed-renew.service', 'crdn-managed-firewall.service'):
                        if str(SYSTEMD_DIR / name) in completed.get('files', {}):
                            run('systemctl', 'disable', '--now', name, check=False)
                    firewall_apply(completed, False)
                    cleanup_certificate(completed)
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
                    firewall_apply(recovered, True)
                    if recovered.get('mode') == 'isolated': run('systemctl', 'enable', '--now', 'crdn-managed-proxy.service')
                    if recovered['web'] != 'caddy' and '/etc/systemd/system/crdn-managed-renew.timer' in recovered['files']:
                        run('systemctl', 'enable', '--now', 'crdn-managed-renew.timer')
                elif recovered['phase'] != 'installed':
                    recovered['files'] = {}
                    recovered['phase'] = 'preparing'
                save(STATE, recovered)
        state = json.loads(STATE.read_text()) if STATE.exists() else None
        if args.operation == 'renew':
            if not state or state.get('phase') != 'installed' or not state.get('certificate'):
                raise ValueError('Нет готовой установки с отдельным сертификатом.')
            owned_check(state)
            if obtain_certificate(state, renew=True): certificate_hook()
            return
        if args.operation == 'recover':
            if state and state['phase'] in ('cleaning', 'removed'):
                print('Удаление отсоединено. Продолжите завершающую очистку через uninstall.sh.')
                return
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
                if META.exists() and not META.is_symlink() and not any(META.iterdir()) and not ROOT.exists() and not DATA.exists():
                    META.rmdir()  # Interrupted between deleting the final state file and rmdir.
                print('CRDN не установлен. Удалять нечего.')
            return
        if state:
            if state['phase'] != 'installed':
                raise ValueError('Найдена незавершённая установка. Перед повтором требуется восстановление и удаление её ресурсов.')
            owned_check(state)
            if args.operation == 'install':
                if args.domain and args.domain != state['domain']:
                    raise ValueError('У существующей установки другой домен.')
                print('CRDN уже установлен. Адрес панели: ' + public_origin(state))
                return
            run('systemctl', 'is-active', UNIT)
            content = source(args)
            managed_update(content, state, args)
            if args.release and 'release-version' not in content:
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
            elif saved.get('phase') in ('cleaning', 'removed'): print('Служба отключена; удаление ресурсов не завершено. Повторите удаление.', file=sys.stderr)
            else: print('Остались ресурсы незавершённой установки; их состояние сохранено для восстановления/удаления.', file=sys.stderr)
        else: print('Активная установка этой операцией не подтверждена.', file=sys.stderr)
        raise SystemExit('Причина: ' + str(error))
