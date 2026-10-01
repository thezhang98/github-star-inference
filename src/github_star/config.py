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


# --- M2: LLM tagging config -------------------------------------------------

def llm_base_url() -> str:
    """OpenAI-compatible base URL; defaults to DeepSeek."""
    return os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")


def llm_api_key() -> str:
    """LLM API key, or raise before any request is made."""
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise ConfigError("LLM_API_KEY not set")
    return key


def llm_model() -> str:
    """Chat model id; defaults to DeepSeek's deepseek-chat."""
    return os.environ.get("LLM_MODEL", "deepseek-chat")


def tag_ttl_days() -> int:
    """Days before a tagged repo is considered stale (default 90)."""
    return int(os.environ.get("TAG_TTL_DAYS", "90"))


def readme_max_chars() -> int:
    """README truncation limit before storing/sending to the LLM (default 4000)."""
    return int(os.environ.get("README_MAX_CHARS", "4000"))


def user_focus_areas() -> str:
    """Optional user focus-area hint fed into clustering (default empty)."""
    return os.environ.get("USER_FOCUS_AREAS", "").strip()
