from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable via environment variables prefixed with GEO_.

    Example: GEO_DATABASE_URL=postgresql+psycopg://user:pass@localhost/geo
    """

    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    app_name: str = "Geospatial File Measurement API"
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: Path = Path("./data/uploads")
    max_upload_bytes: int = 50 * 1024 * 1024  # 50 MB
    # Guards against zip bombs: limits on what an uploaded archive may expand to.
    max_extracted_bytes: int = 500 * 1024 * 1024  # 500 MB
    max_archive_members: int = 1000
