"""Application configuration and settings."""
import os
import secrets
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


def _default_token_file() -> Path:
    return Path.home() / ".tartvm-manager" / "token"


class Settings(BaseSettings):
    """Application settings."""
    
    # Server settings
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DEBUG: bool = False
    
    # Security
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    TOKEN_FILE: Path = Field(default_factory=_default_token_file)
    
    # Tart settings
    TART_PATH: str = "tart"
    DEFAULT_NETWORK_INTERFACE: str = "en0"

    # Task settings
    MAX_TASK_LOGS: int = 1000

    # Tart command timeouts (in seconds)
    TIMEOUT_LIST: int = 5
    TIMEOUT_GET: int = 10
    TIMEOUT_IP: int = 4
    TIMEOUT_STOP: int = 40
    TIMEOUT_DELETE: int = 60
    TIMEOUT_PULL: int = 3600
    TIMEOUT_CLONE: int = 120

    # GitHub API settings
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_TOKEN_FILE: Path = Field(default_factory=lambda: Path.home() / ".tartvm-manager" / "github_token")
    
    class Config:
        """Pydantic config."""
        env_file = ".env"
        env_prefix = "TARTVM_"


def ensure_token_file(settings: Settings) -> None:
    """Ensure the token file exists with proper permissions."""
    settings.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings.TOKEN_FILE.parent.chmod(0o700)
    except Exception:
        pass
    if not settings.TOKEN_FILE.exists():
        settings.TOKEN_FILE.write_text(settings.SECRET_KEY)
        settings.TOKEN_FILE.chmod(0o600)


def _ensure_token_file_perms(token_file: Path) -> None:
    try:
        mode = token_file.stat().st_mode & 0o777
        if mode != 0o600:
            token_file.chmod(0o600)
    except Exception:
        pass


def _maybe_migrate_legacy_token(legacy_token_file: Path, new_token_file: Path) -> None:
    if new_token_file.exists() or not legacy_token_file.exists():
        return
    try:
        new_token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            new_token_file.parent.chmod(0o700)
        except Exception:
            pass
        new_token_file.write_text(legacy_token_file.read_text().strip())
        new_token_file.chmod(0o600)
    except Exception:
        pass


# Initialize settings
settings = Settings()

# Ensure token file exists
legacy_token_file = Path(".token")
_maybe_migrate_legacy_token(legacy_token_file, settings.TOKEN_FILE)

if not settings.TOKEN_FILE.exists():
    ensure_token_file(settings)
else:
    _ensure_token_file_perms(settings.TOKEN_FILE)
    settings.SECRET_KEY = settings.TOKEN_FILE.read_text().strip()

# Load GitHub token if it exists
if settings.GITHUB_TOKEN_FILE.exists():
    try:
        _ensure_token_file_perms(settings.GITHUB_TOKEN_FILE)
        settings.GITHUB_TOKEN = settings.GITHUB_TOKEN_FILE.read_text().strip()
    except Exception:
        pass
