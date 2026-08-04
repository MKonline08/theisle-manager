from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=72)


class SetupRequest(LoginRequest):
    pass


class UserCreate(LoginRequest):
    role: str = "viewer"


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    enabled: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class ServerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    description: str = Field(default="", max_length=500)
    version: str = Field(default="stable", max_length=64)
    game_port: int = Field(ge=1024, le=65535)
    query_port: int = Field(ge=1024, le=65535)
    max_players: int = Field(default=100, ge=1, le=300)
    ram_limit_mb: int = Field(default=4096, ge=1024, le=65536)
    cpu_limit: int = Field(default=200, ge=25, le=6400)
    disk_limit_mb: int = Field(default=20480, ge=1024, le=1048576)
    region: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=128)
    map_name: str = Field(default="Gateway", max_length=64)

    @field_validator("query_port")
    @classmethod
    def different_ports(cls, value, info):
        if info.data.get("game_port") == value:
            raise ValueError("query port must differ from game port")
        return value


class ServerPatch(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    max_players: int | None = Field(default=None, ge=1, le=300)
    ram_limit_mb: int | None = Field(default=None, ge=1024, le=65536)
    cpu_limit: int | None = Field(default=None, ge=25, le=6400)
    disk_limit_mb: int | None = Field(default=None, ge=1024, le=1048576)
    region: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=128)


class ConfigPatch(BaseModel):
    config: dict


class ConsoleCommand(BaseModel):
    command: str = Field(min_length=1, max_length=512)


class ModInstall(BaseModel):
    workshop_id: str = Field(pattern=r"^\d{1,20}$")


class BackupCreate(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
