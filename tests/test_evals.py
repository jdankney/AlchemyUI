"""router-eval's fixture set and scoring. The real runs need real models;
this checks that the set is sound and the arithmetic honest."""

from __future__ import annotations

import json

from alchemyui.evals import EVAL_DIR, load_cases, render_report, run_eval, score
from alchemyui.planner.client import LLMError
from alchemyui.planner.schema import Plan
from tests.test_planner_schema import V
from tests.test_router import GOOD, Scripted

CASES = load_cases()


def P(**kw):
    return Plan.from_dict({**json.loads(GOOD), **kw})


def test_the_fixture_set_is_sound():
    assert len(CASES) >= 30
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        for name in c["attachments"]:
            assert (EVAL_DIR / "images" / name).exists(), (c["id"], name)
        allowed = c["expect"]["action"]
        for a in [allowed] if isinstance(allowed, str) else allowed:
            assert a in V.actions, (c["id"], a)
        src = c["expect"].get("source")
        assert src is None or src in c["sources"], c["id"]


def test_every_spec_example_is_a_case():
    texts = " ".join(c["text"] for c in CASES)
    for example in ("spooky lighthouse at night", "Snowy Forest during Snowstorm",
                    "add me in it", "Nike Pro sweatpants", "make it darker",
                    "use the second one", "now add a dog"):
        assert example in texts, example


def test_score_checks_the_ideal_action_finely_and_accepts_alternatives():
    case = {"expect": {"action": ["transform", "ask"], "source": "t1.2"}}
    assert score(case, P(action="transform", source="t1.2"))[0]
    assert not score(case, P(action="transform", source="t1.1"))[0]
    assert score(case, P(action="ask", question="which?"))[0]      # no source check on ask
    assert not score(case, P(action="generate"))[0]
    assert score(case, None) == (False, "no valid plan")


async def test_run_eval_scores_each_backend_alone():
    cases = [{"id": "a", "text": "x", "attachments": [], "context": [], "has_likeness": False,
              "sources": [], "faces": ["me"], "expect": {"action": "generate"}},
             {"id": "b", "text": "y", "attachments": ["dog.jpg"], "context": [],
              "has_likeness": False, "sources": ["a1"], "faces": ["me", "a1"],
              "expect": {"action": "transform", "source": "a1"}}]
    # b: generate while a photo is attached -- refused, repaired, refused again
    good = Scripted("primary", GOOD, GOOD, GOOD)
    down = Scripted("fallback", LLMError("unreachable", "x"), LLMError("unreachable", "x"))
    also_good = Scripted("backup", GOOD, GOOD, GOOD)
    res = await run_eval(cases, {"primary": good, "fallback": down, "backup": also_good}, V)
    assert [r["ok"] for r in res["rows"]["primary"]] == [True, False]
    assert res["rows"]["primary"][1]["why"] == "no valid plan"
    assert res["rows"]["primary"][1]["repaired"] is True
    assert res["summary"]["primary"]["accuracy"] == 0.5
    assert res["summary"]["fallback"]["ok"] == 0          # the default planner never counts
    assert res["summary"]["backup"]["ok"] == 1            # whatever slot it ran in
    report = render_report(res, cases)
    assert "| primary |" in report and "| fallback |" in report and "**MISS**" in report
