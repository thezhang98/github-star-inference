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


# --- M4: report / idea generation config ------------------------------------

def active_recent_days() -> int:
    """Days window for the 小众宝藏 "recently active" gate (default 180).

    Tighter than status_class's 365-day 停更 line on purpose — a treasure must
    be genuinely alive, not merely "touched once in the past year" (边界一).
    """
    return int(os.environ.get("ACTIVE_RECENT_DAYS", "180"))


def max_combo_candidates() -> int:
    """Max A+B combo pairs the LLM may propose in one call (default 15, 边界三)."""
    return int(os.environ.get("MAX_COMBO_CANDIDATES", "15"))


def search_provider() -> str:
    """Demand-verification search backend: 'tavily' | 'brave' | '' (unset).

    Empty (or a missing SEARCH_API_KEY) means the client-dialogue channel: combo
    ideas carry the 未验证 note instead of server-side evidence (边界四).
    """
    return os.environ.get("SEARCH_PROVIDER", "").strip().lower()


def search_api_key() -> str:
    """API key for the search provider; empty = unconfigured (边界四)."""
    return os.environ.get("SEARCH_API_KEY", "").strip()


def search_configured() -> bool:
    """True only when BOTH provider and key are set — either missing = off."""
    return bool(search_provider() and search_api_key())
