# Service on this machine

The Python environment is `/root/token-tv/.venv`. The installed systemd unit
is `/etc/systemd/system/token-tv.service`; its source is
`deploy/token-tv.service`. It runs as root to use the existing CLI logins in
root's home directory. The dashboard binds to `192.168.2.240:8788`.

Account metadata is stored in `/root/.config/token-tv/config.json`. Runtime
snapshots and original clock display backups are stored in `/var/lib/token-tv`.
Account and clock configuration changes require a service restart.

The configured displays are `10.0.0.128` (SD_PRO) and `10.0.0.175` (SmallTV-PRO,
stock V3.3.75EN). Both use the selected HUD face. Firmware is detected automatically;
the PRO uses Picture theme `4` and a one-image slideshow. TokenTV deletes other album
images after verifying its uploaded image. Existing photos are not retained or backed up.

Codex uses the existing login in `~/.codex`. Gemini uses the existing
Antigravity (`agy`) login. On this Linux machine, TokenTV reads the email from
agy's startup banner in a temporary terminal, exits without sending a prompt,
then fetches `/usage` only if that identity matches the configured account.
The repository workspace has been trusted in agy for this startup check.

If no account configuration exists yet, create it with the actual account email:

```bash
/root/token-tv/.venv/bin/token-tv setup --yes --codex-email YOUR_CHATGPT_EMAIL
```

Use `--claude-email`, `--grok-email`, or `--gemini-email` for other providers.
Add `--device-url http://CLOCK_IP` to configure a physical clock. Repeated email
flags configure additional accounts. Setup never overwrites an existing config.

The service is enabled at boot. Launch and inspect it with:

```bash
systemctl start token-tv.service
systemctl status token-tv.service
journalctl -u token-tv.service -n 50 --no-pager
```

Open http://192.168.2.240:8788 after starting it. This dashboard serves live
readings only after the configured account has a working CLI login.

After editing the unit source, reinstall and reload it:

```bash
install -m 0644 /root/token-tv/deploy/token-tv.service /etc/systemd/system/token-tv.service
systemctl daemon-reload
systemctl restart token-tv.service
```
