import io
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from sqlalchemy.orm import Session

from .config import settings
from .models import Backup, GameServer
from .secrets import decrypt


SAFE_FILE = re.compile(r"[^A-Za-z0-9._-]+")


class ManagerError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerServerManager:
    """All Docker interaction is centralized here to keep API handlers safe and auditable."""

    def __init__(self) -> None:
        self.settings = settings()
        try:
            self.client = docker.from_env()
        except Exception as exc:
            raise ManagerError(f"Docker connection unavailable: {exc}") from exc

    def server_dir(self, server_id: str) -> Path:
        return self.settings.data_dir / "servers" / server_id

    def _host_data_dir(self) -> Path:
        """Find the host source of /data by inspecting this manager container.

        This works with named volumes on CasaOS, Docker Desktop, and standard Compose.
        A bare-metal run can set MANAGER_HOST_DATA_PATH explicitly.
        """
        configured = os.getenv("MANAGER_HOST_DATA_PATH")
        if configured:
            return Path(configured)
        try:
            current = self.client.containers.get(socket.gethostname())
            for mount in current.attrs.get("Mounts", []):
                if mount.get("Destination") == "/data" and mount.get("Source"):
                    return Path(mount["Source"])
        except Exception:
            pass
        raise ManagerError(
            "Unable to resolve manager storage on the Docker host. "
            "Set MANAGER_HOST_DATA_PATH to the absolute host path mounted at /data."
        )

    def _manager_network(self) -> str | None:
        """Place managed servers on the panel network for private RCON access."""
        try:
            current = self.client.containers.get(socket.gethostname())
            networks = current.attrs.get("NetworkSettings", {}).get("Networks", {})
            return next(iter(networks), None)
        except Exception:
            return None

    def _container_name(self, server: GameServer) -> str:
        return f"mk-panel-{server.game_type}-{server.id[:12]}"

    def _container(self, server: GameServer):
        try:
            return self.client.containers.get(self._container_name(server))
        except NotFound:
            return None

    def prepare_server(self, server: GameServer) -> None:
        root = self.server_dir(server.id)
        root.mkdir(parents=True, exist_ok=True)
        # The game image runs unprivileged as its own UID. A permissive per-server
        # directory avoids assuming UID/GID values from the SteamCMD base image;
        # it is still isolated by a unique bind mount per server container.
        root.chmod(0o777)
        directories = ("mods", "config", "defaultconfigs", "plugins", "Backups") if server.game_type == "minecraft" else ("Saved", "Config", "Logs", "Mods", "Plugins", "Backups")
        for directory in directories:
            path = root / directory
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)
        if server.game_type == "theisle":
            # SteamCMD runs as an unprivileged user and must create the game tree
            # beside this panel-managed configuration directory.
            game_root = root / "TheIsle"
            saved_config = game_root / "Saved" / "Config" / "LinuxServer"
            saved_config.mkdir(parents=True, exist_ok=True)
            for directory in (game_root, game_root / "Saved", game_root / "Saved" / "Config", saved_config):
                directory.chmod(0o777)
        self.write_config(server)

    def write_config(self, server: GameServer) -> None:
        root = self.server_dir(server.id)
        config = server.config or {}
        general = config.get("general", {})
        networking = config.get("networking", {})
        if server.game_type == "minecraft":
            minecraft = config.get("minecraft", {})
            properties = [
                f"server-port={server.game_port}", f"max-players={general.get('max_players', server.max_players)}",
                f"motd={general.get('server_name', server.name)}", f"level-name={minecraft.get('level_name', 'world')}",
                f"level-seed={minecraft.get('seed', '')}", f"gamemode={minecraft.get('gamemode', 'survival')}",
                f"difficulty={minecraft.get('difficulty', 'normal')}", f"online-mode={str(bool(minecraft.get('online_mode', True))).lower()}",
                f"pvp={str(bool(minecraft.get('pvp', True))).lower()}",
                "enable-rcon=true", f"rcon.port={networking.get('rcon_port', server.query_port)}",
                f"rcon.password={decrypt(networking.get('rcon_password_encrypted', networking.get('rcon_password', '')))}",
            ]
            (root / "server.properties").write_text("\n".join(properties) + "\n", encoding="utf-8")
            (root / "Config").mkdir(parents=True, exist_ok=True)
            (root / "Config" / "panel-config.json").write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
            return
        (root / "Config").mkdir(parents=True, exist_ok=True)
        gameplay = config.get("gameplay", {})
        admins = config.get("admins", {})
        world = config.get("world", {})
        rcon_password = decrypt(networking.get("rcon_password_encrypted", networking.get("rcon_password", "")))
        lines = [
            "[/Script/TheIsle.TIGameSession]",
            f"ServerName={general.get('server_name', server.name)}",
            f"MapName={world.get('map', 'Gateway')}",
            f"MaxPlayerCount={general.get('max_players', server.max_players)}",
            f"bServerPassword={str(bool(decrypt(server.password))).lower()}",
            f"ServerPassword={decrypt(server.password)}",
            f"QueryPort={networking.get('query_port', server.query_port)}",
            f"bRconEnabled={str(bool(rcon_password)).lower()}", f"RconPort={networking.get('rcon_port', 8888)}", f"RconPassword={rcon_password}",
            f"bSpawnAI={str(bool(gameplay.get('spawn_ai', True))).lower()}",
            f"GrowthMultiplier={gameplay.get('growth_rate', 1.0)}",
            f"AdminsSteamIDs={','.join(str(x) for x in admins.get('admin_ids', []))}",
        ]
        ini_path = root / "Config" / "TheIsleManager.ini"
        ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config_path = root / "Config" / "panel-config.json"
        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True), encoding="utf-8"
        )
        ini_path.chmod(0o666)
        config_path.chmod(0o666)
        game_ini_path = root / "TheIsle" / "Saved" / "Config" / "LinuxServer" / "Game.ini"
        game_ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        game_ini_path.chmod(0o666)

    def ensure_container(self, server: GameServer):
        self.assert_disk_budget(server)
        container = self._container(server)
        if container:
            return container
        self.prepare_server(server)
        source = self._host_data_dir() / "servers" / server.id
        network = self._manager_network()
        # Resource values deliberately come from validated numeric fields only.
        try:
            if server.game_type == "minecraft":
                minecraft = (server.config or {}).get("minecraft", {})
                networking = (server.config or {}).get("networking", {})
                return self.client.containers.create(
                    image=self.settings.minecraft_image,
                    name=self._container_name(server), detach=True,
                    labels={"io.theisle.manager": "true", "io.theisle.server-id": server.id, "io.mk-panel.game": "minecraft"},
                    environment={
                        "EULA": "TRUE", "TYPE": str(minecraft.get("server_type", "FABRIC")).upper(),
                        "VERSION": str(minecraft.get("minecraft_version", "LATEST")), "SERVER_PORT": str(server.game_port),
                        "MAX_PLAYERS": str(server.max_players), "MEMORY": f"{max(1024, server.ram_limit_mb - 512)}M",
                        "MOTD": server.name, "LEVEL": str(minecraft.get("level_name", "world")),
                        "SEED": str(minecraft.get("seed", "")), "MODE": str(minecraft.get("gamemode", "survival")),
                        "DIFFICULTY": str(minecraft.get("difficulty", "normal")), "ONLINE_MODE": str(bool(minecraft.get("online_mode", True))).upper(),
                        "PVP": str(bool(minecraft.get("pvp", True))).upper(), "ENABLE_RCON": "TRUE",
                        "RCON_PORT": str(networking.get("rcon_port", server.query_port)),
                        "RCON_PASSWORD": decrypt(networking.get("rcon_password_encrypted", networking.get("rcon_password", ""))),
                    },
                    volumes={str(source): {"bind": "/data", "mode": "rw"}}, ports={f"{server.game_port}/tcp": server.game_port},
                    mem_limit=f"{server.ram_limit_mb}m", nano_cpus=int((server.cpu_limit / 100) * 1_000_000_000), pids_limit=2048,
                    restart_policy={"Name": "unless-stopped"}, security_opt=["no-new-privileges:true"], cap_drop=["ALL"],
                    log_config={"type": "json-file", "config": {"max-size": "20m", "max-file": "5"}}, **({"network": network} if network else {}),
                )
            return self.client.containers.create(
                image=self.settings.server_image,
                name=self._container_name(server),
                detach=True,
                labels={"io.theisle.manager": "true", "io.theisle.server-id": server.id, "io.mk-panel.game": "theisle"},
                environment={
                    "STEAM_APP_ID": server.steam_app_id,
                    "STEAM_BRANCH": self.settings.steam_branch,
                    "SERVER_NAME": server.name,
                    "SERVER_PORT": str(server.game_port),
                    "QUERY_PORT": str(server.query_port),
                    "MAX_PLAYERS": str(server.max_players),
                    "SERVER_PASSWORD": decrypt(server.password),
                    "SERVER_MAP": str((server.config or {}).get("world", {}).get("map", "Gateway")),
                },
                volumes={str(source): {"bind": "/server", "mode": "rw"}},
                ports={
                    f"{server.game_port}/udp": server.game_port,
                    f"{server.game_port}/tcp": server.game_port,
                    f"{server.query_port}/udp": server.query_port,
                },
                mem_limit=f"{server.ram_limit_mb}m",
                nano_cpus=int((server.cpu_limit / 100) * 1_000_000_000),
                pids_limit=2048,
                # Do not hammer SteamCMD after a failed install. Three retries cover
                # short Steam outages, then the container stays stopped so its log
                # remains useful and the administrator can retry deliberately.
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
                log_config={"type": "json-file", "config": {"max-size": "20m", "max-file": "5"}},
                **({"network": network} if network else {}),
            )
        except ImageNotFound as exc:
            raise ManagerError(
                f"Game-server image '{self.settings.server_image}' is missing. Run docker compose up -d --build."
            ) from exc
        except DockerException as exc:
            raise ManagerError(f"Could not create Docker server container: {getattr(exc, 'explanation', str(exc))}") from exc

    def start(self, server: GameServer) -> None:
        self.assert_disk_budget(server)
        # Repair permissions for existing server storage as well as new servers.
        self.prepare_server(server)
        container = self.ensure_container(server)
        container.reload()
        if container.status != "running":
            container.start()

    def stop(self, server: GameServer) -> None:
        container = self._container(server)
        if container:
            container.reload()
            if container.status == "running":
                container.stop(timeout=30)

    def restart(self, server: GameServer) -> None:
        container = self.ensure_container(server)
        container.reload()
        if container.status == "running":
            container.restart(timeout=30)
        else:
            container.start()

    def delete(self, server: GameServer, delete_files: bool = False) -> None:
        container = self._container(server)
        if container:
            container.remove(force=True)
        if delete_files:
            shutil.rmtree(self.server_dir(server.id), ignore_errors=True)

    def status(self, server: GameServer) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "offline", "uptime_seconds": 0, "version": server.version,
            "player_count": self.player_count(server), "map": (server.config or {}).get("world", {}).get("map", "Gateway"),
            "cpu_percent": 0.0, "ram_bytes": 0, "ram_limit_bytes": server.ram_limit_mb * 1024 * 1024,
            "disk_bytes": self.directory_size(self.server_dir(server.id)),
            "disk_limit_bytes": server.disk_limit_mb * 1024 * 1024,
            "network_rx_bytes": 0, "network_tx_bytes": 0,
            "exit_code": None, "oom_killed": False, "restart_count": 0, "error_message": "",
        }
        container = self._container(server)
        if not container:
            return result
        try:
            container.reload()
            result["status"] = container.status
            state = container.attrs.get("State", {})
            result["exit_code"] = state.get("ExitCode")
            result["oom_killed"] = bool(state.get("OOMKilled", False))
            result["restart_count"] = int(container.attrs.get("RestartCount", 0))
            result["error_message"] = state.get("Error", "")
            started = state.get("StartedAt", "")
            if container.status == "running" and started and not started.startswith("0001"):
                try:
                    result["uptime_seconds"] = max(0, int(time.time() - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()))
                except ValueError:
                    pass
                stats = container.stats(stream=False)
                cpu_stats, pre_cpu = stats.get("cpu_stats", {}), stats.get("precpu_stats", {})
                cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - pre_cpu.get("cpu_usage", {}).get("total_usage", 0)
                system_delta = cpu_stats.get("system_cpu_usage", 0) - pre_cpu.get("system_cpu_usage", 0)
                online = max(1, len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])))
                result["cpu_percent"] = round((cpu_delta / system_delta) * online * 100, 2) if system_delta > 0 else 0
                mem = stats.get("memory_stats", {})
                result["ram_bytes"] = max(0, mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0))
                result["ram_limit_bytes"] = mem.get("limit", result["ram_limit_bytes"])
                for interface in stats.get("networks", {}).values():
                    result["network_rx_bytes"] += interface.get("rx_bytes", 0)
                    result["network_tx_bytes"] += interface.get("tx_bytes", 0)
        except (APIError, NotFound) as exc:
            result["status"] = "unknown"
            result["error"] = str(exc)
        return result

    def logs(self, server: GameServer, tail: int = 500) -> str:
        container = self._container(server)
        if container:
            try:
                return container.logs(tail=max(1, min(tail, 5000)), timestamps=True).decode("utf-8", errors="replace")
            except APIError:
                return ""
        log_file = self.server_dir(server.id) / "Logs" / "latest.log"
        if log_file.exists():
            return "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:])
        return ""

    def player_count(self, server: GameServer) -> int:
        log = self.logs(server, tail=300)
        matches = re.findall(r"(?:Players|PlayerCount)\s*[:=]\s*(\d+)", log, flags=re.IGNORECASE)
        return int(matches[-1]) if matches else 0

    @staticmethod
    def directory_size(path: Path) -> int:
        try:
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        except OSError:
            return 0

    def assert_disk_budget(self, server: GameServer, additional_bytes: int = 0) -> None:
        used = self.directory_size(self.server_dir(server.id)) + additional_bytes
        limit = server.disk_limit_mb * 1024 * 1024
        if used > limit:
            raise ManagerError(
                f"Disk policy exceeded ({used // (1024 * 1024)} MB used; limit is {server.disk_limit_mb} MB). "
                "Increase the server disk limit or remove files/backups before continuing."
            )

    def run_steam_action(self, server: GameServer, action: str, workshop_id: str | None = None) -> str:
        if server.game_type != "theisle":
            raise ManagerError("SteamCMD actions apply only to The Isle. Minecraft updates when its container starts.")
        if action not in {"install", "update", "validate", "workshop"}:
            raise ManagerError("Unsupported Steam action")
        source = self._host_data_dir() / "servers" / server.id
        self.assert_disk_budget(server)
        env = {"SERVER_ACTION": action, "STEAM_APP_ID": server.steam_app_id, "STEAM_BRANCH": self.settings.steam_branch}
        if workshop_id:
            env["WORKSHOP_ID"] = workshop_id
        try:
            output = self.client.containers.run(
                image=self.settings.server_image,
                remove=True,
                detach=False,
                environment=env,
                volumes={str(source): {"bind": "/server", "mode": "rw"}},
                network_mode="bridge",
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
            ).decode("utf-8", errors="replace")
            self.assert_disk_budget(server)
            return output
        except DockerException as exc:
            raise ManagerError(f"SteamCMD action failed: {getattr(exc, 'explanation', str(exc))}") from exc

    def create_backup(self, db: Session, server: GameServer, name: str | None, kind: str = "manual") -> Backup:
        root = self.server_dir(server.id)
        if not root.exists():
            raise ManagerError("Server storage does not exist")
        safe_name = SAFE_FILE.sub("-", name or f"{server.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}").strip(".-")
        if not safe_name:
            safe_name = f"backup-{int(time.time())}"
        output_dir = self.settings.data_dir / "backups" / server.id
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{safe_name}.zip"
        suffix = 2
        while path.exists():
            path = output_dir / f"{safe_name}-{suffix}.zip"
            suffix += 1
        include = ("world", "mods", "config", "defaultconfigs", "plugins", "server.properties") if server.game_type == "minecraft" else ("TheIsle", "Config", "Mods", "Plugins")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for folder in include:
                folder_path = root / folder
                if folder_path.exists():
                    if folder_path.is_file():
                        archive.write(folder_path, folder_path.relative_to(root))
                    else:
                        for file in folder_path.rglob("*"):
                            if file.is_file():
                                archive.write(file, file.relative_to(root))
        backup = Backup(server_id=server.id, name=path.stem, path=str(path), size_bytes=path.stat().st_size, kind=kind)
        db.add(backup)
        db.commit()
        db.refresh(backup)
        return backup

    def restore_backup(self, server: GameServer, backup: Backup) -> None:
        archive_path = Path(backup.path)
        if not archive_path.is_file():
            raise ManagerError("Backup file no longer exists")
        root = self.server_dir(server.id)
        root_resolved = root.resolve()
        allowed_roots = {"world", "mods", "config", "defaultconfigs", "plugins", "server.properties"} if server.game_type == "minecraft" else {"TheIsle", "Config", "Mods", "Plugins"}
        max_bytes = min(self.settings.max_restore_bytes, server.disk_limit_mb * 1024 * 1024)
        total_bytes = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if len(members) > self.settings.max_backup_files:
                    raise ManagerError("Backup contains too many files")
                for member in members:
                    member_path = Path(member.filename)
                    if member.is_dir():
                        continue
                    if member.flag_bits & 0x1:
                        raise ManagerError("Encrypted backup archives are not supported")
                    if not member_path.parts or member_path.parts[0] not in allowed_roots:
                        raise ManagerError("Backup contains an unsupported file path")
                    target = (root / member_path).resolve()
                    if not target.is_relative_to(root_resolved):
                        raise ManagerError("Backup contains an unsafe file path")
                    total_bytes += member.file_size
                    if total_bytes > max_bytes:
                        raise ManagerError("Backup exceeds this server's restore size limit")

                self.stop(server)
                for member in members:
                    if member.is_dir():
                        continue
                    target = (root / Path(member.filename)).resolve()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ManagerError(f"Could not restore backup: {exc}") from exc

    def _upload_destination(self, server: GameServer, category: str, filename: str) -> tuple[Path, str]:
        if category not in {"Mods", "Plugins"}:
            raise ManagerError("Invalid upload category")
        safe_name = SAFE_FILE.sub("-", Path(filename).name).strip(".-")
        if not safe_name:
            raise ManagerError("Invalid filename")
        destination = self.category_dir(server, category) / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination, safe_name

    def category_dir(self, server: GameServer, category: str) -> Path:
        if server.game_type == "minecraft":
            return self.server_dir(server.id) / {"Mods": "mods", "Plugins": "plugins"}[category]
        return self.server_dir(server.id) / category

    def create_upload_staging_path(self, server: GameServer, category: str, filename: str) -> Path:
        _, safe_name = self._upload_destination(server, category, filename)
        uploads = self.settings.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f"{server.id}-{uuid.uuid4().hex}-{safe_name}.part"

    def commit_staged_upload(self, server: GameServer, category: str, filename: str, staging: Path) -> Path:
        if not staging.is_file():
            raise ManagerError("Uploaded file is missing")
        self.assert_disk_budget(server, staging.stat().st_size)
        destination, _ = self._upload_destination(server, category, filename)
        os.replace(staging, destination)
        return destination

    def import_minecraft_modpack(self, server: GameServer, archive_path: Path) -> int:
        """Safely import client-supplied modpack content without allowing zip-slip paths."""
        if server.game_type != "minecraft" or not archive_path.is_file():
            raise ManagerError("Minecraft modpack archive is missing")
        allowed = {"mods", "config", "defaultconfigs", "kubejs"}
        root = self.server_dir(server.id).resolve()
        count = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for item in archive.infolist():
                    member = Path(item.filename)
                    if item.is_dir():
                        continue
                    if item.flag_bits & 0x1 or not member.parts or member.parts[0] not in allowed:
                        continue
                    destination = (root / member).resolve()
                    if not destination.is_relative_to(root):
                        raise ManagerError("Modpack contains an unsafe file path")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    count += 1
        except zipfile.BadZipFile as exc:
            raise ManagerError("Minecraft modpack must be a valid .zip archive") from exc
        if not count:
            raise ManagerError("No mods, config, defaultconfigs, or kubejs files were found in this modpack")
        return count

    def list_files(self, server: GameServer, category: str) -> list[dict[str, Any]]:
        if category not in {"Mods", "Plugins"}:
            raise ManagerError("Invalid file category")
        directory = self.category_dir(server, category)
        if not directory.exists():
            return []
        return [
            {"name": item.name, "size_bytes": item.stat().st_size, "last_updated": datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(), "enabled": not item.name.endswith(".disabled")}
            for item in sorted(directory.iterdir(), key=lambda entry: entry.name.lower()) if item.is_file()
        ]

    def toggle_file(self, server: GameServer, category: str, name: str, enabled: bool) -> None:
        safe_name = Path(name).name
        if safe_name != name:
            raise ManagerError("Invalid filename")
        directory = self.category_dir(server, category)
        original = directory / safe_name
        disabled = directory / (safe_name[:-9] if safe_name.endswith(".disabled") else safe_name + ".disabled")
        if enabled and original.name.endswith(".disabled"):
            original.rename(disabled)
        elif not enabled and not original.name.endswith(".disabled"):
            original.rename(disabled)

    def remove_file(self, server: GameServer, category: str, name: str) -> None:
        path = self.category_dir(server, category) / Path(name).name
        if path.name != name or not path.is_file():
            raise ManagerError("File not found")
        path.unlink()

    def notify_discord(self, server: GameServer, event: str, message: str) -> None:
        """Send an optional, outbound-only Discord webhook notification.

        Webhook URLs are validated before saving and errors are intentionally non-fatal:
        an unavailable Discord endpoint must never interrupt server control actions.
        """
        discord = (server.config or {}).get("discord", {})
        if not isinstance(discord, dict) or not discord.get("enabled"):
            return
        events = discord.get("events", {})
        if not isinstance(events, dict) or not events.get(event, False):
            return
        webhook_url = decrypt(str(discord.get("webhook_url_encrypted", "")))
        if not webhook_url:
            return
        body = json.dumps({
            "username": "The Isle Manager",
            "content": f"**{server.name}** - {message}"[:1900],
            "allowed_mentions": {"parse": []},
        }).encode("utf-8")
        request = Request(webhook_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "TheIsleManager/1.1"})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=6) as response:
                if response.status not in {200, 204}:
                    return
        except (HTTPError, URLError, OSError):
            return

    def cleanup_logs(self) -> None:
        cutoff = time.time() - self.settings.log_retention_days * 86400
        for server_dir in (self.settings.data_dir / "servers").glob("*/Logs"):
            for item in server_dir.rglob("*"):
                try:
                    if item.is_file() and item.stat().st_mtime < cutoff:
                        item.unlink()
                except OSError:
                    continue

    def schedule_backups(self, db: Session) -> int:
        created = 0
        now = datetime.now(timezone.utc)
        for server in db.query(GameServer).all():
            schedule = (server.config or {}).get("backups", {}).get("schedule", "daily")
            hours = {"hourly": 1, "daily": 24, "weekly": 168}.get(schedule)
            if not hours:
                continue
            latest = db.query(Backup).filter(Backup.server_id == server.id, Backup.kind == schedule).order_by(Backup.created_at.desc()).first()
            if not latest or (now - latest.created_at) >= timedelta(hours=hours):
                self.create_backup(db, server, f"{schedule}-{now.strftime('%Y%m%d-%H%M%S')}", schedule)
                created += 1
        self.cleanup_logs()
        return created

    def schedule_updates(self, db: Session) -> int:
        """Apply configured game-server updates with a pre-update recovery point."""
        from .models import AuditEvent

        created = 0
        now = datetime.now(timezone.utc)
        intervals = {"daily": 24, "weekly": 168}
        for server in db.query(GameServer).all():
            automation = (server.config or {}).get("automation", {})
            schedule = automation.get("update_schedule", "off") if isinstance(automation, dict) else "off"
            hours = intervals.get(schedule)
            if not hours:
                continue
            latest = (
                db.query(AuditEvent)
                .filter(AuditEvent.target == server.id, AuditEvent.action == "servers.auto_update")
                .order_by(AuditEvent.created_at.desc())
                .first()
            )
            if latest and (now - latest.created_at) < timedelta(hours=hours):
                continue
            was_running = self.status(server)["status"] == "running"
            recovery_point: Backup | None = None
            try:
                recovery_point = self.create_backup(db, server, f"pre-auto-update-{now.strftime('%Y%m%d-%H%M%S')}", "pre-update")
                self.stop(server)
                output = self.run_steam_action(server, "update")
                if was_running:
                    self.start(server)
                db.add(AuditEvent(action="servers.auto_update", target=server.id, detail=f"schedule={schedule}; {output[-500:]}"))
                db.commit()
                self.notify_discord(server, "server_updated", f"scheduled {schedule} update completed.")
                created += 1
            except ManagerError as exc:
                if recovery_point:
                    try:
                        self.restore_backup(server, recovery_point)
                    except ManagerError:
                        pass
                if was_running:
                    try:
                        self.start(server)
                    except ManagerError:
                        pass
                db.add(AuditEvent(action="servers.auto_update_failed", target=server.id, detail=str(exc)[:500]))
                db.commit()
                self.notify_discord(server, "server_update_failed", f"scheduled update failed: {str(exc)[:300]}")
        return created

