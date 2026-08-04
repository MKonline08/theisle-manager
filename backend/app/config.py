from functools import lru_cache
from pathlib import Path
import os


class Settings:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    database_url = os.getenv("DATABASE_URL", "sqlite:////data/theisle-manager.db")
    jwt_secret = os.getenv("JWT_SECRET", "development-secret-change-me")
    jwt_ttl_minutes = int(os.getenv("JWT_TTL_MINUTES", "720"))
    steam_app_id = os.getenv("STEAM_APP_ID", "412680")
    steam_branch = os.getenv("STEAM_BRANCH", "evrima")
    server_image = os.getenv("THEISLE_SERVER_IMAGE", "theisle-manager-server:latest")
    cors_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
    log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "14"))
    max_upload_bytes = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    max_restore_bytes = int(os.getenv("MAX_RESTORE_MB", "102400")) * 1024 * 1024
    max_backup_files = int(os.getenv("MAX_BACKUP_FILES", "20000"))

    def ensure_directories(self) -> None:
        for name in ("servers", "backups", "logs", "uploads"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)


@lru_cache
def settings() -> Settings:
    return Settings()
