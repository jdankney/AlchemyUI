"""Runtime configuration. All values come from environment / .env (see
.env.example). Nothing machine-specific is committed: paths, the ComfyUI URL
and the dev identity all live in the gitignored .env the installer scaffolds.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    upload_dir: str = str(REPO_ROOT / "data" / "uploads")

    # --- uploads (Phase 2) -------------------------------------------------
    upload_max_mb: float = 24.0
    # A 6GB card will OOM long before it runs out of patience, and SDXL is
    # trained at ~1024. Anything larger is scaled down on arrival; dimensions
    # are then snapped to a multiple of 8, which the VAE requires.
    upload_max_edge: int = 1536

    # --- quality chain (Phase 3) -------------------------------------------
    # The chain upscales 4x and scales back down. On a 1024px base the spec's
    # 0.38 lands at ~1556px, which fits; on a 1344px one it would be 2043px
    # and the sampler OOMs. scale_by is reduced per job to hold this ceiling,
    # so the chain degrades to "less upscaling" instead of "failed".
    hq_max_edge: int = 1600

    # --- reference faces (Phase 4) -----------------------------------------
    # InsightFace's models, reused from ComfyUI rather than downloaded twice.
    insightface_root: str = str(Path.home() / "ComfyUI" / "models" / "insightface")
    face_det_size: int = 640
    # Every reference is normalised to this square size, which is what stops
    # ImageBatch rescaling and centre-cropping the second reference onwards.
    reference_size: int = 768
    # How much context around the face to keep: FaceID wants head and shoulders,
    # not a tight box.
    reference_margin: float = 2.2
    reference_min_face_px: int = 64
    max_references: int = 4
    # FaceID weight used when a personal LoRA is also active. The spec's
    # 0.4-0.5: both encode identity, and at full strength they fight.
    lora_face_weight: float = 0.45

    # --- identity ----------------------------------------------------------
    # Auth lives in oauth2-proxy in front (see deploy/). The app only ever
    # trusts forwarded headers -- or one fake identity in dev.
    # Comma-separated. Who may open /admin. Checked against the forwarded
    # email on every request (Llamacracy's rule); identity itself stays keyed
    # on the OIDC sub.
    admin_emails: str = ""
    dev_mode: str = ""
    dev_email: str = "dev@localhost"
    dev_sub: str = "dev-local-sub"
    dev_name: str = "Dev User"

    # --- metering (v2) -------------------------------------------------------
    # Visibility, not billing: every job's energy and its electricity cost are
    # recorded and shown per user. Nothing is ever blocked on them.
    # CPU and the rest of the laptop while it generates. Intel RAPL is
    # root-only here (the Platypus mitigation), so this is an estimate.
    non_gpu_load_watts: float = 25.0
    # Used only if nvidia-smi gave no reading for a job.
    gpu_fallback_watts: float = 60.0
    electricity_rate: float = 0.30           # $/kWh, flat
    tou_schedule: str = ""                   # JSON {"0": 0.25, ..., "23": 0.30}, local hours

    @property
    def tou_table(self) -> dict[int, float] | None:
        if not self.tou_schedule.strip():
            return None
        raw = json.loads(self.tou_schedule)
        return {int(k): float(v) for k, v in raw.items()}

    def rate_for_hour(self, hour_local: int) -> float:
        table = self.tou_table
        if table is None:
            return self.electricity_rate
        return table.get(hour_local % 24, self.electricity_rate)

    @field_validator("tou_schedule")
    @classmethod
    def _validate_tou(cls, v: str) -> str:
        if v.strip():
            json.loads(v)  # fail fast on malformed JSON
        return v

    # --- queue -------------------------------------------------------------
    # How long a submitted prompt may sit before ComfyUI starts executing it.
    # Generous: ComfyUI may be busy with work submitted from its own UI, and a
    # cold SDXL load on a 6GB card is already ~35s of that.
    job_start_timeout_s: float = 900.0
    # Weight of each new observation in the per-workflow duration EWMA.
    ewma_alpha: float = 0.3

    # Checkpoint-swap-aware ordering. A swap costs 30s+, so a job whose model
    # is already loaded may jump ahead -- but only within this many places,
    # and only until something has been passed over this many times. With a
    # single checkpoint in play neither setting does anything.
    queue_reorder_window: int = 6
    queue_max_skips: int = 3

    # Batch generation runs n separate jobs, sequentially. Also the most
    # images one chat turn may ask for ("give me 4 variations").
    max_batch: int = 4

    # --- router (v2 / Phase 7) ----------------------------------------------
    # Primary: Llamacracy's /v1 with the `alchemyui` account's key; an empty
    # URL or model disables it. Fallback: the local CPU-only llama-server
    # (the alchemyui-router unit); an empty URL disables it.
    router_primary_url: str = ""
    router_primary_key: str = ""
    router_primary_model: str = ""
    router_primary_concurrency: int = 2
    router_fallback_url: str = "http://127.0.0.1:8081/v1"
    router_fallback_model: str = "gemma4-e4b"
    router_fallback_concurrency: int = 1
    # Read by the alchemyui-router unit, not by the app: where the model is.
    router_fallback_gguf: str = ""
    router_fallback_mmproj: str = ""
    router_fallback_port: int = 8081
    # Llamacracy queues us like any user and may have to wake the model first.
    router_timeout_s: float = 45.0
    # CPU prompt processing of an image is slow; this is a fallback, not a race.
    router_fallback_timeout_s: float = 240.0
    router_context_turns: int = 6
    router_thumb_edge: int = 768

    # --- web references (Phase 9) -------------------------------------------
    # A SearXNG instance with the JSON API enabled. The app sends only the
    # search phrase; images are fetched only after the person picks one.
    search_url: str = ""
    search_timeout_s: float = 15.0
    search_results: int = 8
    search_max_mb: float = 12.0
    search_max_redirects: int = 3
    search_user_agent: str = "AlchemyUI/0.1 (+self-hosted image generator)"

    # ----------------------------------------------------------------------
    @property
    def is_dev(self) -> bool:
        return self.dev_mode.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
