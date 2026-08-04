import asyncio
import base64
import hashlib
import hmac
import json
import socket
import struct
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import jwt
from docker.errors import DockerException
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .manager import DockerServerManager, ManagerError
from .models import AuditEvent, Backup, GameServer, User
from .schemas import BackupCreate, ConfigPatch, ConsoleCommand, LoginRequest, ModInstall, ServerCreate, ServerPatch, SetupRequest, TokenOut, UserCreate, UserOut
from .secrets import decrypt, encrypt


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)
ROLE_PERMISSIONS = {
    "owner": {"*"},
    "admin": {"servers:manage", "console:access", "config:edit", "backups:manage", "users:manage", "mods:manage"},
    "moderator": {"console:access", "config:edit", "backups:manage", "mods:manage"},
    "viewer": set(),
}


DISCORD_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
DISCORD_EVENTS = {
    "server_created", "server_started", "server_stopped", "server_restarted", "server_installed",
    "server_updated", "server_update_failed", "backup_created", "backup_restored", "mod_installed",
}


def api_error(status: int, detail: str) -> None:
    raise HTTPException(status_code=status, detail=detail)


def validate_discord_webhook(value: str) -> str:
    parsed = urlparse(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in DISCORD_WEBHOOK_HOSTS
        or parsed.port not in {None, 443}
        or len(parts) < 4
        or parts[:2] != ["api", "webhooks"]
    ):
        api_error(422, "Enter a valid HTTPS Discord webhook URL")
    return value.strip()


def get_manager() -> DockerServerManager:
    try:
        return DockerServerManager()
    except ManagerError as exc:
        api_error(503, str(exc))


def make_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user.id, "role": user.role, "iat": now, "exp": now + timedelta(minutes=settings().jwt_ttl_minutes)},
        settings().jwt_secret,
        algorithm="HS256",
    )


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        api_error(401, "Authentication required")
    try:
        payload = jwt.decode(credentials.credentials, settings().jwt_secret, algorithms=["HS256"])
        user = db.get(User, payload.get("sub"))
    except jwt.PyJWTError:
        user = None
    if not user or not user.enabled:
        api_error(401, "Session is invalid or expired")
    return user


def require(permission: str):
    def check(user: Annotated[User, Depends(current_user)]) -> User:
        permissions = ROLE_PERMISSIONS.get(user.role, set())
        if "*" not in permissions and permission not in permissions:
            api_error(403, "Your role does not have permission for this action")
        return user
    return check


def audit(db: Session, actor: User | None, action: str, target: str, detail: str = "") -> None:
    db.add(AuditEvent(actor_id=actor.id if actor else None, action=action, target=target, detail=detail))
    db.commit()


def default_config(server: ServerCreate) -> dict[str, Any]:
    if server.game_type == "minecraft":
        return {
            "general": {"server_name": server.name, "max_players": server.max_players, "welcome_message": ""},
            "minecraft": {"server_type": "FABRIC", "minecraft_version": "LATEST", "online_mode": True, "difficulty": "normal", "gamemode": "survival", "seed": "", "level_name": "world", "pvp": True},
            "networking": {"port": server.game_port, "rcon_host": "", "rcon_port": server.query_port, "rcon_password": ""},
            "backups": {"schedule": "daily"}, "automation": {"update_schedule": "off"},
            "discord": {"enabled": False, "webhook_url": "", "events": {event: True for event in DISCORD_EVENTS}},
        }
    return {
        "general": {"server_name": server.name, "max_players": server.max_players, "welcome_message": ""},
        "gameplay": {"growth_rate": 1.0, "damage_multiplier": 1.0, "food_multiplier": 1.0, "water_multiplier": 1.0, "spawn_settings": {}},
        "admins": {"admin_ids": [], "moderators": [], "permissions": {}},
        "world": {"map": server.map_name, "weather": "dynamic", "time_settings": "default"},
        "networking": {"port": server.game_port, "query_port": server.query_port, "server_visibility": "public", "rcon_host": "", "rcon_port": 8888, "rcon_password": ""},
        "backups": {"schedule": "daily"},
        "automation": {"update_schedule": "off"},
        "discord": {
            "enabled": False,
            "webhook_url": "",
            "events": {event: True for event in DISCORD_EVENTS},
        },
    }


