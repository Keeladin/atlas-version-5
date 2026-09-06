from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from atlas.config import get_settings
from atlas.db import get_session_factory

from .service import SESSION_COOKIE_NAME, AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()
_auth_attempts: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(request: Request, *, limit: int = 30, window_seconds: int = 60) -> None:
    forwarded = request.headers.get("x-forwarded-for", "")
    key = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
    if key not in _auth_attempts and len(_auth_attempts) >= 4096:
        key = "__overflow__"
    now = monotonic()
    attempts = _auth_attempts[key]
    while attempts and attempts[0] <= now - window_seconds:
        attempts.popleft()
    if len(attempts) >= limit:
        raise HTTPException(status_code=429, detail="Too many authentication attempts; try again shortly")
    attempts.append(now)


class EnrollmentRequest(BaseModel):
    enrollment_code: str


class CredentialRequest(BaseModel):
    challenge_id: UUID
    credential: dict[str, Any]
    enrollment_code: str | None = None


def _service(session) -> AuthService:
    return AuthService(
        session,
        rp_id=settings.auth_rp_id,
        rp_name=settings.auth_rp_name,
        origin=settings.auth_origin,
        enrollment_code_file=settings.auth_enrollment_code_file,
        enrolled_marker_file=settings.auth_enrolled_marker_file,
        session_hours=settings.auth_session_hours,
    )

@router.get("/status")
async def auth_status(request: Request):
    if not settings.auth_required:
        return {"required": False, "enrolled": False, "authenticated": True}
    factory = get_session_factory()
    async with factory() as session:
        service = _service(session)
        enrolled = await service.is_enrolled()
        auth_session = await service.validate_session(request.cookies.get(SESSION_COOKIE_NAME))
        await session.commit()
    return {
        "required": True,
        "enrolled": enrolled,
        "authenticated": auth_session is not None,
    }


@router.post("/register/options")
async def register_options(payload: EnrollmentRequest, request: Request):
    _rate_limit(request)
    factory = get_session_factory()
    async with factory() as session:
        service = _service(session)
        try:
            challenge_id, options = await service.registration_options(payload.enrollment_code)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    return {"challenge_id": str(challenge_id), "options": options}


@router.post("/register/verify")
async def register_verify(payload: CredentialRequest, request: Request, response: Response):
    _rate_limit(request)
    factory = get_session_factory()
    async with factory() as session:
        service = _service(session)
        try:
            token = await service.verify_registration(
                payload.challenge_id,
                payload.enrollment_code or "",
                payload.credential,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    _set_session_cookie(response, token)
    return {"status": "authenticated"}

@router.post("/login/options")
async def login_options(request: Request):
    _rate_limit(request)
    factory = get_session_factory()
    async with factory() as session:
        service = _service(session)
        try:
            challenge_id, options = await service.login_options()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    return {"challenge_id": str(challenge_id), "options": options}


@router.post("/login/verify")
async def login_verify(payload: CredentialRequest, request: Request, response: Response):
    _rate_limit(request)
    factory = get_session_factory()
    async with factory() as session:
        service = _service(session)
        try:
            token = await service.verify_login(payload.challenge_id, payload.credential)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    _set_session_cookie(response, token)
    return {"status": "authenticated"}


@router.post("/logout")
async def logout(request: Request, response: Response):
    factory = get_session_factory()
    async with factory() as session:
        await _service(session).logout(request.cookies.get(SESSION_COOKIE_NAME))
        await session.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "signed_out"}

def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        path="/",
    )
