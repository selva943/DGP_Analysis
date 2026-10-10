"""Application settings.

Everything is overridable through environment variables (prefix ``DGP_``) or a
``.env`` file placed in the liveapp folder.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

LIVEAPP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = LIVEAPP_DIR.parent

# PostgreSQL schemes that carry an explicit "+driver" already. Left untouched.
_EXPLICIT_PG = ("postgresql+psycopg://", "postgresql+psycopg2://", "postgresql+pg8000://")


def pin_postgres_driver(url: str) -> str:
    """Give a bare ``postgresql://`` URL an explicit driver.

    SQLAlchemy 2.1.0 (released 2026-09-24) changed the default DBAPI for
    ``postgresql://`` from psycopg2 to psycopg (v3). That is a silent,
    runtime-only change: the same requirements.txt produced a working function
    on one day and

        ModuleNotFoundError: No module named 'psycopg'

    on the next, purely because the resolved version differed between the
    development machine and the deployment.

    Rewriting the scheme here means the installed package decides, not
    SQLAlchemy's version. Whatever is importable wins, so this works on any
    SQLAlchemy 2.0 or 2.1 and cannot break again on the next minor release.
    """
    if url.startswith(_EXPLICIT_PG):
        return url
    if not url.startswith(("postgresql://", "postgres://")):
        return url

    try:
        import psycopg  # noqa: F401

        scheme = "postgresql+psycopg://"
    except ImportError:
        try:
            import psycopg2  # noqa: F401

            scheme = "postgresql+psycopg2://"
        except ImportError:
            # Neither driver present. Leave the URL alone so SQLAlchemy's own
            # error surfaces, naming the driver it actually looked for.
            return url
    return scheme + url.split("://", 1)[1]


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

    # Running the ETL pipeline from inside the request process. It spawns the
    # root-level scripts via subprocess, which needs a writable checkout, a
    # persistent Python interpreter and minutes of wall clock. Vercel provides
    # none of those, so this is 0 there and 1 on the source machine.
    local_processing_enabled: bool = True

    # First-run admin, created only when the users table is completely empty.
    # This is how a fresh deployment (Vercel) gets its initial login without
    # ever committing a password to git. Once any user exists the values are
    # ignored, so rotating the password here cannot affect a live database.
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_display_name: str = ""

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
    def using_sqlite(self) -> bool:
        return not self.database_url

    @property
    def bootstrap_admin(self) -> tuple[str, str, str] | None:
        """The first-run admin triple, or None when not configured/usable."""
        username = self.bootstrap_admin_username.strip()
        password = self.bootstrap_admin_password
        if not username or not password:
            return None
        return username, password, (self.bootstrap_admin_display_name.strip() or username)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return pin_postgres_driver(self.database_url)
        # SQLite fallback for local use. Serverless platforms mount the bundle
        # read-only, so the directory cannot be created there - that is a
        # configuration error (DGP_DATABASE_URL missing), not a crash to
        # swallow. Fail with a message that says what to fix.
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                "Could not create the SQLite storage directory "
                f"({self.storage_dir}): {exc}. Serverless deployments run on a "
                "read-only filesystem and cannot use the SQLite fallback - set "
                "DGP_DATABASE_URL to your PostgreSQL/Supabase connection string."
            ) from exc
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