def server_payload(server: GameServer, manager: DockerServerManager | None = None) -> dict[str, Any]:
    public_config = json.loads(json.dumps(server.config or {}))
    public_config.get("general", {}).pop("server_password", None)
    public_config.get("networking", {}).pop("rcon_password", None)
    public_config.get("networking", {}).pop("rcon_password_encrypted", None)
    public_config.get("discord", {}).pop("webhook_url", None)
    public_config.get("discord", {}).pop("webhook_url_encrypted", None)
    result = {
        "id": server.id, "name": server.name, "description": server.description, "version": server.version, "game_type": server.game_type,
        "steam_app_id": server.steam_app_id, "game_port": server.game_port, "query_port": server.query_port,
        "max_players": server.max_players, "ram_limit_mb": server.ram_limit_mb, "cpu_limit": server.cpu_limit,
        "disk_limit_mb": server.disk_limit_mb, "region": server.region, "config": public_config,
        "created_at": server.created_at, "updated_at": server.updated_at,
    }
    if manager:
        result["metrics"] = manager.status(server)
    return result


def get_server(db: Session, server_id: str) -> GameServer:
    server = db.get(GameServer, server_id)
    if not server:
        api_error(404, "Server not found")
    return server


def source_rcon_command(host: str, port: int, password: str, command: str) -> str:
    """Execute Source-compatible RCON (used by Minecraft)."""
    def packet(request_id: int, kind: int, body: str) -> bytes:
        payload = struct.pack("<ii", request_id, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def receive_exact(connection: socket.socket, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise ManagerError("RCON connection closed before a complete response arrived")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def receive_packet(connection: socket.socket) -> bytes:
        length = struct.unpack("<i", receive_exact(connection, 4))[0]
        if length < 10 or length > 1024 * 1024:
            raise ManagerError("RCON returned an invalid packet length")
        return receive_exact(connection, length)

    with socket.create_connection((host, port), timeout=8) as conn:
        conn.sendall(packet(10, 3, password))
        answer = receive_packet(conn)
        request_id, _ = struct.unpack("<ii", answer[:8])
        if request_id == -1:
            raise ManagerError("RCON authentication failed")
        conn.sendall(packet(11, 2, command))
        answer = receive_packet(conn)
        return answer[8:-2].decode("utf-8", errors="replace")


def isle_rcon_command(host: str, port: int, password: str, command: str) -> str:
    """Execute The Isle Evrima's game-native RCON protocol, not Source RCON."""
    names = {"broadcast": 0x10, "announce": 0x10, "kick": 0x30, "ban": 0x20, "save": 0x50}
    parts = command.split(maxsplit=1)
    opcode = names.get(parts[0].lower())
    if opcode is None:
        raise ManagerError("This command is not supported by The Isle RCON")
    arguments = parts[1] if len(parts) > 1 else ""
    with socket.create_connection((host, port), timeout=8) as conn:
        conn.settimeout(8)
        conn.sendall(b"\x01" + password.encode("utf-8"))
        auth = conn.recv(4096).decode("utf-8", errors="replace")
        if "Password Accepted" not in auth:
            raise ManagerError("The Isle RCON authentication failed")
        conn.sendall(bytes((0x02, opcode)) + arguments.encode("utf-8") + b"\x00")
        return conn.recv(65536).decode("utf-8", errors="replace").strip() or "Command sent"


def run_scheduled_maintenance() -> None:
    db = SessionLocal()
    try:
        manager = DockerServerManager()
        manager.schedule_backups(db)
        manager.schedule_updates(db)
    finally:
        db.close()


async def automatic_maintenance(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            # SteamCMD and archive work can take minutes; keep the API event loop responsive.
            await asyncio.to_thread(run_scheduled_maintenance)
        except Exception:
            # The dashboard remains available even if Docker or a scheduled task is temporarily unavailable.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=900)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings().ensure_directories()
    Base.metadata.create_all(bind=engine)
    # Existing CasaOS installations predate multi-game support.  This lightweight
    # migration is safe to run repeatedly and preserves all current server records.
    if "game_type" not in {column["name"] for column in inspect(engine).get_columns("servers")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE servers ADD COLUMN game_type VARCHAR(24) NOT NULL DEFAULT 'theisle'"))
    stop = asyncio.Event()
    task = asyncio.create_task(automatic_maintenance(stop))
    yield
    stop.set()
    await task


app = FastAPI(title="The Isle Manager", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins or ["http://localhost", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/about")
def about() -> dict[str, Any]:
    return {"name": "MK Panel", "version": app.version, "steam_app_id": settings().steam_app_id, "games": ["theisle", "minecraft"]}


@app.get("/api/setup/status")
def setup_status(db: Annotated[Session, Depends(get_db)]) -> dict[str, bool]:
    return {"initialized": db.query(func.count(User.id)).scalar() > 0}


@app.post("/api/setup/initialize", response_model=TokenOut)
def initialize(payload: SetupRequest, db: Annotated[Session, Depends(get_db)]):
    if db.query(func.count(User.id)).scalar() > 0:
        api_error(409, "The panel has already been initialized")
    user = User(email=payload.email.lower(), password_hash=pwd_context.hash(payload.password), role="owner")
    db.add(user)
    db.commit()
    db.refresh(user)
    audit(db, user, "setup.initialize", "panel")
    return {"access_token": make_token(user), "user": user}


@app.post("/api/auth/login", response_model=TokenOut)
def login(payload: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not user.enabled or not pwd_context.verify(payload.password, user.password_hash):
        api_error(401, "Incorrect email or password")
    audit(db, user, "auth.login", user.email)
    return {"access_token": make_token(user), "user": user}


@app.get("/api/auth/me", response_model=UserOut)
def me(user: Annotated[User, Depends(current_user)]):
    return user


@app.get("/api/users", response_model=list[UserOut])
def list_users(db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(require("users:manage"))]):
    return db.query(User).order_by(User.created_at.asc()).all()


@app.post("/api/users", response_model=UserOut)
def create_user(payload: UserCreate, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("users:manage"))]):
    if payload.role not in ROLE_PERMISSIONS:
        api_error(422, "Invalid role")
    user = User(email=payload.email.lower(), password_hash=pwd_context.hash(payload.password), role=payload.role)
    try:
        db.add(user)
        db.commit()
    except IntegrityError:
        db.rollback()
        api_error(409, "A user with that email already exists")
    db.refresh(user)
    audit(db, actor, "users.create", user.email, payload.role)
    return user


@app.patch("/api/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: dict[str, Any], db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("users:manage"))]):
    user = db.get(User, user_id)
    if not user:
        api_error(404, "User not found")
    if "role" in payload:
        if payload["role"] not in ROLE_PERMISSIONS:
            api_error(422, "Invalid role")
        user.role = payload["role"]
    if "enabled" in payload:
        user.enabled = bool(payload["enabled"])
    db.commit()
    db.refresh(user)
    audit(db, actor, "users.update", user.email)
    return user


@app.get("/api/servers")
def list_servers(db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
    manager = get_manager()
    return [server_payload(server, manager) for server in db.query(GameServer).order_by(GameServer.name.asc()).all()]


@app.post("/api/servers")
def create_server(payload: ServerCreate, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("servers:manage"))]):
    if db.query(GameServer).filter((GameServer.game_port == payload.game_port) | (GameServer.query_port == payload.game_port) | (GameServer.game_port == payload.query_port) | (GameServer.query_port == payload.query_port)).first():
        api_error(409, "One or both ports are already assigned to a managed server")
    raw = payload.model_dump(exclude={"map_name", "password"})
    server = GameServer(**raw, password=encrypt(payload.password), steam_app_id=settings().steam_app_id, config=default_config(payload))
    try:
        db.add(server)
        db.commit()
        db.refresh(server)
        manager = get_manager()
        manager.prepare_server(server)
    except IntegrityError:
        db.rollback()
        api_error(409, "A server with that name already exists")
    except ManagerError as exc:
        db.delete(server)
        db.commit()
        api_error(503, str(exc))
    manager.notify_discord(server, "server_created", "server created and ready to install.")
    audit(db, actor, "servers.create", server.id, server.name)
    return server_payload(server, manager)


@app.get("/api/servers/{server_id}")
def read_server(server_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
    return server_payload(get_server(db, server_id), get_manager())


@app.patch("/api/servers/{server_id}")
def patch_server(server_id: str, payload: ServerPatch, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("servers:manage"))]):
    server = get_server(db, server_id)
    changes = payload.model_dump(exclude_unset=True)
    manager = get_manager()
    was_running = manager.status(server)["status"] == "running"
    recreate_container = any(key in {"ram_limit_mb", "cpu_limit"} for key in changes)
    for key, value in changes.items():
        if key == "password":
            value = encrypt(value)
        setattr(server, key, value)
    db.commit()
    db.refresh(server)
    manager.write_config(server)
    if recreate_container:
        # Docker only applies memory/CPU limits at container creation. Recreate the
        # container while keeping the bind-mounted server files intact.
        manager.delete(server, delete_files=False)
        if was_running:
            manager.start(server)
    audit(db, actor, "servers.update", server.id)
    return server_payload(server, manager)


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str, delete_data: bool = Query(False), db: Annotated[Session, Depends(get_db)] = None, actor: Annotated[User, Depends(require("servers:manage"))] = None):
    server = get_server(db, server_id)
    manager = get_manager()
    try:
        manager.delete(server, delete_data)
    except ManagerError as exc:
        api_error(503, str(exc))
    db.delete(server)
    db.commit()
    audit(db, actor, "servers.delete", server_id, f"delete_data={delete_data}")
    return {"deleted": True, "data_deleted": delete_data}


@app.post("/api/servers/{server_id}/actions/{action}")
def server_action(server_id: str, action: str, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("servers:manage"))]):
    server = get_server(db, server_id)
    manager = get_manager()
    update_backup: Backup | None = None
    was_running = False
    try:
        if action == "start":
            manager.start(server)
            output = "Server start requested"
        elif action == "stop":
            manager.stop(server)
            output = "Server stopped"
        elif action == "restart":
            manager.restart(server)
            output = "Server restart requested"
        elif action == "install" and server.game_type == "theisle":
            # A first install can take several minutes. Starting the game container
            # returns immediately while SteamCMD runs in the container, so CasaOS
            # never times out the HTTP request and its output remains in Console.
            manager.start(server)
            output = "Installation started in the background. Open Console to follow progress; the server starts automatically when installation completes."
        elif action in {"install", "update", "verify"}:
            if server.game_type == "minecraft":
                if action == "verify":
                    api_error(422, "Minecraft files are managed by the selected server image; use Restart to validate them.")
                was_running = manager.status(server)["status"] == "running"
                manager.delete(server, delete_files=False)
                manager.start(server)
                output = "Minecraft server image will download or update when it starts"
                if not was_running:
                    manager.stop(server)
                return {"ok": True, "action": action, "output": output, "metrics": manager.status(server)}
            was_running = manager.status(server)["status"] == "running"
            if action == "update":
                update_backup = manager.create_backup(db, server, None, "pre-update")
            manager.stop(server)
            output = manager.run_steam_action(server, "validate" if action == "verify" else action)
            if was_running:
                manager.start(server)
        else:
            api_error(404, "Unknown action")
    except ManagerError as exc:
        if action == "update" and update_backup:
            try:
                manager.restore_backup(server, update_backup)
                if was_running:
                    manager.start(server)
                api_error(503, f"Update failed and the pre-update backup was restored: {exc}")
            except ManagerError:
                pass
        api_error(503, str(exc))
    event = {
        "start": "server_started", "stop": "server_stopped", "restart": "server_restarted",
        "install": "server_installed", "update": "server_updated",
    }.get(action)
    if event:
        manager.notify_discord(server, event, f"{action} completed.")
    audit(db, actor, f"servers.{action}", server.id)
    return {"ok": True, "action": action, "output": output[-8000:], "metrics": manager.status(server)}


