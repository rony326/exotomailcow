from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    encryption_key: str
    admin_user: str
    admin_password: str
    database_url: str = "sqlite:///./data/exotomailcow.db"
    concurrency: int = 4
    mailcow_dav_base_url: str
    mailcow_imap_host: str
    mailcow_imap_port: int = 993
    log_level: str = "INFO"
    log_dir: str = "./data/logs"

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()
