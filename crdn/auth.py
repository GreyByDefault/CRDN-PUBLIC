"""Single-owner credentials and bounded in-memory server sessions (stdlib only)."""
from collections import defaultdict, deque
import fcntl
import hashlib
import hmac
import json
import re
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

COOKIE = '__Host-crdn-session'
SESSION_TTL = 30 * 60
PREAUTH_TTL = 900
AUTH_SESSION_LIMIT = 512
PREAUTH_BURST = 64
PREAUTH_PER_SECOND = 4
# Successful logins are already capped at 30/min: <=450 consumed tokens per 15min.
CONSUMED_PREAUTH_LIMIT = 512


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
        self.preauth_secret = secrets.token_bytes(32)
        self.consumed_preauth = {}
        self.preauth_credit = float(PREAUTH_BURST)
        self.preauth_refilled = self.clock()
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
        self.consumed_preauth = {key: expiry for key, expiry in self.consumed_preauth.items() if expiry > now}
        for key in list(self.attempts):
            queue = self.attempts[key]
            while queue and queue[0] <= now - 60: queue.popleft()
            if not queue: del self.attempts[key]

    def _preauth(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'p\.[0-9]{1,20}\.[A-Za-z0-9_-]{22}\.[0-9a-f]{64}', token):
            return None
        body, signature = token.rsplit('.', 1)
        if not hmac.compare_digest(signature, hmac.new(self.preauth_secret, body.encode(), hashlib.sha256).hexdigest()):
            return None
        issued = int(body.split('.')[1])
        if not issued <= self.clock() < issued + PREAUTH_TTL or self._key(token) in self.consumed_preauth:
            return None
        csrf = hmac.new(self.preauth_secret, b'csrf:' + token.encode(), hashlib.sha256).hexdigest()
        return {'csrf': csrf, 'authenticated': False, 'remind': False, 'expires': issued + PREAUTH_TTL}

    def _new(self, authenticated=False):
        self._prune()
        if not authenticated:
            now = self.clock()
            self.preauth_credit = min(PREAUTH_BURST, self.preauth_credit + max(0, now - self.preauth_refilled) * PREAUTH_PER_SECOND)
            self.preauth_refilled = now
            if self.preauth_credit < 1:
                raise AuthError(429, 'Слишком много запросов сессии. Повторите позже.')
            self.preauth_credit -= 1
            body = 'p.' + str(int(now)) + '.' + secrets.token_urlsafe(16)
            token = body + '.' + hmac.new(self.preauth_secret, body.encode(), hashlib.sha256).hexdigest()
            return token, self._preauth(token)
        if len(self.sessions) >= AUTH_SESSION_LIMIT:
            raise AuthError(429, 'Слишком много запросов. Повторите позже.')
        token = secrets.token_urlsafe(32)
        session = {'csrf': secrets.token_urlsafe(32), 'authenticated': True,
                   'remind': self.account['default'], 'expires': self.clock() + SESSION_TTL}
        self.sessions[self._key(token)] = session
        return token, session

    def session(self, token, create=False):
        with self.lock:
            self._prune()
            session = self.sessions.get(self._key(token)) if isinstance(token, str) and len(token) <= 128 else None
            if session:
                return token, session.copy()
            preauth = self._preauth(token)
            if preauth:
                return token, preauth
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
                'remind_default': session['remind'],
                'default_credentials': bool(session['authenticated'] and self.account['default'])}

    def authorize_update(self, token, csrf):
        with self.lock:
            _, session = self.session(token)
            if not session or not session['authenticated']:
                raise AuthError(401, 'Требуется вход.')
            if not isinstance(csrf, str) or not hmac.compare_digest(csrf.encode(), session['csrf'].encode()):
                raise AuthError(403, 'Сессия устарела. Обновите страницу.')

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
                if session['authenticated']:
                    del self.sessions[self._key(token)]
                else:
                    if len(self.sessions) >= AUTH_SESSION_LIMIT or len(self.consumed_preauth) >= CONSUMED_PREAUTH_LIMIT:
                        raise AuthError(429, 'Слишком много запросов. Повторите позже.')
                    self.consumed_preauth[self._key(token)] = session['expires']
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
                self.preauth_secret = secrets.token_bytes(32)
                self.consumed_preauth.clear()
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
