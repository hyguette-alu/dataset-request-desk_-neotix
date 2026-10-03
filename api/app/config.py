from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment.

    Defaults are development-only. In production every secret below would come
    from the platform's secret store, not from a default in source.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://drd:drd@localhost:5432/drd"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 8 * 60

    # Session cookie. secure=False so it works over plain http on localhost;
    # it would be True behind TLS in production.
    cookie_name: str = "drd_session"
    cookie_secure: bool = False

    seed_users_path: str = "/seed/users.json"
    seed_episodes_path: str = "/seed/episodes.csv"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
