"""Configuration read from environment variables."""
import os


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def github_token() -> str:
    """Return the GitHub PAT, or raise before any request is made."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ConfigError("GITHUB_TOKEN not set")
    return token


def db_path() -> str:
    """SQLite file location; overridable for tests via GITHUB_STAR_DB."""
    return os.environ.get("GITHUB_STAR_DB", "github_star.db")