@app.get("/api/servers/{server_id}/logs", response_class=PlainTextResponse)
def server_logs(server_id: str, tail: int = Query(500, ge=1, le=5000), q: str = "", db: Annotated[Session, Depends(get_db)] = None, _: Annotated[User, Depends(current_user)] = None):
    text = get_manager().logs(get_server(db, server_id), tail)
    if q:
        query = q.lower()
        text = "\n".join(line for line in text.splitlines() if query in line.lower())
    return text


@app.get("/api/servers/{server_id}/logs/download")
def download_logs(server_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
    server = get_server(db, server_id)
    logs = get_manager().logs(server, 5000)
    path = settings().data_dir / "logs" / f"{server.id}-latest.log"
    path.write_text(logs, encoding="utf-8")
    return FileResponse(path, media_type="text/plain", filename=f"{server.name}-latest.log")


@app.post("/api/servers/{server_id}/console")
def console(server_id: str, payload: ConsoleCommand, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("console:access"))]):
    server = get_server(db, server_id)
    command = payload.command.strip()
    base = command.split(maxsplit=1)[0].lower()
    if base not in {"restart", "stop", "save", "broadcast", "kick", "ban"}:
        api_error(422, "Only restart, stop, save, broadcast, kick, and ban are accepted")
    manager = get_manager()
    try:
        if base == "restart":
            manager.restart(server)
            output = "Restart requested"
        elif base == "stop":
            manager.stop(server)
            output = "Stop requested"
        else:
            networking = (server.config or {}).get("networking", {})
            port = networking.get("rcon_port")
            password = decrypt(networking.get("rcon_password_encrypted", networking.get("rcon_password", "")))
            if not port or not password:
                api_error(409, "Configure an RCON port and password in Networking before sending in-game commands")
            host = networking.get("rcon_host") or manager._container_name(server)
            output = source_rcon_command(str(host), int(port), password, command) if server.game_type == "minecraft" else isle_rcon_command(str(host), int(port), password, command)
    except (ManagerError, OSError, ValueError) as exc:
        api_error(502, f"Console command failed: {exc}")
    audit(db, actor, "console.command", server.id, command)
    return {"ok": True, "output": output}


