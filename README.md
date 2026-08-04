# The Isle Manager

A self-hosted Docker panel for running multiple isolated The Isle dedicated-server instances. It includes a React/Tailwind dashboard, FastAPI REST/WebSocket API, PostgreSQL persistence, SteamCMD lifecycle actions, backups, mod/plugin management, role-based access, and server resource limits.

> Confirm the current dedicated-server app ID and SteamCMD account requirements from The Isle's official documentation before first deployment. The default `STEAM_APP_ID=412680` is configurable because publisher distribution can change.

## Install on Debian or Ubuntu

Install Docker Engine and the Docker Compose plugin, then clone or copy the project to the server. From the project folder run:

```bash
sudo bash scripts/install.sh
```

The installer copies the application to `/opt/theisle-manager`, generates unique PostgreSQL/JWT secrets in `.env`, builds images, starts the stack, and prints the panel URL. Open `http://SERVER-IP:8080` and create the first Owner account.

For a manual deployment, copy `.env.example` to `.env`, replace the password and JWT placeholders with long random values, then run:

```bash
docker compose up -d --build
```

`server-image` is a one-shot Compose build target that produces `theisle-manager-server:latest`; it exits successfully after that build. Confirm all services with `docker compose ps`.

## CasaOS

1. In CasaOS, choose **App Store → Custom Install** and import this repository's `docker-compose.yml`.
2. Enter strong values for `POSTGRES_PASSWORD` and `JWT_SECRET`.
3. Change `PANEL_PORT` if necessary and install.
4. Open the CasaOS app card and create the first Owner account.

The root compose file includes CasaOS metadata and the bundled [app icon](casaos/icon.svg). Named Docker volumes keep application data between CasaOS upgrades.

## Architecture

| Container | Purpose |
| --- | --- |
| `theisle-manager-web` | Nginx serving the React/Tailwind dashboard and proxying API/WebSocket traffic. |
| `theisle-manager-api` | FastAPI control plane, data access, authentication, backups, file operations, and Docker orchestration. |
| `theisle-manager-db` | PostgreSQL 16 persistent database. |
| `server-image` | Builds the reusable SteamCMD The Isle server image. |

Each managed instance gets an isolated container, game/query ports, memory and CPU limits, PID limit, restart policy, JSON log rotation, and its own `Saved`, `Config`, `Logs`, `Mods`, `Plugins`, and `Backups` folders.

## Server operations

Create a server from the dashboard with its name, description, version label, ports, player limit, RAM/CPU/disk limits, region, password, and map. The panel validates ports among its managed servers.

- **Install** downloads the configured app through SteamCMD.
- **Start**, **Stop**, and **Restart** control only that server's container.
- **Update** creates a pre-update archive, runs SteamCMD, and restores the archive automatically if SteamCMD fails.
- **Verify** runs SteamCMD validation.
- Dashboard metrics show container CPU/RAM/network counters, disk usage, parsed player count when the game log provides it, map, uptime, status, and version label.

Console input is deliberately limited to `restart`, `stop`, `save`, `broadcast`, `kick`, and `ban`. The last four use standard RCON when the game server enables it and its details are entered in **Configuration → Networking**. The panel never sends console text to a shell.

## Configuration, mods, and plugins

The configuration screen contains forms for game password, player count, gameplay multipliers, admins/moderators, map/weather/time, networking/RCON, and automatic backup frequency. Saving writes panel configuration and generated `.ini` data; manual `.ini` editing is not required. Changing game/query ports recreates the game container, while other runtime changes should be followed by a normal restart.

The **Mods** and **Plugins** tabs manage their own folders. Upload, enable/disable (disabled files retain a `.disabled` suffix), or delete files. Mods can also be installed by Steam Workshop ID when the current dedicated-server app makes compatible Workshop content available.

## Backup and recovery

Per-server backups archive `Saved`, `Config`, `Mods`, and `Plugins`. Create them manually or choose hourly, daily, or weekly automatic backups. Restore stops the server first and rejects unsafe archive paths.

Create a portable full-panel backup (database, manager data, and environment file) with:

```bash
sudo bash scripts/backup.sh
```

Timestamped output is placed under `backups/`. Protect it: it includes encrypted credentials and `.env` key material.

## Update and health checks

From a Git checkout:

```bash
sudo bash scripts/update.sh
sudo bash scripts/healthcheck.sh
```

The update script first creates a full panel backup, fast-forwards the checkout, rebuilds images, and restarts Compose. For another release approach, preserve `.env` and Docker volumes, replace the application code, then run `docker compose up -d --build`.

## Security

- First-run setup creates the Owner; Owners may add Admin, Moderator, and Viewer accounts.
- User passwords use bcrypt. Game/RCON credentials are encrypted at rest using a key derived from required `JWT_SECRET`; normal server responses omit them.
- JWT authentication protects API endpoints and console WebSockets.
- Upload paths are sanitized and folder-limited. Restore validates archive paths.
- The API container has Docker socket access because isolated server creation needs it. Treat Owner access as Docker-host administration.

Do not expose this panel directly to the public internet. Place it behind a TLS reverse proxy or VPN. Rotating `JWT_SECRET` requires re-entering game/RCON credentials.

## Troubleshooting

| Problem | Resolution |
| --- | --- |
| Panel unavailable | Run `docker compose ps`, then `docker compose logs web api`; open the configured panel port in the firewall. |
| Server will not start | Check Console output and confirm the Steam app ID and SteamCMD access. First installation may take time. |
| Game image missing | Run `docker compose up -d --build`; the API waits for the build job. |
| Port conflict | Use unused ports. The panel checks its own instances but cannot reserve unrelated host ports. |
| Workshop fails | Verify app/item compatibility and anonymous SteamCMD access. |
| RCON fails | Enable the dedicated server's RCON listener and enter exact host/port/password under Networking. |
| API reference | Open `/api/docs` on the panel host. |

Project folders: `backend/` (FastAPI), `frontend/` (React/TypeScript/Tailwind), `docker/theisle-server/` (SteamCMD server image), `scripts/` (operations), and `casaos/` (metadata asset). Generated `.env`, frontend output, logs, and runtime data are ignored by Git.
