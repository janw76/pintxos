# Running Pintxøs natively (without Docker)

This doc covers keeping a native, non-Docker install of Pintxøs running as a
background service — the thing Docker's `restart: unless-stopped` gives you
for free, a native install has to get from the OS instead. On Linux that's
systemd; on macOS it's launchd. For how to install Pintxøs and what each
environment variable does, see the main [README](../README.md) — this doc
only covers keeping it running.

## Before you start

Pick a working directory that will hold `.env` (if you use one) and the
`data/` directory the SQLite database lives in. Reasonable choices:

- Linux: `/var/lib/pintxos`
- macOS: `~/Library/Application Support/Pintxos`, or just `~/pintxos` if you'd
  rather keep it simple

`PINTXOS_DATA_DIR` defaults to `./data`, relative to whatever directory the
`pintxos` process is started from — so the working directory you pick above
matters. Point `PINTXOS_DATA_DIR` at an absolute path if you want to be
certain, especially once you're launching from a service manager rather than
a terminal.

By default the server binds to `127.0.0.1:8000`, i.e. it's only reachable
from the same machine. To reach it from another machine (including over
Tailscale) you must set `PINTXOS_HOST` to `0.0.0.0` or to a specific
interface address, such as your Tailscale IP. Before you do,
re-read the [security warning](../README.md#security-warning) in the
README: Pintxøs has no authentication, so anything that can reach it can
add, delete, or repoll feeds.

## systemd (Linux)

Create a dedicated system user and directories, install into a venv, and
write an environment file:

```bash
sudo useradd --system --home /var/lib/pintxos --shell /usr/sbin/nologin pintxos
sudo mkdir -p /var/lib/pintxos /etc/pintxos

sudo python3 -m venv /opt/pintxos/venv
sudo /opt/pintxos/venv/bin/pip install git+https://github.com/janw76/pintxos

sudo tee /etc/pintxos/env >/dev/null <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
PINTXOS_HOST=0.0.0.0
PINTXOS_DATA_DIR=/var/lib/pintxos/data
EOF
sudo chmod 600 /etc/pintxos/env
sudo chown root:root /etc/pintxos/env
sudo chown -R pintxos:pintxos /var/lib/pintxos
```

Then create `/etc/systemd/system/pintxos.service`:

```ini
[Unit]
Description=Pintxos RSS rewriter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pintxos
Group=pintxos
WorkingDirectory=/var/lib/pintxos
EnvironmentFile=/etc/pintxos/env
ExecStart=/opt/pintxos/venv/bin/pintxos
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/pintxos
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Because `EnvironmentFile` supplies the variables directly, you don't need a
`.env` file in the working directory — the `.env`-in-cwd mechanism `pintxos`
supports is only useful when you're launching it from a shell.

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pintxos
sudo systemctl status pintxos
sudo journalctl -u pintxos -f
```

(`systemd-analyze verify` isn't available outside Linux, so this unit was
checked by inspection rather than run through the verifier.)

## launchd (macOS)

Find the absolute path to the installed binary — you'll need it verbatim,
since `~` is not expanded inside a plist:

```bash
which pintxos
```

Create the logs directory and the agent plist at
`~/Library/LaunchAgents/ie.boboco.pintxos.plist` (replace `/Users/YOU` with
your home directory and the `ProgramArguments` path with the output of
`which pintxos` above):

```bash
mkdir -p ~/Library/Logs/pintxos
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ie.boboco.pintxos</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/pintxos</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/pintxos</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>sk-ant-...</string>
        <key>PINTXOS_HOST</key>
        <string>0.0.0.0</string>
        <key>PINTXOS_DATA_DIR</key>
        <string>/Users/YOU/pintxos/data</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/Library/Logs/pintxos/pintxos.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/Library/Logs/pintxos/pintxos.err.log</string>
</dict>
</plist>
```

Load and manage it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ie.boboco.pintxos.plist
launchctl print gui/$(id -u)/ie.boboco.pintxos

# Stop:
launchctl bootout gui/$(id -u)/ie.boboco.pintxos

# Restart after an upgrade:
launchctl kickstart -k gui/$(id -u)/ie.boboco.pintxos
```

A LaunchAgent only runs while that user is logged in. If you need Pintxøs to
run on a Mac that's left logged out or is headless, use a LaunchDaemon under
`/Library/LaunchDaemons` instead (root-owned, runs regardless of login state)
— see Apple's launchd documentation for the differences from a LaunchAgent.

## Tailscale

Bind Pintxøs to your Tailscale IP so it's reachable from your tailnet but
not your LAN or the public internet: run `tailscale ip -4` and set
`PINTXOS_HOST` to that address (use `0.0.0.0` instead if your host firewall
already restricts inbound access). From there you can either point RSS
readers at `http://<machine>:8000` directly within the tailnet, or use
`tailscale serve` or a [Tailscale service](https://tailscale.com/docs/features/tailscale-services)
to get a proper HTTPS URL — if you do, set `PINTXOS_BASE_URL` to that
`https://` URL so the links in generated output feeds are correct. See the
[Quickstart](../README.md#quickstart) section of the README for the
recommended Tailscale service approach.

## Upgrading

```bash
uv tool upgrade pintxos
# or, for a venv install:
/opt/pintxos/venv/bin/pip install --upgrade git+https://github.com/janw76/pintxos
```

Then restart the service:

```bash
sudo systemctl restart pintxos          # systemd
launchctl kickstart -k gui/$(id -u)/ie.boboco.pintxos   # launchd
```

The SQLite database lives under `PINTXOS_DATA_DIR` and isn't touched by the
upgrade itself, but back it up before upgrading anyway, in case a schema
migration is involved.
