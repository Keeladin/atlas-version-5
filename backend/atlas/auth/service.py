from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from atlas.persistence.models import AuthChallengeRow, AuthCredentialRow, AuthSessionRow

SESSION_COOKIE_NAME = "atlas_session"
class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        rp_id: str,
        rp_name: str,
        origin: str,
        enrollment_code_file: Path,
        enrolled_marker_file: Path,
        session_hours: int = 24,
    ) -> None:
        self.session = session
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.origin = origin
        self.enrollment_code_file = enrollment_code_file
        self.enrolled_marker_file = enrolled_marker_file
        self.session_hours = session_hours

    async def is_enrolled(self) -> bool:
        return (await self.session.scalar(select(AuthCredentialRow.id).limit(1))) is not None

    async def credential_count(self) -> int:
        rows = (await self.session.scalars(select(AuthCredentialRow.id))).all()
        return len(rows)

    def enrollment_code_valid(self, supplied: str) -> bool:
        try:
            expected = self.enrollment_code_file.read_text().strip()
        except FileNotFoundError:
            return False
        return bool(supplied) and hmac.compare_digest(supplied.strip(), expected)
    async def _prune_expired(self) -> None:
        now = datetime.now(UTC)
        await self.session.execute(delete(AuthChallengeRow).where(AuthChallengeRow.expires_at <= now))
        await self.session.execute(delete(AuthSessionRow).where(AuthSessionRow.expires_at <= now))

    async def registration_options(self, enrollment_code: str) -> tuple[UUID, dict]:
        await self._prune_expired()
        if await self.is_enrolled():
            raise ValueError("Atlas already has an owner passkey")
        if not self.enrollment_code_valid(enrollment_code):
            raise PermissionError("Invalid enrollment code")
        user_handle = secrets.token_bytes(32)
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_name="jaco",
            user_display_name="Jaco",
            user_id=user_handle,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        row = AuthChallengeRow(
            kind="register",
            challenge=options.challenge,
            user_handle=user_handle,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.session.add(row)
        await self.session.flush()
        return row.id, options_to_json_dict(options)

    async def login_options(self) -> tuple[UUID, dict]:
        await self._prune_expired()
        credentials = (await self.session.scalars(select(AuthCredentialRow))).all()
        if not credentials:
            raise ValueError("Atlas has no enrolled owner passkey")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        row = AuthChallengeRow(
            kind="login",
            challenge=options.challenge,
            user_handle=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.session.add(row)
        await self.session.flush()
        return row.id, options_to_json_dict(options)

    async def _challenge(self, challenge_id: UUID, kind: str) -> AuthChallengeRow:
        row = await self.session.get(AuthChallengeRow, challenge_id)
        if row is None or row.kind != kind:
            raise ValueError("Authentication challenge was not found")
        if row.expires_at <= datetime.now(UTC):
            await self.session.delete(row)
            raise ValueError("Authentication challenge expired")
        return row

    async def verify_registration(
        self,
        challenge_id: UUID,
        enrollment_code: str,
        credential: dict,
    ) -> str:
        if await self.is_enrolled():
            raise ValueError("Atlas already has an owner passkey")
        if not self.enrollment_code_valid(enrollment_code):
            raise PermissionError("Invalid enrollment code")
        challenge = await self._challenge(challenge_id, "register")
        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            raise PermissionError(f"Passkey registration failed: {exc}") from exc
        response = credential.get("response") if isinstance(credential, dict) else None
        transports = response.get("transports", []) if isinstance(response, dict) else []
        device_type = getattr(verification.credential_device_type, "value", verification.credential_device_type)
        row = AuthCredentialRow(
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
            user_handle=challenge.user_handle or b"",
            transports=[str(item) for item in transports],
            device_type=str(device_type) if device_type is not None else None,
            backed_up=bool(verification.credential_backed_up),
        )
        self.session.add(row)
        await self.session.delete(challenge)
        return await self.create_session()

    def finalize_enrollment(self) -> None:
        self.enrolled_marker_file.parent.mkdir(parents=True, exist_ok=True)
        self.enrolled_marker_file.write_text("enrolled\n")
        self.enrollment_code_file.unlink(missing_ok=True)

    async def verify_login(self, challenge_id: UUID, credential: dict) -> str:
        challenge = await self._challenge(challenge_id, "login")
        credential_id = credential.get("id") if isinstance(credential, dict) else None
        if not isinstance(credential_id, str):
            raise PermissionError("Passkey credential id is missing")
        credential_bytes = base64url_to_bytes(credential_id)
        row = await self.session.scalar(
            select(AuthCredentialRow).where(AuthCredentialRow.credential_id == credential_bytes)
        )
        if row is None:
            raise PermissionError("Passkey is not enrolled for Atlas")
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=row.public_key,
                credential_current_sign_count=row.sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            raise PermissionError(f"Passkey authentication failed: {exc}") from exc
        row.sign_count = verification.new_sign_count
        row.device_type = str(getattr(verification.credential_device_type, "value", verification.credential_device_type))
        row.backed_up = bool(verification.credential_backed_up)
        row.last_used_at = datetime.now(UTC)
        await self.session.delete(challenge)
        return await self.create_session()

    async def create_session(self) -> str:
        await self._prune_expired()
        token = secrets.token_urlsafe(32)
        self.session.add(AuthSessionRow(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
            last_seen_at=datetime.now(UTC),
        ))
        await self.session.flush()
        return token

    async def validate_session(self, token: str | None) -> AuthSessionRow | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await self.session.scalar(
            select(AuthSessionRow).where(AuthSessionRow.token_hash == token_hash)
        )
        if row is None:
            return None
        if row.expires_at <= datetime.now(UTC):
            await self.session.delete(row)
            return None
        now = datetime.now(UTC)
        if row.last_seen_at <= now - timedelta(minutes=5):
            row.last_seen_at = now
        return row

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await self.session.execute(delete(AuthSessionRow).where(AuthSessionRow.token_hash == token_hash))
