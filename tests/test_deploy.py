"""The deploy artifacts, checked as text: they ship reviewed, not executed
(install.sh touches systemd on the author's machine)."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT = (REPO / "deploy/systemd/alchemyui-router.service").read_text()


def test_router_unit_is_cpu_only_twice_over():
    """-ngl 0 alone is not enough: a CUDA build still offloads large prompt
    batches and the vision projector unless it cannot see a GPU at all."""
    assert "CUDA_VISIBLE_DEVICES= " in UNIT and "-ngl 0" in UNIT
    assert "--no-mmproj-offload" in UNIT and "--mmproj" in UNIT
    assert "--host 127.0.0.1" in UNIT


def test_router_unit_takes_machine_paths_from_the_env_file():
    assert "${ROUTER_FALLBACK_GGUF}" in UNIT and "${ROUTER_FALLBACK_MMPROJ}" in UNIT
    assert "EnvironmentFile=@REPO@/.env" in UNIT
    assert "/home/" not in UNIT


def test_env_example_documents_every_router_setting():
    ex = (REPO / ".env.example").read_text()
    for k in ("ROUTER_PRIMARY_URL", "ROUTER_PRIMARY_KEY", "ROUTER_PRIMARY_MODEL",
              "ROUTER_FALLBACK_URL", "ROUTER_FALLBACK_MODEL", "ROUTER_FALLBACK_GGUF",
              "ROUTER_FALLBACK_MMPROJ", "ROUTER_FALLBACK_PORT", "ROUTER_TIMEOUT_S",
              "ROUTER_FALLBACK_TIMEOUT_S", "ROUTER_CONTEXT_TURNS", "MAX_BATCH=4"):
        assert k in ex, k
    # a key belongs in .env, never in the example
    key_line = next(line for line in ex.splitlines() if line.startswith("ROUTER_PRIMARY_KEY"))
    assert key_line.strip() == "ROUTER_PRIMARY_KEY="


def test_scripts_parse():
    for script in ("deploy/install.sh", "deploy/alchemyui-cli.sh"):
        subprocess.run(["bash", "-n", str(REPO / script)], check=True)
    assert "alchemyui-router.service" in (REPO / "deploy/install.sh").read_text()
    assert "alchemyui-router.service" in (REPO / "deploy/alchemyui-cli.sh").read_text()
