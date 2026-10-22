from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="forbid"
    )
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default='gpt-4.1-mini', min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9._:-]+$')
    openai_timeout_seconds: float = Field(default=45, ge=5, le=60)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    database_path: Path = Path("data/agentx.sqlite3")
    execution_enabled: bool = False
    python_execution_enabled: bool = False
    sandbox_supervisor_socket: Path | None = None
    sandbox_deadline_socket: Path | None = None
    sandbox_runtime_identity: str | None = None
    sandbox_runner_image: str | None = Field(default=None, pattern=r'^sha256:[a-f0-9]{64}$')
    sandbox_bootstrap_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    sandbox_seccomp_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    workspace_path: Path = BACKEND_DIR / 'workspace'
    execution_max_attempts: int = Field(default=2, ge=1, le=3)
    execution_lease_seconds: int = Field(default=120, ge=30, le=300)

    @field_validator('workspace_path')
    @classmethod
    def resolve_workspace(cls, path: Path) -> Path:
        return (path if path.is_absolute() else BACKEND_DIR / path).resolve()

    @model_validator(mode='after')
    def dedicated_workspace(self):
        workspace = self.workspace_path.resolve()
        protected = [BACKEND_DIR / '.env', self.database_path.resolve(), BACKEND_DIR / 'app', BACKEND_DIR / 'tests']
        if any(p == workspace or p.is_relative_to(workspace) for p in protected):
            raise ValueError('WORKSPACE_PATH must be a dedicated directory separate from backend configuration, source and database')
        if any(workspace.is_relative_to(p) for p in protected[2:]):
            raise ValueError('WORKSPACE_PATH cannot be inside backend source or tests')
        return self

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment or "*" in origin):
                raise ValueError("CORS_ORIGINS must contain explicit HTTP(S) origins without paths")
            _ = parsed.port  # Reject malformed ports.
        return origins

    @field_validator("database_path")
    @classmethod
    def resolve_database_path(cls, path: Path) -> Path:
        return path if path.is_absolute() else BACKEND_DIR / path
