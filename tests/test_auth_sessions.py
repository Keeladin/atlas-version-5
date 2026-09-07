import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from atlas.auth.service import AuthService
from atlas.persistence.models import AuthChallengeRow, AuthSessionRow


class _Session:
    def __init__(self, scalar_value=None, get_value=None):
        self.scalar_value = scalar_value
        self.get_value = get_value
        self.added = []
        self.deleted = []
        self.executed = []

    async def scalar(self, statement):
        return self.scalar_value

    async def execute(self, statement):
        self.executed.append(statement)

    async def get(self, model, key):
        return self.get_value

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)


def _service(session, tmp_path: Path) -> AuthService:
    return AuthService(
        session, rp_id="atlas.example", rp_name="Atlas V5", origin="https://atlas.example",
        enrollment_code_file=tmp_path / "code", enrolled_marker_file=tmp_path / "enrolled", session_hours=24,
    )


@pytest.mark.asyncio
async def test_session_persists_only_hash_of_bearer_token(tmp_path: Path) -> None:
    session = _Session()
    token = await _service(session, tmp_path).create_session()
    row = next(item for item in session.added if isinstance(item, AuthSessionRow))
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token != row.token_hash
    assert len(row.token_hash) == 64


@pytest.mark.asyncio
async def test_expired_session_is_deleted_and_rejected(tmp_path: Path) -> None:
    row = AuthSessionRow(
        token_hash=hashlib.sha256(b"expired").hexdigest(),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        last_seen_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session = _Session(scalar_value=row)
    result = await _service(session, tmp_path).validate_session("expired")
    assert result is None
    assert session.deleted == [row]


@pytest.mark.asyncio
async def test_valid_session_refreshes_last_seen_after_five_minutes(tmp_path: Path) -> None:
    old_seen = datetime.now(UTC) - timedelta(minutes=10)
    row = AuthSessionRow(
        token_hash=hashlib.sha256(b"valid").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(hours=1), last_seen_at=old_seen,
    )
    session = _Session(scalar_value=row)
    result = await _service(session, tmp_path).validate_session("valid")
    assert result is row
    assert row.last_seen_at > old_seen


@pytest.mark.asyncio
async def test_missing_session_token_fails_closed_without_query(tmp_path: Path) -> None:
    session = _Session()
    assert await _service(session, tmp_path).validate_session(None) is None
    assert session.executed == []


@pytest.mark.asyncio
async def test_challenge_kind_mismatch_is_rejected(tmp_path: Path) -> None:
    row = AuthChallengeRow(
        kind="register", challenge=b"abc", user_handle=b"owner",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session = _Session(get_value=row)
    with pytest.raises(ValueError, match="not found"):
        await _service(session, tmp_path)._challenge(row.id, "login")


@pytest.mark.asyncio
async def test_expired_challenge_is_deleted(tmp_path: Path) -> None:
    row = AuthChallengeRow(
        kind="login", challenge=b"abc", user_handle=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = _Session(get_value=row)
    with pytest.raises(ValueError, match="expired"):
        await _service(session, tmp_path)._challenge(row.id, "login")
    assert session.deleted == [row]


def test_finalize_enrollment_consumes_one_time_code(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.write_text("secret\n")
    service = _service(_Session(), tmp_path)
    service.finalize_enrollment()
    assert not code.exists()
    assert (tmp_path / "enrolled").read_text() == "enrolled\n"
