"""Runtime configuration. All values come from environment / .env (see
.env.example). Nothing machine-specific is committed: paths, the ComfyUI URL
and the dev identity all live in the gitignored .env the installer scaffolds.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- ComfyUI -----------------------------------------------------------
    # The app is the only thing that talks to it. Port 8188 must never be
    # reachable from a browser: no auth, and /prompt accepts arbitrary graphs.
    comfyui_url: str = "http://127.0.0.1:8188"

    # --- binding / storage -------------------------------------------------
    app_bind_host: str = "127.0.0.1"
    port: int = 8300
    database_path: str = str(REPO_ROOT / "data" / "alchemyui.db")
    output_dir: str = str(REPO_ROOT / "data" / "outputs")
    workflows_dir: str = str(REPO_ROOT / "workflows")

    # --- identity ----------------------------------------------------------
    # Auth, if ever wanted, lives in oauth2-proxy in front (see deploy/). The
    # app only ever trusts forwarded headers -- or a fake identity in dev.
    dev_mode: str = ""
    dev_email: str = "dev@localhost"
    dev_sub: str = "dev-local-sub"
    dev_name: str = "Dev User"

    # --- queue -------------------------------------------------------------
    # How long a submitted prompt may sit before ComfyUI starts executing it.
    # Generous: ComfyUI may be busy with work submitted from its own UI, and a
    # cold SDXL load on a 6GB card is already ~35s of that.
    job_start_timeout_s: float = 900.0
    # Weight of each new observation in the per-workflow duration EWMA.
    ewma_alpha: float = 0.3

    # ----------------------------------------------------------------------
    @property
    def is_dev(self) -> bool:
        return self.dev_mode.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
