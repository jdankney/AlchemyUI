"""The workflow registry -- the core design decision (spec section 5).

Users never see a node graph. The app keeps a library of pre-built ComfyUI
workflows exported in API format, deep-clones one per request and injects
parameters into known node IDs before POSTing to /prompt.

A capability is a registry entry, not code: build the graph, export it, add an
entry. The same declarative-inventory idea as Llamacracy's bench/inventory.json,
applied to workflows instead of models.

Node IDs are brittle -- rebuilding a graph in the editor renumbers them -- so
every binding is checked against its template at import time and a mismatch is
a loud SystemExit at boot, never a surprise at generation time.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_settings

# ComfyUI's KSampler accepts an unsigned 64-bit seed (nodes.py: max
# 0xffffffffffffffff), but SQLite's INTEGER is *signed* 64-bit, so anything
# above 2**63-1 cannot be stored on the image row. We cap at the signed max
# rather than widening the column: a seed we cannot record is a seed the
# gallery cannot reproduce, and 9.2e18 is not a meaningfully smaller space.
MAX_SEED = 0x7FFFFFFFFFFFFFFF


class RegistryError(Exception):
    """A registry entry does not match its template. Always fatal at boot."""


@dataclass(frozen=True)
class ParamBinding:
    node: str
    field: str


@dataclass(frozen=True)
class Stage:
    """One reporting pass in a graph that samples more than once.

    The quality chain runs a base sampler, then a hi-res pass, then
    FaceDetailer. ComfyUI reports progress per node, so following a single one
    would leave the bar stuck at 100% for two thirds of a six-minute job.
    `weight` is that stage's share of the wall clock -- rough is fine, it only
    has to stop the bar from lying.
    """
    node: str
    label: str
    weight: float


# How each known input is coerced. Keeping this in one table is what lets a
# new capability be a registry entry rather than a change to resolve().
_TEXT_INPUTS = frozenset({"prompt", "negative"})
_INT_INPUTS = frozenset({"steps", "pad_left", "pad_top", "pad_right", "pad_bottom",
                         "grow_mask", "feathering", "refine_steps", "face_guide"})
_FLOAT_INPUTS = frozenset({"cfg", "denoise", "scale_by", "refine", "face_denoise"})
# Filenames inside ComfyUI's input/ directory, set by the upload step rather
# than by the person -- passed through untouched.
_REF_INPUTS = frozenset({"image", "mask"})

# What kind of editor the browser should put in front of the person.
_KINDS = ("txt2img", "img2img", "inpaint", "outpaint", "enhance")


@dataclass(frozen=True)
class WorkflowInfo:
    key: str
    display: str
    template_name: str
    checkpoint: str
    blurb: str
    kind: str                            # txt2img | img2img | inpaint | outpaint
    requires: tuple[str, ...]            # "image", "mask" -- what the UI must collect first
    inputs: tuple[str, ...]
    params: dict[str, ParamBinding]
    defaults: dict[str, Any]
    limits: dict[str, tuple[float, float]]
    choices: dict[str, tuple[str, ...]]
    aspects: dict[str, tuple[int, int]]
    progress_node: str
    output_node: str
    stages: tuple[Stage, ...]
    hq_variant: str | None
    est_seconds_per_step: float
    est_load_s: float
    template: dict = field(repr=False, default_factory=dict)

    # -- parameter resolution ------------------------------------------------
    def resolve(self, raw: dict) -> dict:
        """Turn a request body into the full, clamped parameter set that will
        be both injected into the graph and stored on the job and image rows.

        What the browser sends is a suggestion. Everything is defaulted from
        the registry, clamped to `limits` and checked against `choices` here,
        so a stored params_json is always complete enough to reproduce the run
        and the graph can never be handed a value ComfyUI would refuse.
        """
        p: dict[str, Any] = {}

        p["prompt"] = (raw.get("prompt") or "").strip()
        negative = raw.get("negative")
        if negative is None:
            negative = self.defaults.get("negative", "")
        p["negative"] = str(negative).strip()

        # Only workflows that size their own canvas take an aspect; img2img and
        # inpaint inherit the source image's dimensions instead.
        if self.aspects and "width" in self.params:
            aspect = raw.get("aspect") or self.defaults.get("aspect")
            if aspect not in self.aspects:
                aspect = next(iter(self.aspects))
            p["aspect"] = aspect
            p["width"], p["height"] = self.aspects[aspect]

        seed = raw.get("seed")
        # A seed of 0 is a legitimate choice, so only None/"" means "surprise me".
        if seed is None or seed == "":
            seed = random.randint(0, MAX_SEED)
        p["seed"] = max(0, min(MAX_SEED, int(seed)))
        # A multi-stage graph has more than one sampler. They all take the
        # job's seed, so the whole run reproduces from one number rather than
        # three -- which is what the gallery's "reuse settings" promises.
        for extra in ("hq_seed", "face_seed"):
            if extra in self.params:
                p[extra] = p["seed"]

        for name in self.inputs:
            if name in ("prompt", "negative", "aspect", "seed"):
                continue
            value = raw.get(name)
            if value is None or value == "":
                value = self.defaults.get(name)
            if value is None:
                continue
            if name in _REF_INPUTS:
                p[name] = str(value)
            elif name in _INT_INPUTS:
                p[name] = int(self._clamp(name, value))
            elif name in _FLOAT_INPUTS:
                p[name] = round(float(self._clamp(name, value)), 4)
            elif name in self.choices:
                allowed = self.choices[name]
                p[name] = value if value in allowed else self.defaults.get(name, allowed[0])
            elif name in _TEXT_INPUTS:
                p[name] = str(value).strip()
            else:
                p[name] = value

        p["checkpoint"] = self.checkpoint
        p["workflow_id"] = self.key
        return p

    def _clamp(self, name: str, value: Any) -> float:
        lo, hi = self.limits.get(name, (float("-inf"), float("inf")))
        return max(lo, min(hi, float(value)))

    # -- graph construction --------------------------------------------------
    def build_graph(self, params: dict) -> dict:
        """Deep-clone the template and inject the resolved parameters.

        Only bindings the caller actually supplied a value for are written, so
        a template default stays in place for anything the registry does not
        bind (e.g. denoise, batch_size).
        """
        graph = copy.deepcopy(self.template)
        for name, binding in self.params.items():
            if name not in params:
                continue
            graph[binding.node]["inputs"][binding.field] = params[name]
        return graph

    def public(self) -> dict:
        """What the browser is told about this workflow. Node IDs and template
        internals deliberately never cross this line."""
        return {
            "id": self.key,
            "display": self.display,
            "blurb": self.blurb,
            "checkpoint": self.checkpoint,
            "kind": self.kind,
            "requires": list(self.requires),
            "hq_variant": self.hq_variant,
            "stages": [s.label for s in self.stages] if len(self.stages) > 1 else [],
            "inputs": list(self.inputs),
            "defaults": dict(self.defaults),
            "limits": {k: list(v) for k, v in self.limits.items()},
            "choices": {k: list(v) for k, v in self.choices.items()},
            "aspects": {k: list(v) for k, v in self.aspects.items()},
        }


class Registry:
    def __init__(self, workflows: dict[str, WorkflowInfo]):
        self._workflows = workflows

    def get(self, key: str) -> WorkflowInfo | None:
        return self._workflows.get(key)

    def require(self, key: str) -> WorkflowInfo:
        w = self._workflows.get(key)
        if w is None:
            raise KeyError(f"unknown workflow {key!r}")
        return w

    def all(self) -> list[WorkflowInfo]:
        return list(self._workflows.values())

    def of_kind(self, kind: str) -> list[WorkflowInfo]:
        return [w for w in self._workflows.values() if w.kind == kind]

    def checkpoints(self) -> list[str]:
        """Every checkpoint any entry needs -- what `validate --live` checks
        against ComfyUI's actual model list."""
        return sorted({w.checkpoint for w in self._workflows.values() if w.checkpoint})


