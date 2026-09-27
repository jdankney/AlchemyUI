"""Primary, then fallback, then a plan made of the user's own words.

Each backend gets one repair retry for a plan that parses but does not hold
together, with the validation error fed back verbatim. An unreachable or
timed-out backend is skipped at once. One that cannot see images is skipped
for turns with images -- and if nothing else answers, it is asked again with
the images described rather than shown, because a model that knows "a1 is
attached" still plans "enhance this" better than no model at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import LLMError
from .prompt import Attachment, build_messages, repair_message
from .schema import Plan, PlanInvalid, Vocabulary, plan_schema, validate_plan


@dataclass
class RouteRequest:
    text: str
    attachments: list[Attachment]
    context: list[str]
    has_likeness: bool
    answers: list[dict]
    sources: list[str]           # handles allowed as `source`
    faces: list[str]             # handles allowed as `faces`


@dataclass
class RouteResult:
    plan: Plan | None            # None: the default planner is asking instead
    source: str                  # primary | fallback | default
    ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    question: dict | None = None  # {"question", "options": [{"label", "plan"}]}


def _plan(vocab: Vocabulary, **kw) -> Plan:
    base = dict(action="generate", source="", faces="", prompt="", negative="",
                aspect=vocab.aspects[0], count=1, strength="medium", quality="fast",
                direction="", question="", options=())
    base.update(kw)
    return Plan(**base)


def default_plan(req: RouteRequest, vocab: Vocabulary) -> tuple[Plan | None, dict | None]:
    """No model answered. Words alone are taken literally as the prompt; with
    an image we cannot know what was meant, so offer what the image allows."""
    text = req.text.strip()
    if not req.attachments:
        return _plan(vocab, prompt=text or "a beautiful photograph"), None
    src = req.attachments[0].handle
    offers = [("transform", "Restyle it", {"strength": "medium"}),
              ("enhance", "Enhance it", {}),
              ("extend", "Make it wider", {"direction": "wider"})]
    options = [{"label": label,
                "plan": _plan(vocab, action=action, source=src,
                              prompt=text or "high quality photo", **extra).as_dict()}
               for action, label, extra in offers if action in vocab.actions]
    return None, {"question": "The planner is offline, so I can't tell what you'd like done "
                              "with that image. Pick one:", "options": options}


class Router:
    def __init__(self, vocab: Vocabulary, primary, fallback):
        self.vocab, self.primary, self.fallback = vocab, primary, fallback

    def status(self) -> dict:
        return {"primary": self.primary.status() if self.primary else None,
                "fallback": self.fallback.status() if self.fallback else None}

    async def _try(self, name: str, client, req: RouteRequest, schema: dict,
                   result: RouteResult, images: bool = True) -> str | None:
        """Up to two calls (the second a repair). Returns None on success
        (result.plan set) or the LLMError kind that stopped this backend."""
        messages = build_messages(self.vocab, req.text, req.attachments, req.context,
                                  req.has_likeness, req.answers, images=images)
        for attempt in range(2):
            try:
                reply = await client.complete(messages, schema)
            except LLMError as e:
                result.errors.append(f"{name}: {e}")
                return e.kind
            result.ms += reply.ms
            result.prompt_tokens += reply.prompt_tokens
            result.completion_tokens += reply.completion_tokens
            try:
                result.plan = validate_plan(reply.content, self.vocab, req.sources, req.faces)
                result.source = name
                return None
            except PlanInvalid as e:
                result.errors.append(f"{name}: invalid plan ({e})")
                if attempt == 0:
                    messages = [*messages, {"role": "assistant", "content": reply.content},
                                repair_message(str(e))]
        return "bad_output"

    async def route(self, req: RouteRequest) -> RouteResult:
        result = RouteResult(None, "default")
        schema = plan_schema(self.vocab, req.sources, req.faces)
        blind = None
        for name, client in (("primary", self.primary), ("fallback", self.fallback)):
            if client is None:
                continue
            kind = await self._try(name, client, req, schema, result)
            if kind is None:
                return result
            if kind == "no_vision" and blind is None:
                blind = (name, client)
        if blind is not None and req.attachments:
            if await self._try(blind[0], blind[1], req, schema, result, images=False) is None:
                return result
        result.plan, result.question = default_plan(req, self.vocab)
        return result