@app.get("/api/servers/{server_id}/config")
def read_config(server_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(require("config:edit"))]):
    server = get_server(db, server_id)
    config = json.loads(json.dumps(server.config or {}))
    config.setdefault("general", {})["server_password"] = decrypt(server.password)
    networking = config.setdefault("networking", {})
    networking["rcon_password"] = decrypt(networking.pop("rcon_password_encrypted", ""))
    discord = config.setdefault("discord", {})
    discord["webhook_url"] = decrypt(discord.pop("webhook_url_encrypted", ""))
    return {"config": config}


@app.put("/api/servers/{server_id}/config")
def update_config(server_id: str, payload: ConfigPatch, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("config:edit"))]):
    server = get_server(db, server_id)
    allowed = {"general", "gameplay", "admins", "world", "networking", "backups", "automation", "discord"}
    if set(payload.config) - allowed:
        api_error(422, "Configuration contains an unsupported section")
    config = json.loads(json.dumps(payload.config))
    for section in allowed:
        if section in config and not isinstance(config[section], dict):
            api_error(422, f"Configuration section '{section}' must be an object")
    previous_ports = (server.game_port, server.query_port)
    game_password = config.get("general", {}).pop("server_password", None)
    if game_password is not None:
        server.password = encrypt(str(game_password))
    networking = config.get("networking", {})
    rcon_password = networking.pop("rcon_password", "")
    if rcon_password:
        networking["rcon_password_encrypted"] = encrypt(str(rcon_password))
    elif "rcon_password_encrypted" not in networking:
        previous_secret = (server.config or {}).get("networking", {}).get("rcon_password_encrypted")
        if previous_secret:
            networking["rcon_password_encrypted"] = previous_secret

    automation = config.setdefault("automation", {})
    if automation.get("update_schedule", "off") not in {"off", "daily", "weekly"}:
        api_error(422, "Automatic update schedule must be off, daily, or weekly")

    discord = config.setdefault("discord", {})
    webhook_url = discord.pop("webhook_url", None)
    if webhook_url is not None:
        if not isinstance(webhook_url, str):
            api_error(422, "Discord webhook URL must be a string")
        if webhook_url.strip():
            discord["webhook_url_encrypted"] = encrypt(validate_discord_webhook(webhook_url))
        else:
            discord.pop("webhook_url_encrypted", None)
    else:
        previous_webhook = (server.config or {}).get("discord", {}).get("webhook_url_encrypted")
        if previous_webhook:
            discord["webhook_url_encrypted"] = previous_webhook
    discord["enabled"] = bool(discord.get("enabled", False))
    events = discord.setdefault("events", {})
    if not isinstance(events, dict) or set(events) - DISCORD_EVENTS or any(not isinstance(value, bool) for value in events.values()):
        api_error(422, "Discord notification events are invalid")
    for event in DISCORD_EVENTS:
        events.setdefault(event, True)

    server.config = config
    general = config.get("general", {})
    if isinstance(general.get("max_players"), int):
        server.max_players = max(1, min(general["max_players"], 300))
    for field, attr in (("port", "game_port"), ("query_port", "query_port")):
        value = networking.get(field)
        if isinstance(value, int) and 1024 <= value <= 65535:
            setattr(server, attr, value)
    if server.game_port == server.query_port:
        api_error(422, "Game and query ports must differ")
    conflict = db.query(GameServer).filter(GameServer.id != server.id).filter(
        (GameServer.game_port.in_([server.game_port, server.query_port])) | (GameServer.query_port.in_([server.game_port, server.query_port]))
    ).first()
    if conflict:
        api_error(409, "One or both ports are already assigned to a managed server")
    db.commit()
    db.refresh(server)
    manager = get_manager()
    manager.write_config(server)
    ports_changed = previous_ports != (server.game_port, server.query_port)
    if ports_changed:
        was_running = manager.status(server)["status"] == "running"
        manager.delete(server, delete_files=False)
        if was_running:
            manager.start(server)
    audit(db, actor, "config.update", server.id)
    public_config = json.loads(json.dumps(server.config))
    public_config.get("general", {}).pop("server_password", None)
    public_config.get("networking", {}).pop("rcon_password_encrypted", None)
    public_config.get("discord", {}).pop("webhook_url_encrypted", None)
    return {"ok": True, "restart_required": manager.status(server)["status"] == "running" and not ports_changed, "config": public_config}


