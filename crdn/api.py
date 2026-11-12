"""Loopback-only HTTP API with a single background sampler."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import re
import os
from http.cookies import SimpleCookie
from pathlib import Path
import threading
import time

from .metrics import Collector
from . import updates
from .auth import AuthStore, AuthError, COOKIE, SESSION_TTL


class SnapshotStore:
    def __init__(self, collector=None, interval=2.0):
        self.collector = collector if collector is not None else Collector()
        if interval <= 0:
            raise ValueError('Sampling interval must be positive')
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._body = b''
        self.update()

    def update(self):
        body = json.dumps(self.collector.sample(), allow_nan=False).encode('utf-8')
        with self._lock:
            self._body = body

    def body(self):
        with self._lock:
            return self._body

    def _run(self):
        deadline = time.monotonic() + self.interval
        while not self._stop.wait(max(0, deadline - time.monotonic())):
            self.update()
            deadline += self.interval
            if deadline <= time.monotonic():
                deadline = time.monotonic() + self.interval

    def start(self):
        self._thread = threading.Thread(target=self._run, name='crdn-sampler', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def make_server(store, port=8765, auth=None, origin=None):
    if auth is None:
        state = os.environ.get('CRDN_STATE_DIR', str(Path.home() / '.local/state/crdn'))
        auth = AuthStore(state)
    origin = origin or os.environ.get('CRDN_ORIGIN', f'http://127.0.0.1:{port}')

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def token(self):
            try:
                cookies = SimpleCookie(self.headers.get('Cookie', ''))
                return cookies[COOKIE].value if COOKIE in cookies else None
            except Exception:
                return None

        def send(self, status, body=b'', content_type='application/json; charset=utf-8', token=None, location=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'same-origin')
            if status == 429: self.send_header('Retry-After', '60')
            if location: self.send_header('Location', location)
            if token is not None:
                age = SESSION_TTL if token else 0
                self.send_header('Set-Cookie', f'{COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; Secure; SameSite=Strict')
            self.end_headers()
            self.wfile.write(body)

        def json(self, status, value, token=None):
            self.send(status, json.dumps(value, ensure_ascii=False).encode(), token=token)

        def asset(self, filename, kind):
            try: body = (Path(__file__).parent / 'web' / filename).read_bytes()
            except OSError:
                self.json(503, {'error': 'Ресурс временно недоступен.'})
                return
            self.send(200, body, kind)

        def do_GET(self):
            try:
                if self.path == '/api/auth/session':
                    token, session = auth.session(self.token(), create=True)
                    self.json(200, auth.public_session(session), token)
                    return
                _, session = auth.session(self.token())
                logged_in = session and session['authenticated']
                if self.path == '/login':
                    if logged_in: self.send(303, location='/')
                    else: self.asset('login.html', 'text/html; charset=utf-8')
                    return
                public_assets = {'/favicon.ico': ('favicon.ico', 'image/x-icon'),
                                 '/auth.css': ('auth.css', 'text/css; charset=utf-8'),
                                 '/auth.js': ('auth.js', 'text/javascript; charset=utf-8')}
                if self.path in public_assets:
                    self.asset(*public_assets[self.path])
                    return
                if not logged_in:
                    if self.path.startswith('/api/'):
                        self.json(401, {'error': 'Требуется вход.'})
                    else: self.send(303, location='/login')
                    return
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
                if self.path == '/api/updates': self.json(200, updates.request())
                elif self.path == '/updates.js': self.asset('updates.js', 'text/javascript; charset=utf-8')
                elif self.path == '/api/metrics': self.send(200, store.body())
                elif self.path in assets: self.asset(*assets[self.path])
                else: self.send(404)
            except AuthError as error:
                self.json(error.status, {'error': error.message})

        def do_POST(self):
            routes = {'/api/auth/login': 'login', '/api/auth/change': 'change',
                      '/api/auth/logout': 'logout', '/api/auth/dismiss': 'dismiss',
                      '/api/auth/activity': 'activity'}
            update_operation = {'/api/updates/check': 'check', '/api/updates/start': 'update'}.get(self.path)
            if self.path not in routes and not update_operation:
                self.send(404)
                return
            try:
                if self.headers.get('Origin') != origin:
                    raise AuthError(403, 'Запрос с другого сайта запрещён.')
                if self.headers.get_content_type() != 'application/json':
                    raise AuthError(415, 'Требуется JSON.')
                try: size = int(self.headers.get('Content-Length', '0'))
                except ValueError: size = 0
                if not 0 < size <= 8192:
                    raise AuthError(413, 'Некорректный размер запроса.')
                try: data = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeError):
                    raise AuthError(400, 'Некорректный запрос.')
                if not isinstance(data, dict): raise AuthError(400, 'Некорректный запрос.')
                if update_operation:
                    auth.authorize_update(self.token(), self.headers.get('X-CSRF-Token'))
                    if update_operation == 'update':
                        if set(data) != {'version'} or not isinstance(data['version'], str) or len(data['version']) > 64 or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?', data['version']):
                            raise AuthError(400, 'Требуется выбранная версия выпуска.')
                        value = updates.request('update', data['version'])
                    else:
                        if data: raise AuthError(400, 'Параметры проверки не принимаются.')
                        value = updates.request('check')
                    self.json(409 if value.get('error') else 200, value)
                    return
                peer = self.client_address[0]
                # Caddy overwrites this header; only the loopback proxy is trusted.
                forwarded = self.headers.get('X-CRDN-Client-IP')
                if forwarded and peer == '127.0.0.1':
                    try: peer = str(ipaddress.ip_address(forwarded))
                    except ValueError: pass
                token, value = auth.mutate(routes[self.path], self.token(),
                                           self.headers.get('X-CSRF-Token'), data, peer)
                self.json(200, value, token)
            except AuthError as error:
                self.json(error.status, {'error': error.message})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        slots = threading.BoundedSemaphore(32)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try: super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try: super().process_request_thread(request, client_address)
            finally: self.slots.release()

    return Server(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='CRDN local metrics API')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    store = SnapshotStore()
    try:
        with make_server(store, args.port) as server:
            store.start()
            print(f'CRDN: http://127.0.0.1:{server.server_port}/', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        store.close()


if __name__ == '__main__':
    main()
