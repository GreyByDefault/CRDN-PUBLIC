"""Single-owner credentials and bounded in-memory server sessions (stdlib only)."""
from collections import defaultdict, deque
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

COOKIE = '__Host-crdn-session'
SESSION_TTL = 30 * 60
PREAUTH_TTL = 900


class AuthError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def password_record(password):
    salt = secrets.token_bytes(32)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                            maxmem=64 * 1024**2, dklen=32)
    return {'salt': salt.hex(), 'hash': digest.hex()}


def verify(password, record):
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record['salt']),
                            n=32768, r=8, p=3, maxmem=64 * 1024**2, dklen=32)
    return hmac.compare_digest(digest.hex(), record['hash'])


def valid_username(username):
    return isinstance(username, str) and 1 <= len(username) <= 64 and all(
        ch.isprintable() and not ch.isspace() for ch in username)


class AuthStore:
    def __init__(self, directory, clock=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'owner.json'
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = defaultdict(deque)
        # Initialization never overwrites an existing account, even after restart.
        with (self.directory / 'owner.lock').open('a') as guard:
            os.chmod(guard.name, 0o600)
            fcntl.flock(guard, fcntl.LOCK_EX)
            if self.path.exists():
                self.account = json.loads(self.path.read_text())
                if (self.account.get('version') != 1 or not valid_username(self.account.get('username'))
                        or not isinstance(self.account.get('default'), bool)
                        or any(not isinstance(self.account.get(key), str) or len(self.account[key]) != 64
                               for key in ('salt', 'hash'))):
                    raise ValueError('Invalid owner record; refusing to reset credentials')
                bytes.fromhex(self.account['salt']); bytes.fromhex(self.account['hash'])
            else:
                self.account = {'version': 1, 'username': 'admin', 'default': True,
                                **password_record('admin')}
                self._save(self.account)

    def _save(self, record):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as target:
                temporary = Path(target.name)
                os.fchmod(target.fileno(), 0o600)
                json.dump(record, target)
                target.write('\n'); target.flush(); os.fsync(target.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fd = os.open(self.directory, os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _prune(self):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value['expires'] > now}
        for key in list(self.attempts):
            queue = self.attempts[key]
            while queue and queue[0] <= now - 60: queue.popleft()
            if not queue: del self.attempts[key]

    def _new(self, authenticated=False):
        self._prune()
        if len(self.sessions) >= 512:
            raise AuthError(429, 'Слишком много запросов. Повторите позже.')
        token = secrets.token_urlsafe(32)
        session = {'csrf': secrets.token_urlsafe(32), 'authenticated': authenticated,
                   'remind': authenticated and self.account['default'],
                   'expires': self.clock() + (SESSION_TTL if authenticated else PREAUTH_TTL)}
        self.sessions[self._key(token)] = session
        return token, session

    def session(self, token, create=False):
        with self.lock:
            self._prune()
            session = self.sessions.get(self._key(token)) if isinstance(token, str) and len(token) <= 128 else None
            if session:
                return token, session.copy()
            if create: return self._new()
            return None, None

    def _budget(self, peer, kind):
        self._prune()
        key = (kind, peer)
        if len(self.attempts[key]) >= 5 or len(self.attempts[('global', kind)]) >= 30:
            raise AuthError(429, 'Слишком много попыток. Повторите через минуту.')
        self.attempts[key].append(self.clock())
        self.attempts[('global', kind)].append(self.clock())

    def public_session(self, session):
        return {'authenticated': session['authenticated'], 'csrf_token': session['csrf'],
                'username': self.account['username'] if session['authenticated'] else None,
                'remind_default': session['remind']}

    def mutate(self, operation, token, csrf, data, peer):
        with self.lock:
            _, session = self.session(token)
            if not session or not isinstance(csrf, str) or not hmac.compare_digest(csrf.encode(), session['csrf'].encode()):
                raise AuthError(403, 'Сессия устарела. Обновите страницу и повторите вход.')
            if operation != 'login' and not session['authenticated']:
                raise AuthError(401, 'Требуется вход.')
            if operation == 'login':
                self._budget(peer, 'login')
                username, password = data.get('username'), data.get('password')
                if not valid_username(username) or not isinstance(password, str) or len(password) > 256:
                    raise AuthError(400, 'Проверьте введённые данные.')
                correct = verify(password, self.account)
                if not correct or not hmac.compare_digest(username.encode(), self.account['username'].encode()):
                    raise AuthError(401, 'Неверный логин или пароль.')
                del self.sessions[self._key(token)]
                new_token, new_session = self._new(True)
                return new_token, self.public_session(new_session)
            if operation == 'change':
                self._budget(peer, 'change')
                current = data.get('current_password')
                if not isinstance(current, str) or len(current) > 256 or not verify(current, self.account):
                    raise AuthError(401, 'Текущий пароль указан неверно.')
                username, password = data.get('username'), data.get('password')
                if not valid_username(username):
                    raise AuthError(400, 'Логин: 1–64 символа без пробелов.')
                if not isinstance(password, str) or not 8 <= len(password) <= 256:
                    raise AuthError(400, 'Новый пароль: от 8 до 256 символов.')
                if password != data.get('password_confirm'):
                    raise AuthError(400, 'Пароли не совпадают.')
                record = {'version': 1, 'username': username, 'default': False, **password_record(password)}
                self._save(record)
                self.account = record
                self.sessions.clear()
                new_token, new_session = self._new(True)
                return new_token, self.public_session(new_session)
            if operation == 'activity':
                age = data.get('age_seconds', 0)
                if (not isinstance(age, (int, float)) or isinstance(age, bool)
                        or not 0 <= age < SESSION_TTL):
                    raise AuthError(400, 'Некорректное время активности.')
                stored = self.sessions[self._key(token)]
                stored['expires'] = max(stored['expires'], self.clock() - age + SESSION_TTL)
                return token, {'ok': True}
            if operation == 'dismiss':
                self.sessions[self._key(token)]['expires'] = self.clock() + SESSION_TTL
                self.sessions[self._key(token)]['remind'] = False
                return token, {'ok': True}
            if operation == 'logout':
                del self.sessions[self._key(token)]
                return '', {'ok': True}
            raise AuthError(404, 'Не найдено.')