@app.get("/api/servers/{server_id}/backups")
def list_backups(server_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
    get_server(db, server_id)
    backups = db.query(Backup).filter(Backup.server_id == server_id).order_by(Backup.created_at.desc()).all()
    return [{"id": item.id, "name": item.name, "size_bytes": item.size_bytes, "kind": item.kind, "created_at": item.created_at} for item in backups]


@app.post("/api/servers/{server_id}/backups")
def create_backup(server_id: str, payload: BackupCreate, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("backups:manage"))]):
    server = get_server(db, server_id)
    try:
        backup = get_manager().create_backup(db, server, payload.name)
    except ManagerError as exc:
        api_error(503, str(exc))
    get_manager().notify_discord(server, "backup_created", f"backup '{backup.name}' created.")
    audit(db, actor, "backups.create", backup.id, server.id)
    return {"id": backup.id, "name": backup.name, "size_bytes": backup.size_bytes, "created_at": backup.created_at}


@app.get("/api/servers/{server_id}/backups/{backup_id}/download")
def download_backup(server_id: str, backup_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
    backup = db.get(Backup, backup_id)
    if not backup or backup.server_id != server_id:
        api_error(404, "Backup not found")
    path = Path(backup.path)
    if not path.is_file():
        api_error(404, "Backup archive is missing")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.post("/api/servers/{server_id}/backups/{backup_id}/restore")
def restore_backup(server_id: str, backup_id: str, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("backups:manage"))]):
    server = get_server(db, server_id)
    backup = db.get(Backup, backup_id)
    if not backup or backup.server_id != server_id:
        api_error(404, "Backup not found")
    try:
        get_manager().restore_backup(server, backup)
    except ManagerError as exc:
        api_error(422, str(exc))
    get_manager().notify_discord(server, "backup_restored", f"backup '{backup.name}' restored; start the server when ready.")
    audit(db, actor, "backups.restore", backup.id, server.id)
    return {"ok": True, "restart_required": True}