# --------------------------------------------------------------------------- #
# validation -- pure JSON, so the whole suite runs with no GPU and no server
# --------------------------------------------------------------------------- #
def _load_template(path: Path, entry_key: str) -> dict:
    if not path.exists():
        raise RegistryError(f"{entry_key}: template not found at {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RegistryError(f"{entry_key}: {path.name} is not valid JSON ({e})")

    if not isinstance(data, dict):
        raise RegistryError(f"{entry_key}: {path.name} is not a JSON object")

    # The regular "Save" in ComfyUI writes a UI graph -- node positions, link
    # arrays, widgets_values -- which /prompt rejects at validation. Name the
    # mistake explicitly; it is by far the most likely one.
    if "nodes" in data and "links" in data:
        raise RegistryError(
            f"{entry_key}: {path.name} is a UI-format workflow, not API format. "
            "Re-export it from ComfyUI with Save (API Format)."
        )

    for node_id, node in data.items():
        if not isinstance(node, dict) or "class_type" not in node or "inputs" not in node:
            raise RegistryError(
                f"{entry_key}: {path.name} node {node_id!r} is not API format "
                "(expected an object with 'class_type' and 'inputs')"
            )
    return data


def _require_node(template: dict, node_id: str, entry_key: str, what: str) -> dict:
    node = template.get(node_id)
    if node is None:
        raise RegistryError(
            f"{entry_key}: {what} refers to node {node_id!r}, which does not exist "
            f"in the template (nodes present: {', '.join(sorted(template))})"
        )
    return node


def validate_entry(key: str, entry: dict, workflows_dir: Path) -> WorkflowInfo:
    """Check one registry entry against its template and return it loaded.

    Every bound node ID must exist and expose the named input field. This is
    the check the spec asks to fail loudly at boot rather than at generation
    time, and it is what `alchemyui validate` runs standalone.
    """
    for required in ("template", "params", "output_node"):
        if required not in entry:
            raise RegistryError(f"{key}: registry entry is missing {required!r}")

    template = _load_template(workflows_dir / entry["template"], key)

    params: dict[str, ParamBinding] = {}
    for name, binding in entry["params"].items():
        if not isinstance(binding, dict) or "node" not in binding or "field" not in binding:
            raise RegistryError(f"{key}: param {name!r} must be {{\"node\": ..., \"field\": ...}}")
        node_id, fld = str(binding["node"]), str(binding["field"])
        node = _require_node(template, node_id, key, f"param {name!r}")
        if fld not in node["inputs"]:
            raise RegistryError(
                f"{key}: param {name!r} binds node {node_id} ({node['class_type']}) field "
                f"{fld!r}, which that node does not expose "
                f"(fields present: {', '.join(sorted(node['inputs']))})"
            )
        params[name] = ParamBinding(node_id, fld)

    output_node = str(entry["output_node"])
    _require_node(template, output_node, key, "output_node")

    progress_node = str(entry.get("progress_node", output_node))
    _require_node(template, progress_node, key, "progress_node")

    aspects = {k: (int(v[0]), int(v[1])) for k, v in (entry.get("aspects") or {}).items()}
    if "width" in params or "height" in params:
        if not aspects:
            raise RegistryError(f"{key}: binds width/height but defines no 'aspects'")

    # A workflow that says it needs an image must actually bind one, or the
    # UI would collect an upload that goes nowhere.
    requires = tuple(entry.get("requires") or ())
    for needed in requires:
        if needed not in params:
            raise RegistryError(
                f"{key}: requires {needed!r} but binds no {needed!r} param, so the "
                f"upload would never reach the graph"
            )

    kind = entry.get("kind", "txt2img")
    if kind not in _KINDS:
        raise RegistryError(f"{key}: unknown kind {kind!r} (want one of {', '.join(_KINDS)})")

    raw_stages = entry.get("stages") or [{"node": progress_node, "label": "", "weight": 1.0}]
    stages = []
    for st in raw_stages:
        node_id = str(st["node"])
        _require_node(template, node_id, key, f"stage {st.get('label', node_id)!r}")
        weight = float(st.get("weight", 1.0))
        if weight <= 0:
            raise RegistryError(f"{key}: stage {node_id} has a non-positive weight")
        stages.append(Stage(node_id, st.get("label", ""), weight))

    est = entry.get("est") or {}
    return WorkflowInfo(
        key=key,
        display=entry.get("display", key),
        template_name=entry["template"],
        checkpoint=entry.get("checkpoint", ""),
        blurb=entry.get("blurb", ""),
        kind=kind,
        requires=requires,
        inputs=tuple(entry.get("inputs", ())),
        params=params,
        defaults=dict(entry.get("defaults") or {}),
        limits={k: (float(v[0]), float(v[1])) for k, v in (entry.get("limits") or {}).items()},
        choices={k: tuple(v) for k, v in (entry.get("choices") or {}).items()},
        aspects=aspects,
        progress_node=progress_node,
        output_node=output_node,
        stages=tuple(stages),
        hq_variant=entry.get("hq_variant"),
        est_seconds_per_step=float(est.get("seconds_per_step", 2.0)),
        est_load_s=float(est.get("load_s", 30.0)),
        template=template,
    )


def load_registry(workflows_dir: str | Path) -> Registry:
    """Load and validate every entry. Raises RegistryError on the first
    problem, naming the entry -- callers at boot turn that into a SystemExit."""
    workflows_dir = Path(workflows_dir)
    path = workflows_dir / "registry.json"
    if not path.exists():
        raise RegistryError(f"workflow registry not found at {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RegistryError(f"{path} is not valid JSON ({e})")

    entries = data.get("workflows")
    if not entries:
        raise RegistryError(f"{path} defines no workflows")

    workflows = {k: validate_entry(k, v, workflows_dir) for k, v in entries.items()}
    # Cross-entry, so it cannot live in validate_entry: a toggle that points at
    # a workflow which isn't there would only fail when someone flicks it.
    for key, w in workflows.items():
        if w.hq_variant and w.hq_variant not in workflows:
            raise RegistryError(
                f"{key}: hq_variant {w.hq_variant!r} is not a workflow in this registry"
            )
        if w.hq_variant == key:
            raise RegistryError(f"{key}: hq_variant points at itself")
    return Registry(workflows)


@lru_cache
def get_registry() -> Registry:
    try:
        return load_registry(get_settings().workflows_dir)
    except RegistryError as e:
        raise SystemExit(
            f"\nworkflow registry is invalid:\n  {e}\n\n"
            "Fix workflows/registry.json or re-export the template, then run\n"
            "  alchemyui validate\n"
        )
