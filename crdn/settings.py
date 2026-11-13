"""User preferences outside release trees; atomic, process-safe writes."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unicodedata
from .auth import AuthError


def validate_name(value):
    if not isinstance(value, str) or len(value) > 24 or any(unicodedata.category(c).startswith('C') for c in value):
        raise AuthError(400, 'Название: не более 24 символов, без управляющих знаков.')
    return value.strip()


class SettingsStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'settings.json'

    def read(self):
        try:
            data = json.loads(self.path.read_text())
            return {'server_name': validate_name(data['server_name'])}
        except FileNotFoundError:
            return {'server_name': ''}
        except (OSError, ValueError, KeyError, TypeError, AuthError):
            raise AuthError(503, 'Настройки повреждены. Требуется проверка через SSH.')

    def save(self, data):
        if set(data) != {'server_name'}:
            raise AuthError(400, 'Ожидается только название сервера.')
        value = {'server_name': validate_name(data['server_name'])}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / 'settings.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.read()  # A damaged existing record must not silently reset.
            fd, name = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(fd, 'w') as out:
                    os.fchmod(out.fileno(), 0o600)
                    json.dump(value, out, ensure_ascii=False)
                    out.flush(); os.fsync(out.fileno())
                os.replace(name, self.path)
                directory = os.open(self.directory, os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
        return value