@app.delete("/api/servers/{server_id}/backups/{backup_id}")
def delete_backup(server_id: str, backup_id: str, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("backups:manage"))]):
    backup = db.get(Backup, backup_id)
    if not backup or backup.server_id != server_id:
        api_error(404, "Backup not found")
    try:
        Path(backup.path).unlink(missing_ok=True)
    except OSError as exc:
        api_error(500, f"Could not remove archive: {exc}")
    db.delete(backup)
    db.commit()
    audit(db, actor, "backups.delete", backup_id, server_id)
    return {"deleted": True}


def files_endpoint(category: str):
    @app.get(f"/api/servers/{{server_id}}/{category.lower()}")
    def list_category(server_id: str, db: Annotated[Session, Depends(get_db)], _: Annotated[User, Depends(current_user)]):
        return get_manager().list_files(get_server(db, server_id), category)

    @app.post(f"/api/servers/{{server_id}}/{category.lower()}/upload")
    async def upload_category(server_id: str, file: UploadFile = File(...), db: Session = Depends(get_db), actor: User = Depends(require("mods:manage"))):
        server = get_server(db, server_id)
        manager = get_manager()
        filename = file.filename or "upload.bin"
        staging = manager.create_upload_staging_path(server, category, filename)
        total = 0
        try:
            with staging.open("wb") as destination:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings().max_upload_bytes:
                        api_error(413, f"Upload exceeds the {settings().max_upload_bytes // (1024 * 1024)} MB limit")
                    destination.write(chunk)
            if category == "Mods" and server.game_type == "minecraft" and filename.lower().endswith(".zip"):
                imported = manager.import_minecraft_modpack(server, staging)
                saved_name = f"{filename} ({imported} files imported)"
                saved_size = total
            else:
                saved = manager.commit_staged_upload(server, category, filename, staging)
                saved_name = saved.name
                saved_size = saved.stat().st_size
        except ManagerError as exc:
            api_error(422, str(exc))
        except OSError as exc:
            api_error(500, f"Could not save uploaded file: {exc}")
        finally:
            await file.close()
            staging.unlink(missing_ok=True)
        audit(db, actor, f"{category.lower()}.upload", server.id, saved_name)
        return {"name": saved_name, "size_bytes": saved_size}

    @app.post(f"/api/servers/{{server_id}}/{category.lower()}/{{name}}/toggle")
    def toggle_category(server_id: str, name: str, enabled: bool, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("mods:manage"))]):
        try:
            get_manager().toggle_file(get_server(db, server_id), category, name, enabled)
        except (ManagerError, OSError) as exc:
            api_error(422, str(exc))
        audit(db, actor, f"{category.lower()}.toggle", server_id, f"{name}={enabled}")
        return {"ok": True}

    @app.delete(f"/api/servers/{{server_id}}/{category.lower()}/{{name}}")
    def delete_category(server_id: str, name: str, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("mods:manage"))]):
        try:
            get_manager().remove_file(get_server(db, server_id), category, name)
        except ManagerError as exc:
            api_error(404, str(exc))
        audit(db, actor, f"{category.lower()}.delete", server_id, name)
        return {"deleted": True}


