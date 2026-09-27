"""`alchemyui` -- the app's own utilities: `validate`, the standalone workflow
check, and `reassign`.

Node IDs are brittle: rebuilding a graph in the ComfyUI editor renumbers them,
and a registry entry that still points at the old numbers will submit happily
and fail deep inside a generation. This is the check that turns that into a
startup error instead, and it runs on every boot as well as from here.

    alchemyui validate          registry <-> templates, offline, no GPU
    alchemyui validate --live   also ask a running ComfyUI whether the node
                                classes are installed and the checkpoints exist
    alchemyui reassign FROM TO  move every image, job, upload, reference and
                                preset from one account (by email) to another
    alchemyui router-eval       score the router's backends on evals/router
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings
from .registry import Registry, RegistryError, load_registry

OK = "\033[1;32m✓\033[0m"
BAD = "\033[1;31m✗\033[0m"
WARN = "\033[1;33m!\033[0m"


def _validate_offline() -> Registry | None:
    settings = get_settings()
    print(f"registry: {settings.workflows_dir}/registry.json")
    try:
        reg = load_registry(settings.workflows_dir)
    except RegistryError as e:
        print(f"  {BAD} {e}")
        return None

    for w in reg.all():
        print(f"  {OK} {w.key:<16} {w.template_name}")
        for name, b in sorted(w.params.items()):
            node = w.template[b.node]
            print(f"      {name:<12} -> node {b.node} ({node['class_type']}).{b.field}")
        print(f"      {'output':<12} -> node {w.output_node} "
              f"({w.template[w.output_node]['class_type']})")
    return reg


async def _validate_live(reg: Registry) -> bool:
    from .comfy import ComfyClient

    settings = get_settings()
    comfy = ComfyClient(settings.comfyui_url)
    print(f"\nlive check against {comfy.base_url}")
    ok = True
    try:
        if not await comfy.health():
            print(f"  {BAD} ComfyUI is not reachable")
            return False
        print(f"  {OK} reachable")

        info = await comfy.object_info()
        installed = set(info)
        for w in reg.all():
            missing = {
                n["class_type"] for n in w.template.values()
                if n["class_type"] not in installed
            }
            if missing:
                ok = False
                print(f"  {BAD} {w.key}: node classes not installed: {', '.join(sorted(missing))}")
            else:
                print(f"  {OK} {w.key}: all {len(w.template)} node classes installed")

        checkpoints = set(await comfy.checkpoints())
        for name in reg.checkpoints():
            if name in checkpoints:
                print(f"  {OK} checkpoint present: {name}")
            else:
                ok = False
                print(f"  {BAD} checkpoint MISSING: {name}")
                print(f"      ComfyUI offers: {', '.join(sorted(checkpoints)) or '(none)'}")

        # Sampler/scheduler names are enumerated by the KSampler node itself,
        # so a curated list in the registry can drift from what's accepted.
        ks = info.get("KSampler", {}).get("input", {}).get("required", {})
        for field_name, key in (("sampler_name", "sampler"), ("scheduler", "scheduler")):
            allowed = set((ks.get(field_name) or [[]])[0])
            if not allowed:
                continue
            for w in reg.all():
                bad = [c for c in w.choices.get(key, ()) if c not in allowed]
                if bad:
                    ok = False
                    print(f"  {BAD} {w.key}: {key} choices ComfyUI rejects: {', '.join(bad)}")
    finally:
        await comfy.aclose()
    return ok


def cmd_validate(args: argparse.Namespace) -> int:
    reg = _validate_offline()
    if reg is None:
        return 1
    if args.live and not asyncio.run(_validate_live(reg)):
        return 1
    print(f"\n{OK} registry valid")
    return 0


def cmd_reassign(args: argparse.Namespace) -> int:
    """Presets are unique per user by name, so one the target already has by
    that name is left with its original owner rather than failing the move."""
    import sqlite3

    from .db import Database

    db = Database(get_settings().database_path)
    try:
        c = db._conn

        def uid(email):
            r = c.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
            return r[0] if r else None

        src, dst = uid(args.src), uid(args.dst)
        if src is None or dst is None:
            print(f"  {BAD} both accounts must have signed in at least once "
                  f"({args.src}: {'ok' if src else 'unknown'}, {args.dst}: {'ok' if dst else 'unknown'})")
            return 1
        c.execute("BEGIN")
        try:
            for t in ("jobs", "images", "uploads", "refs"):
                n = c.execute(f"UPDATE {t} SET user_id = ? WHERE user_id = ?", (dst, src)).rowcount
                print(f"  {OK} {t:<8} {n} row(s)")
            n = c.execute("UPDATE OR IGNORE presets SET user_id = ? WHERE user_id = ?",
                          (dst, src)).rowcount
            print(f"  {OK} presets  {n} row(s)")
            c.execute("COMMIT")
        except sqlite3.Error:
            c.execute("ROLLBACK")
            raise
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alchemyui", description="AlchemyUI utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="check the workflow registry against its templates")
    v.add_argument("--live", action="store_true",
                   help="also verify node classes and checkpoints against a running ComfyUI")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("reassign", help="move one account's rows to another, by email")
    r.add_argument("src", metavar="FROM")
    r.add_argument("dst", metavar="TO")
    r.set_defaults(func=cmd_reassign)

    e = sub.add_parser("router-eval", help="score the router's LLM backends on the fixture set")
    e.add_argument("--backend", choices=("primary", "fallback", "both"), default="both")
    e.add_argument("--only", metavar="IDS", help="run only these cases (comma-separated ids)")
    e.add_argument("--out", metavar="DIR", help="where to write the report (default data/router-eval)")
    e.set_defaults(func=lambda a: __import__("alchemyui.evals", fromlist=["run_cli"]).run_cli(
        a.backend, a.only, a.out))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
