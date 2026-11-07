"""Notification Manager: record Linux desktop notifications and browse them in a local web UI."""

import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file, stream_with_context)

# ─── Configuration (environment variables) ──────────────────────────────────
#   NM_HOST           bind address           (default 127.0.0.1 — local only)
#   NM_PORT           port                   (default 5000)
#   NM_DATA_DIR       database, wallpaper, token
#                     (default $XDG_DATA_HOME/notification-manager or
#                      ~/.local/share/notification-manager)
#   NM_ALLOWED_HOSTS  extra comma-separated host names the UI may be reached
#                     by, e.g. a LAN hostname when NM_HOST is not loopback

HOST = os.environ.get('NM_HOST', '127.0.0.1')
PORT = int(os.environ.get('NM_PORT', '5000'))
DATA_DIR = os.path.expanduser(
    os.environ.get('NM_DATA_DIR')
    or os.path.join(os.environ.get('XDG_DATA_HOME') or '~/.local/share', 'notification-manager')
)
DB_PATH = os.path.join(DATA_DIR, 'notifications.db')
TOKEN_PATH = os.path.join(DATA_DIR, 'token')
WP_EXTS = ('jpg', 'jpeg', 'png', 'webp', 'gif')
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}
if HOST not in ('0.0.0.0', '::', ''):
    ALLOWED_HOSTS.add(HOST.strip('[]'))
ALLOWED_HOSTS |= {h.strip().lower() for h in os.environ.get('NM_ALLOWED_HOSTS', '').split(',') if h.strip()}

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()

_recent_notifs: dict = {}   # (app, summary, body) → last seen timestamp
_recent_lock = threading.Lock()

_token: str | None = None
_token_lock = threading.Lock()


def log(msg: str):
    # stdout may be a closed terminal; a failed write must never kill the listener
    try:
        print(msg, flush=True)
    except OSError:
        pass


def ensure_data_dir():
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)


# ─── Request protection ─────────────────────────────────────────────────────
#
# The UI is a local web server, so two browser-based attacks matter even when
# it only listens on 127.0.0.1:
#   * DNS rebinding — a malicious site re-points its own hostname at 127.0.0.1
#     and reads /notifications as "same origin". Blocked by the Host check.
#   * Cross-site requests (CSRF) — any site can POST to localhost:5000.
#     Blocked by requiring a secret token header on every state-changing
#     request; other sites cannot read the token or send custom headers
#     cross-origin (no CORS headers are ever returned).

def get_token() -> str:
    """Per-install secret, persisted so it survives restarts."""
    global _token
    with _token_lock:
        if _token is None:
            ensure_data_dir()
            try:
                with open(TOKEN_PATH, encoding='ascii') as f:
                    _token = f.read().strip()
            except FileNotFoundError:
                _token = ''
            if not _token:
                _token = secrets.token_urlsafe(32)
                fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='ascii') as f:
                    f.write(_token)
        return _token


def _host_name(netloc: str) -> str:
    """'localhost:5000' → 'localhost', '[::1]:5000' → '::1'."""
    netloc = (netloc or '').strip().lower()
    if netloc.startswith('['):
        return netloc[1:netloc.find(']')] if ']' in netloc else netloc
    return netloc.rsplit(':', 1)[0] if netloc.count(':') == 1 else netloc


@app.before_request
def guard_request():
    if _host_name(request.host) not in ALLOWED_HOSTS:
        abort(403, 'host not allowed')
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get('Origin')
    if origin and _host_name(urlsplit(origin).netloc) not in ALLOWED_HOSTS:
        abort(403, 'cross-origin request')
    if not hmac.compare_digest(request.headers.get('X-NM-Token', ''), get_token()):
        abort(403, 'missing or invalid token')


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'",
    )
    return resp


@app.errorhandler(403)
@app.errorhandler(413)
def json_error(err):
    return jsonify({'error': getattr(err, 'description', str(err))}), err.code


# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    ensure_data_dir()
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                app_name  TEXT    NOT NULL,
                summary   TEXT    DEFAULT '',
                body      TEXT    DEFAULT '',
                checked   INTEGER DEFAULT 0,
                comment   TEXT    DEFAULT ''
            )
        ''')
        # Migration: add comment column to existing databases
        cols = {row[1] for row in conn.execute('PRAGMA table_info(notifications)')}
        if 'comment' not in cols:
            conn.execute("ALTER TABLE notifications ADD COLUMN comment TEXT DEFAULT ''")
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


# ─── SSE broadcast ───────────────────────────────────────────────────────────

def broadcast(data: dict):
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ─── Notification storage ────────────────────────────────────────────────────

def save_notification(app_name: str, summary: str, body: str):
    key = (app_name, summary, body)
    now = time.time()
    with _recent_lock:
        if now - _recent_notifs.get(key, 0) < 2.0:
            return
        _recent_notifs[key] = now
    timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO notifications (timestamp, app_name, summary, body) VALUES (?, ?, ?, ?)',
            (timestamp, app_name, summary, body)
        )
        notif_id = cur.lastrowid
    notif = {
        'id': notif_id, 'timestamp': timestamp,
        'app_name': app_name, 'summary': summary,
        'body': body, 'checked': 0, 'comment': '',
    }
    broadcast(notif)
    log(f'[{timestamp}] {app_name!r}: {summary}')


# ─── App names from .desktop files ───────────────────────────────────────────

SNAP_DESKTOP_DIR = '/var/lib/snapd/desktop/applications'
DESKTOP_DIRS = [
    os.path.expanduser('~/.local/share/applications'),
    os.path.expanduser('~/.local/share/flatpak/exports/share/applications'),
    '/var/lib/flatpak/exports/share/applications',
    SNAP_DESKTOP_DIR,
    '/usr/local/share/applications',
    '/usr/share/applications',
]
_desktop_names: dict = {}


def _desktop_files(entry: str) -> list:
    if entry.startswith('snap.'):
        # snaps are identified as 'snap.<name>'; snapd installs '<name>_<app>.desktop'
        snap = entry[5:]
        try:
            found = sorted(f for f in os.listdir(SNAP_DESKTOP_DIR)
                           if f.startswith(snap + '_') and f.endswith('.desktop'))
        except OSError:
            found = []
        preferred = f'{snap}_{snap}.desktop'
        if preferred in found:
            found.remove(preferred)
            found.insert(0, preferred)
        return [os.path.join(SNAP_DESKTOP_DIR, f) for f in found]
    return [os.path.join(d, entry + '.desktop') for d in DESKTOP_DIRS]


def desktop_entry_name(entry: str) -> str:
    """App name for a desktop-entry hint, e.g. 'snap.slack' / 'slack_slack' → 'Slack'."""
    if entry not in _desktop_names:
        name = entry[5:] if entry.startswith('snap.') else entry
        for path in _desktop_files(entry):
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    in_main = False
                    for line in f:
                        line = line.strip()
                        if line.startswith('['):
                            in_main = line == '[Desktop Entry]'
                        elif in_main and line.startswith('Name='):
                            name = line[5:].strip() or name
                            break
                break
            except OSError:
                continue
        _desktop_names[entry] = name
    return _desktop_names[entry]


# ─── dbus-monitor parser ─────────────────────────────────────────────────────

# A line dbus-monitor emits for message structure (not part of a string value).
# String values are printed raw — embedded newlines and quotes are not escaped —
# so a string arg only ends where the next structural line begins.
_STRUCT_RE = re.compile(
    r'^(method call|method return|signal|error) '
    r'|^\s+(string "|u?int(16|32|64) |byte |boolean |double |variant |array \[|dict entry\(|struct \{|object path )'
    r'|^\s+[\])}]$'
)
# Notify's last arg (expire_timeout), printed at top level after the hints dict
_EXPIRE_RE = re.compile(r'^   int32 -?\d+$')


def _parse_string_open(line: str):
    """Text after `string "` on the line, or None if the line isn't a string arg."""
    m = re.match(r'\s*string "(.*)$', line)
    return m.group(1) if m else None


def parse_notify_stream(lines):
    """Parse dbus-monitor output for Notify calls.

    Yields (args, desktop_entry) where args is
    [app_name, replaces_id, app_icon, summary, body] and desktop_entry is the
    value of the 'desktop-entry' hint (or None).
    """
    collecting = False
    args: list = []
    pending: list | None = None   # lines of the string arg being read
    desktop_entry: str | None = None
    want_entry = False            # last hint key was "desktop-entry"

    for raw in lines:
        line = raw.rstrip('\n')
        stripped = line.strip()

        if pending is not None:
            if not _STRUCT_RE.match(line):
                pending.append(line)
                continue
            text = '\n'.join(pending)
            args.append(text[:-1] if text.endswith('"') else text)
            pending = None

        if 'member=Notify' in line:
            if collecting and len(args) == 5:
                yield args, desktop_entry   # previous message ended early
            collecting = True
            args = []
            desktop_entry = None
            want_entry = False
            continue

        if not collecting:
            continue

        n = len(args)
        if n in (0, 2, 3, 4):
            text = _parse_string_open(line)
            if text is not None:
                pending = [text]
            elif stripped and not stripped.startswith('#'):
                collecting = False
        elif n == 1:
            m = re.match(r'\s*uint32 (\d+)', line)
            if m:
                args.append(int(m.group(1)))
            elif stripped and not stripped.startswith('#'):
                collecting = False
        else:
            # actions + hints: pick up the desktop-entry hint, emit at expire_timeout
            if stripped == 'string "desktop-entry"':
                want_entry = True
            elif want_entry:
                m = re.match(r'\s*variant\s+string "(.*)"$', line)
                if m:
                    desktop_entry = m.group(1)
                want_entry = False
            elif _EXPIRE_RE.match(line):
                yield args, desktop_entry
                collecting = False
                args = []

    if collecting and len(args) == 5:
        yield args, desktop_entry


def resolve_app_name(args: list, desktop_entry: str | None) -> str:
    # Portal notifications (snaps/flatpaks, e.g. Slack) arrive with an empty
    # app_name; GNOME identifies the sender via the desktop-entry hint instead.
    app_name = args[0].strip()
    if not app_name and desktop_entry:
        app_name = desktop_entry_name(desktop_entry)
    return app_name or 'Unknown'


def listen_dbus():
    cmd = [
        'dbus-monitor', '--session',
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    ]
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            for args, desktop_entry in parse_notify_stream(proc.stdout):
                try:
                    save_notification(resolve_app_name(args, desktop_entry),
                                      args[3].strip(), args[4].strip())
                except Exception as exc:
                    log(f'failed to save notification: {exc!r}')
            log('dbus-monitor exited, restarting in 5s')
        except Exception as exc:
            log(f'dbus-monitor error: {exc!r}')
        finally:
            if proc and proc.poll() is None:
                proc.kill()

        time.sleep(5)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', token=get_token())


@app.route('/notifications')
def get_notifications():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM notifications ORDER BY id DESC').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/notifications/<int:nid>/check', methods=['PATCH'])
def update_check(nid: int):
    body = request.get_json(silent=True) or {}
    checked = 1 if body.get('checked') else 0
    with get_db() as conn:
        conn.execute('UPDATE notifications SET checked=? WHERE id=?', (checked, nid))
    return jsonify({'ok': True})


@app.route('/notifications/<int:nid>/comment', methods=['PATCH'])
def update_comment(nid: int):
    body = request.get_json(silent=True) or {}
    comment = str(body.get('comment', ''))[:10_000]
    with get_db() as conn:
        conn.execute('UPDATE notifications SET comment=? WHERE id=?', (comment, nid))
    return jsonify({'ok': True})


# ─── Wallpaper ───────────────────────────────────────────────────────────────

def _wallpaper_path():
    for ext in WP_EXTS:
        p = os.path.join(DATA_DIR, f'wallpaper.{ext}')
        if os.path.exists(p):
            return p
    return None


def _remove_wallpapers():
    for ext in WP_EXTS:
        p = os.path.join(DATA_DIR, f'wallpaper.{ext}')
        if os.path.exists(p):
            os.remove(p)


@app.route('/wallpaper', methods=['GET'])
def get_wallpaper():
    path = _wallpaper_path()
    if path:
        return send_file(path)
    return '', 404


@app.route('/wallpaper', methods=['POST'])
def set_wallpaper():
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    filename = f.filename or ''
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in WP_EXTS:
        return jsonify({'error': 'unsupported format'}), 400
    ensure_data_dir()
    _remove_wallpapers()
    f.save(os.path.join(DATA_DIR, f'wallpaper.{ext}'))
    return jsonify({'ok': True})


@app.route('/wallpaper', methods=['DELETE'])
def delete_wallpaper():
    _remove_wallpapers()
    return jsonify({'ok': True})


# ─── Server control ──────────────────────────────────────────────────────────
# Exit 42 = restart requested (systemd's Restart=on-failure brings it back);
# exit 0  = stop (systemd leaves it stopped).

@app.route('/server/restart', methods=['POST'])
def server_restart():
    def do_restart():
        time.sleep(0.4)
        os._exit(42)
    threading.Thread(target=do_restart, daemon=False).start()
    return jsonify({'ok': True})


@app.route('/server/stop', methods=['POST'])
def server_stop():
    def do_stop():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=do_stop, daemon=False).start()
    return jsonify({'ok': True})


# ─── SSE stream ──────────────────────────────────────────────────────────────

@app.route('/stream')
def stream():
    q: queue.Queue = queue.Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f'data: {json.dumps(data)}\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    init_db()
    get_token()
    t = threading.Thread(target=listen_dbus, daemon=True, name='dbus-listener')
    t.start()
    shown = 'localhost' if HOST in ('127.0.0.1', '::1', 'localhost') else HOST
    log(f'Notification Manager → http://{shown}:{PORT}  (data: {DATA_DIR})')
    if HOST not in ('127.0.0.1', '::1', 'localhost'):
        log(f'WARNING: listening on {HOST}; anyone who can reach this port and knows an '
            f'allowed host name can read your notifications.')
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == '__main__':
    main()
