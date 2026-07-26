from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://copilot:copilot@localhost:5433/copilot"
    redis_url: str = "redis://localhost:6379/0"

    firefly_base_url: str = "http://localhost:8080"
    firefly_pat: str = ""
    firefly_webhook_secret: str = ""

    # LLM:走 Anthropic Messages API 协议;切自研网关只改 anthropic_base_url
    anthropic_api_key: str = ""
    anthropic_base_url: str | None = None
    llm_model: str = "claude-opus-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 1024
    llm_timeout: float = 30.0

    confidence_threshold: float = 0.9
    default_currency: str = "CNY"
    default_asset_account: str = "现金钱包"

    # 企业微信群机器人 webhook(告警通道)
    wecom_webhook_url: str = ""
    # Web 控制台访问令牌;本机使用可留空,公网部署必须设置
    console_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
