# Deployment

> A CI workflow (`.github/workflows/ci.yml`) builds the image and smoke-tests `/readyz`; it was added recently and has not run yet.

## Docker Compose (Ubuntu Server 24.04)
```bash
git clone <repo> vibevault && cd vibevault
cp .env.example .env && chmod 600 .env       # fill VIBEVAULT_ADMIN_TOKEN and VIBEVAULT_INGEST_TOKENS (openssl rand -hex 32)
docker compose up -d --build
docker compose ps        # healthy once /readyz returns 200 (migrations at head)
```
* Image: multi-stage (Node builds the UI, wheel build, slim runtime), runs as non-root uid 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges`, data in volume `/data` (mode 0700), `HEALTHCHECK` on `/readyz`.
* Migrations run automatically at start. For a manual step: `docker compose run --rm vibevault vibevault migrate`.
* The port is published on `127.0.0.1:8787` only. To use it from your PC via Tailscale, set `VIBEVAULT_BIND` to the server's Tailscale IP (or front it with `tailscale serve`); do not forward it to the internet. There is no TLS in the app; Tailscale provides encryption, or terminate TLS on your own proxy.
* Resource use is small (one Python process, SQLite). No inference runs on the server.

### External Reverse-Proxy Network (opt-in)
By default, VibeVault attaches only to Compose's `default` network and binds to `127.0.0.1`. If running behind an external reverse proxy (e.g. Caddy, Traefik, nginx), use the opt-in `docker-compose.proxy.yml` override:
```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```
This attaches the service to an external network named `proxy` (customizable via `VIBEVAULT_PROXY_NETWORK` in your `.env` or environment).

## Local development
See README. `VIBEVAULT_DATABASE_URL` defaults to `sqlite:///./data/vibevault.db`.

## Backup
Online, consistent (SQLite backup API, safe while running; verifies `integrity_check`, file mode 0600):
```bash
docker compose exec vibevault vibevault backup /data/backups/            # writes vibevault-<UTC>.db inside the volume
docker compose cp vibevault:/data/backups/vibevault-<UTC>.db /mnt/usb512/vibevault-backups/
```
(or `vibevault backup <dest>` when not using Docker). Schedule it with a systemd timer/cron, and copy to the external 512 GB drive; encrypt it if the drive leaves the house. **Backups contain secrets that slipped past redaction** — protect them like the live DB. Test a restore periodically.

## Restore
```bash
docker compose stop vibevault
docker compose run --rm vibevault vibevault restore /data/backups/vibevault-<UTC>.db
docker compose start vibevault        # applies newer migrations, if any
```
`restore` refuses a backup that fails `integrity_check`, and keeps the previous DB as `*.pre-restore-<UTC>`. Collectors' spools replay safely afterwards (events are idempotent), so a restore to an older backup is healed by the next collector sync as long as spools/transcripts still exist.

## Upgrade
`git pull && docker compose up -d --build` after taking a backup (migrations are forward-only in practice). Roll back = restore the pre-upgrade backup.

## Retention
Nothing is deleted by default. `vibevault prune --older-than-days 365` (dry-run) then `--yes`. See [privacy](privacy.md).

## Operations
Logs: JSON lines on stderr (`docker compose logs`), no payloads. Health: `/healthz`, `/readyz`. Queue/failures: UI → Processing. Single worker thread in-process; to separate it set `VIBEVAULT_WORKER_ENABLED=false` on the API and run `vibevault worker` elsewhere against the same database file (SQLite: same host only).
