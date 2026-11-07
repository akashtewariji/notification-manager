# Notification Manager

Never lose a desktop notification again. Notification Manager quietly records every
notification shown on your Linux desktop (Slack, Firefox, email, build scripts, anything
that uses `notify-send`) into a local SQLite database, and gives you a searchable web UI
to review them, tick them off and add notes.

Everything stays on your machine: no cloud, no accounts, no telemetry.

## Features

- Captures every notification sent through the freedesktop `org.freedesktop.Notifications`
  D-Bus interface, including multi-line bodies
- Correct app names for snap/Flatpak apps that notify through the desktop portal
  (e.g. Slack), using the `desktop-entry` hint
- Live updates in the browser (Server-Sent Events), no refresh needed
- Search, filter by app, filter by date range, sort
- Mark notifications as done and attach a comment to each
- Custom wallpaper, colours and font size
- Runs as a systemd user service: starts with your session, restarts if it crashes

## Requirements

- Linux desktop with a D-Bus session bus (GNOME, KDE, XFCE, …)
- Python 3.10 or newer, with `venv`
- `dbus-monitor` (package `dbus` on Debian/Ubuntu, `dbus-tools` on Fedora)
- systemd user session (optional — see `--no-service`)

## Install

```bash
git clone https://github.com/akashtewariji/notification-manager.git
cd notification-manager
./install.sh
```

Then open <http://localhost:5000>.

The installer copies the app to `~/.local/share/notification-manager/app`, creates a
Python virtual environment there, and installs and starts the
`notification-manager` systemd user service.

| Option / variable | Effect |
|---|---|
| `NM_PORT=5050 ./install.sh` | Use a different port |
| `NM_INSTALL_DIR=… ./install.sh` | Install the code somewhere else |
| `./install.sh --no-service` | Install only; start it yourself |

To **update**, `git pull` and run `./install.sh` again. Your data is never touched.

## Uninstall

```bash
./uninstall.sh           # remove the service and the app, keep your recorded notifications
./uninstall.sh --purge   # also delete all recorded notifications
```

## Where your data lives

`~/.local/share/notification-manager/` (or `$XDG_DATA_HOME/notification-manager/`):

| File | Contents |
|---|---|
| `notifications.db` | Every recorded notification: app, title, **full body text**, your notes |
| `wallpaper.*` | Your chosen background image |
| `token` | Random secret that protects the web UI (see Security) |

The folder is created with `700` permissions and the files with `600`. **The database
contains the full text of everything that has popped up on your screen**, including chat
message previews. Treat it like your chat history: don't commit it, share it or back it up
somewhere public.

## Configuration

Set these in the service with `systemctl --user edit notification-manager`
(add `[Service]` and `Environment=NAME=value` lines), or in the environment when running
`app.py` directly.

| Variable | Default | Meaning |
|---|---|---|
| `NM_HOST` | `127.0.0.1` | Address to listen on |
| `NM_PORT` | `5000` | Port |
| `NM_DATA_DIR` | `~/.local/share/notification-manager` | Database, wallpaper, token |
| `NM_ALLOWED_HOSTS` | *(empty)* | Extra comma-separated host names the UI may be reached by |

## Security

The UI is a web server on your machine, so it is built to be safe from other websites you
visit and from other machines on your network:

- **Local only by default.** It listens on `127.0.0.1`, so nothing else on your network can
  reach it.
- **No access from other websites.** Requests whose `Host` header is not `localhost`,
  `127.0.0.1` or `[::1]` are refused. This blocks DNS-rebinding attacks, where a malicious
  site points its own domain at `127.0.0.1` to read your notifications.
- **No cross-site actions.** Every request that changes something (mark done, comment,
  wallpaper, restart, stop) must carry a secret token that only the real page has.
  Requests with a foreign `Origin` are refused, and no CORS headers are ever sent.
- **Uploads are limited** to images of up to 10 MB.
- **Strict browser headers:** Content-Security-Policy, no framing, `nosniff`, no referrer.

There is **no login**. Anyone with access to your user account can read the database
anyway. If you set `NM_HOST=0.0.0.0` to reach the UI from another device, anyone who can
reach the port can read your notifications. Only do that on a network you trust, and add
the host name you will use to `NM_ALLOWED_HOSTS`.

Flask's built-in server is used on purpose: this is a single-user local tool, not a public
web service. The "development server" warning in the logs is expected.

## Troubleshooting

**Is it running?**

```bash
systemctl --user status notification-manager
journalctl --user -u notification-manager -f
```

**Is it actually capturing?** Send a test notification and check that it appears in the UI:

```bash
notify-send "Notification Manager" "test $(date +%T)"
```

If the UI loads but nothing new shows up, restart the service
(`systemctl --user restart notification-manager`) and check the logs for `dbus-monitor` errors.

**Snap-confined terminals.** A `notify-send` run from inside a snap sandbox may be delivered
through the desktop portal and show up with the terminal's name. To test from outside the
sandbox use `systemd-run --user --wait --pipe notify-send "test" "body"`.

**Port already in use.** Reinstall with `NM_PORT=5050 ./install.sh`.

## How it works

```
apps ──Notify()──▶ D-Bus session bus ──▶ dbus-monitor ──stdout──▶ app.py
                                                                   ├─ parse + save → SQLite
                                                                   └─ push → browser (SSE)
browser ◀── Flask (127.0.0.1:5000): page, JSON API, live stream
```

`app.py` runs `dbus-monitor` as a child process, parses each `Notify` call from its text
output, stores it in SQLite and pushes it to any open browser tab. The UI is a single
HTML page (`templates/index.html`) with plain JavaScript, no build step.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
NM_DATA_DIR=/tmp/nm-dev NM_PORT=5050 .venv/bin/python app.py
```

The tests cover the `dbus-monitor` parser (with recorded sample output in
`tests/fixtures/`) and the security checks. When run directly rather than under
systemd, the UI's Restart button only stops the process.

## License

MIT. See [LICENSE](LICENSE).
