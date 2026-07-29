from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from potato_gateway.config import get_settings


AUTH_SCHEME = "Bearer "


def require_bearer_token(request: Request) -> None:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith(AUTH_SCHEME):
        raise_unauthorized()

    provided_token = authorization[len(AUTH_SCHEME) :].strip()
    if not provided_token:
        raise_unauthorized()

    if not secrets.compare_digest(provided_token, settings.gateway_token):
        raise_unauthorized()


def raise_unauthorized() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )
