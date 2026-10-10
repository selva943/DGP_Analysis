"""Application settings.

Everything is overridable through environment variables (prefix ``DGP_``) or a
``.env`` file placed in the liveapp folder.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

LIVEAPP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = LIVEAPP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DGP_",
        env_file=str(LIVEAPP_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- paths -------------------------------------------------------------
    project_root: Path = PROJECT_ROOT
    storage_dir: Path = LIVEAPP_DIR / "storage"

    # --- database ----------------------------------------------------------
    # SQLite by default (zero-config). Point DGP_DATABASE_URL at PostgreSQL /
    # SQL Server / MySQL to run against a server database.
    database_url: str = ""

    # --- auth --------------------------------------------------------------
    jwt_secret: str = "change-me-in-production-please-set-DGP_JWT_SECRET"
    jwt_algorithm: str = "HS256"
    token_ttl_minutes: int = 720  # 12 hours

    # --- http --------------------------------------------------------------
    # Comma-separated origins allowed to call the API. The frontend is served
    # by this same app, so same-origin requests need no CORS at all. Leave
    # blank to disable CORS entirely (recommended unless a separate frontend
    # host is used). Never use "*" in production.
    cors_origins: str = ""

    # Bundle import from the browser. A full import takes ~45s to write tens of
    # thousands of rows to the database, which exceeds the 60s ceiling of most
    # serverless platforms. Set to 0 where that is the case (e.g. Vercel) and
    # publish from the source machine with publish_site.py instead.
    import_enabled: bool = True

    # --- processing --------------------------------------------------------
    # Timeout (seconds) for each ETL step spawned by the admin "process data".
    etl_step_timeout: int = 3600
    # Skip the filesystem ETL scripts and only rebuild the dashboard payload
    # from files already present (useful when inputs are refreshed elsewhere).
    skip_source_scripts: bool = False

    # Visit dates / months excluded from the dashboard. Mirrors
    # build_dashboard.py so both the ETL path and the bundle import agree.
    excluded_visit_months: str = "2026-08"
    excluded_visit_dates: str = "2026-10-15"

    @property
    def excluded_months(self) -> tuple[str, ...]:
        return tuple(p for p in self.excluded_visit_months.split(",") if p.strip())

    @property
    def excluded_dates(self) -> tuple[str, ...]:
        return tuple(p for p in self.excluded_visit_dates.split(",") if p.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.storage_dir / 'dgp.db').as_posix()}"

    # Source folders inside the DGP project.
    @property
    def input_dir(self) -> Path:
        return self.project_root / "Input"

    @property
    def updating_dir(self) -> Path:
        return self.project_root / "updating_files"

    @property
    def history_dir(self) -> Path:
        return self.project_root / "history"

    @property
    def output_dir(self) -> Path:
        return self.project_root / "output"


settings = Settings()
