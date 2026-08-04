# The Isle Manager hardening plan

This release turns the initial full-platform implementation into a safer operational baseline.

## Completed in this branch

1. **Large file handling** — Mod and plugin uploads are streamed in 1 MB chunks to a staging area, bounded by a configurable size limit, and then moved into the server folder only after the disk policy check passes.
2. **Backup recovery safeguards** — Restore validates folders, archive encryption, file count, expanded size, and archive paths before writing any file.
3. **Discord integration** — Each server can use one encrypted Discord webhook, choose the event groups it receives, and send non-blocking notices for lifecycle actions, updates, recovery events, and Workshop installs.
4. **Automatic game updates** — Per-server daily/weekly SteamCMD scheduling creates a recovery archive, stops and updates the server, restores the previous run state, and sends an optional Discord outcome notice.
5. **Responsiveness and protocol robustness** — Scheduled archive/SteamCMD work runs outside the API event loop, and RCON packet reads now handle TCP fragmentation safely.

## Operator checklist

- Confirm the The Isle dedicated-server app ID and launch arguments before installing a production server.
- Set strong POSTGRES_PASSWORD and JWT_SECRET values, and place the panel behind a VPN or TLS reverse proxy.
- Set per-server storage limits that accommodate game content plus at least one recovery archive.
- Test a backup restore and, if used, a Discord webhook before relying on scheduled updates.
