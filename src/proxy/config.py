from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8082
    log_file: str = "proxy.log"

    # NVIDIA NIM
    nvidia_nim_api_key: str = ""
    nvidia_nim_model: str = "meta/llama-3.3-70b-instruct"
    nvidia_nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_nim_max_tokens: int = 32768
    nvidia_nim_temperature: float = 1.0
    nvidia_nim_top_p: float = 1.0

    # Request optimizations — avoid burning NIM quota on Claude Code housekeeping calls
    skip_quota_probes: bool = True
    skip_title_generation: bool = True
    skip_suggestion_mode: bool = True
    mock_prefix_detection: bool = True
    mock_filepath_extraction: bool = True

    # Telegram (optional)
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0

    # CLI sessions (used by Telegram integration)
    claude_workspace: str = "./workspace"
    max_cli_sessions: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
