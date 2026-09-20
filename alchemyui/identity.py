"""Identity -- or rather, the deliberate near-absence of it.

AlchemyUI is single user. Bound to 127.0.0.1 with DEV_MODE=1 there is no auth
at all, and nothing in the app is keyed per user: no users table, no user_id
column, no metering.

The one rule kept from Llamacracy is the refusal: if DEV_MODE is unset the app
requires oauth2-proxy's forwarded headers and returns 503 without them, so it
can never accidentally end up serving open on a network. Sharing with friends
over NetBird means registering a second OAuth client in Llamacracy's existing
Dex and running a second oauth2-proxy -- not writing auth code here.

Users are keyed on the OIDC `sub` claim (X-Forwarded-User), never the email:
emails change, `sub` does not. Nothing uses that key yet; it is carried so the
day something does, it is already the right one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from .config import Settings, get_settings

log = logging.getLogger("alchemyui.identity")

_H_SUB = "x-forwarded-user"
_H_EMAIL = "x-forwarded-email"
_H_USERNAME = "x-forwarded-preferred-username"


@dataclass(frozen=True)
class Principal:
    sub: str
    email: str
    display_name: str


async def get_principal(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Principal:
    if settings.is_dev:
        return Principal(settings.dev_sub, settings.dev_email, settings.dev_name)

    sub = request.headers.get(_H_SUB, "").strip()
    email = request.headers.get(_H_EMAIL, "").strip().lower()
    name = request.headers.get(_H_USERNAME, "").strip()
    if not sub or not email:
        log.error(
            "request with no identity headers (X-Forwarded-User/Email) and "
            "DEV_MODE unset -- refusing. Is oauth2-proxy in front? path=%s",
            request.url.path,
        )
        raise HTTPException(
            status_code=503,
            detail="identity headers missing; the app must run behind oauth2-proxy",
        )
    return Principal(sub, email, name or email.split("@")[0])
