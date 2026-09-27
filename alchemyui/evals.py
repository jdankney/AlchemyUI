"""`alchemyui router-eval`: the router judged on real models (spec 7.5).

Every case in evals/router/cases.json goes to each backend on its own --
the same prompt, schema and repair retry the app uses, but no fallback and
no default planner, so a miss is a miss. The report puts the backends side
by side: that is how the primary model is chosen, and how the gap between
primary and fallback is known rather than assumed.

A case's `expect.action` may list several acceptable actions, first the
ideal one. The finer checks (source, faces, direction, count, quality) apply
only when the plan chose that ideal action.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

from .planner.prompt import Attachment, thumbnail
from .planner.router import Router, RouteRequest
from .planner.schema import Plan, Vocabulary, face_handles

EVAL_DIR = Path(__file__).resolve().parent.parent / "evals" / "router"
FINE = ("source", "faces", "direction", "count", "quality")


def load_cases(path: Path = EVAL_DIR / "cases.json") -> list[dict]:
    return json.loads(Path(path).read_text())


def score(case: dict, plan: Plan | None) -> tuple[bool, str]:
    if plan is None:
        return False, "no valid plan"
    allowed = case["expect"]["action"]
    allowed = [allowed] if isinstance(allowed, str) else list(allowed)
    if plan.action not in allowed:
        return False, f"{plan.action}, wanted {' or '.join(allowed)}"
    if plan.action == allowed[0]:
        for key in FINE:
            want = case["expect"].get(key)
            if want is not None and getattr(plan, key) != want:
                return False, f"{plan.action} with {key}={getattr(plan, key)!r}, wanted {want!r}"
    return True, ""


def _request(case: dict, images: Path, edge: int) -> RouteRequest:
    atts = [Attachment(f"a{i + 1}", thumbnail((images / name).read_bytes(), edge))
            for i, name in enumerate(case["attachments"])]
    # faces are derived exactly as the engine derives them, never hand-listed
    return RouteRequest(case["text"], atts, case["context"], case["has_likeness"], [],
                        case["sources"],
                        face_handles(case["has_likeness"], [a.handle for a in atts]))


async def run_eval(cases: list[dict], clients: dict, vocab: Vocabulary,
                   images: Path = EVAL_DIR / "images", edge: int = 768,
                   on_row=None) -> dict:
    """clients: {"primary": LLMClient, ...}. Sequential, so latencies are
    honest rather than a measure of contention."""
    results: dict = {"rows": {}, "summary": {}}
    for name, client in clients.items():
        router = Router(vocab, client, None)
        rows = []
        for case in cases:
            t0 = time.monotonic()
            r = await router.route(_request(case, images, edge))
            # Each backend runs alone, in the router's first slot; anything
            # but the no-LLM default is this backend's own answer.
            plan = r.plan if r.source != "default" else None
            ok, why = score(case, plan)
            row = {"id": case["id"], "ok": ok, "why": why,
                   "plan": plan.as_dict() if plan else None,
                   "ms": round((time.monotonic() - t0) * 1000),
                   "repaired": any("invalid plan" in e for e in r.errors),
                   "errors": r.errors}
            rows.append(row)
            if on_row:
                on_row(name, case, row)
        results["rows"][name] = rows
        ms = [r["ms"] for r in rows]
        results["summary"][name] = {
            "model": getattr(client, "model", ""), "n": len(rows),
            "ok": sum(r["ok"] for r in rows),
            "accuracy": sum(r["ok"] for r in rows) / len(rows) if rows else 0.0,
            "median_ms": round(statistics.median(ms)) if ms else 0,
            "max_ms": max(ms) if ms else 0,
            "repaired": sum(r["repaired"] for r in rows),
        }
    return results


def render_report(results: dict, cases: list[dict]) -> str:
    names = list(results["rows"])
    out = ["# Router eval", "", f"{len(cases)} cases, {time.strftime('%Y-%m-%d %H:%M')}", ""]
    out.append("| Backend | Model | Correct | Accuracy | Median | Slowest | Repaired |")
    out.append("|---|---|---|---|---|---|---|")
    for n in names:
        s = results["summary"][n]
        out.append(f"| {n} | {s['model']} | {s['ok']}/{s['n']} | {s['accuracy']:.0%} | "
                   f"{s['median_ms'] / 1000:.1f}s | {s['max_ms'] / 1000:.1f}s | {s['repaired']} |")
    out += ["", "| Case | Message | Expected | " + " | ".join(names) + " |",
            "|---|---|---|" + "---|" * len(names)]
    by_id = {n: {r["id"]: r for r in results["rows"][n]} for n in names}
    for case in cases:
        want = case["expect"]["action"]
        want = want if isinstance(want, str) else " / ".join(want)
        text = (case["text"] or "(no words)").replace("|", "/")[:60]
        if case["attachments"]:
            text += f" [{len(case['attachments'])} image{'s' if len(case['attachments']) > 1 else ''}]"
        cells = []
        for n in names:
            r = by_id[n][case["id"]]
            got = r["plan"]["action"] if r["plan"] else "none"
            cells.append(("ok " if r["ok"] else "**MISS** ") + got
                         + ("" if r["ok"] else f": {r['why']}") + f" ({r['ms'] / 1000:.1f}s)")
        out.append(f"| {case['id']} | {text} | {want} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def run_cli(backend: str, only: str | None, out_dir: str | None) -> int:
    from .config import REPO_ROOT, get_settings
    from .registry import load_registry
    from .threads import build_router

    settings = get_settings()
    vocab = Vocabulary.from_registry(load_registry(settings.workflows_dir), settings.max_batch)
    built = build_router(settings, vocab)
    clients = {n: getattr(built, n) for n in ("primary", "fallback")
               if backend in (n, "both") and getattr(built, n) is not None}
    if not clients:
        print(f"no {backend} backend is configured (ROUTER_PRIMARY_URL/MODEL, ROUTER_FALLBACK_URL)")
        return 1
    cases = [c for c in load_cases() if not only or c["id"] == only]

    def progress(name, case, row):
        mark = "ok  " if row["ok"] else "MISS"
        print(f"  {name:<8} {mark} {case['id']:<24} {row['ms'] / 1000:5.1f}s  {row['why']}",
              flush=True)

    async def go():
        try:
            return await run_eval(cases, clients, vocab, edge=settings.router_thumb_edge,
                                  on_row=progress)
        finally:
            for c in clients.values():
                await c.aclose()

    results = asyncio.run(go())
    report = render_report(results, cases)
    out = Path(out_dir) if out_dir else REPO_ROOT / "data" / "router-eval"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"router-eval-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(report)
    (path.with_suffix(".json")).write_text(json.dumps(results, indent=1))
    print()
    for n, s in results["summary"].items():
        print(f"{n}: {s['model']}  {s['ok']}/{s['n']} ({s['accuracy']:.0%})  "
              f"median {s['median_ms'] / 1000:.1f}s")
    print(f"report: {path}")
    return 0