files_endpoint("Mods")
files_endpoint("Plugins")


@app.post("/api/servers/{server_id}/mods/workshop")
def workshop_mod(server_id: str, payload: ModInstall, db: Annotated[Session, Depends(get_db)], actor: Annotated[User, Depends(require("mods:manage"))]):
    server = get_server(db, server_id)
    try:
        output = get_manager().run_steam_action(server, "workshop", payload.workshop_id)
    except ManagerError as exc:
        api_error(502, str(exc))
    get_manager().notify_discord(server, "mod_installed", f"Workshop mod {payload.workshop_id} installed.")
    audit(db, actor, "mods.workshop", server.id, payload.workshop_id)
    return {"ok": True, "output": output[-8000:]}


@app.websocket("/ws/servers/{server_id}/console")
async def console_socket(websocket: WebSocket, server_id: str, token: str = Query("")):
    try:
        claims = jwt.decode(token, settings().jwt_secret, algorithms=["HS256"])
        if not claims.get("sub"):
            raise jwt.PyJWTError
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    db = next(get_db())
    try:
        user = db.get(User, claims["sub"])
        server = db.get(GameServer, server_id)
        if not user or not user.enabled or not server:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        manager = get_manager()
        previous = ""
        tick = 0
        while True:
            current = manager.logs(server, 1000)
            if current != previous:
                # A rotated log or a very large change is sent as a fresh tail.
                lines = current.splitlines()
                if previous and current.startswith(previous):
                    lines = current[len(previous):].splitlines()
                await websocket.send_json({"type": "logs", "lines": lines[-500:]})
                previous = current
            if tick % 3 == 0:
                await websocket.send_json({"type": "metrics", "metrics": manager.status(server)})
            tick += 1
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
    finally:
        db.close()

