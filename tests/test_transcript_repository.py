from datetime import UTC, datetime
from uuid import uuid4

import pytest
from atlas.persistence.models import TranscriptRow
from atlas.transcript.repository import TranscriptRepository


class _Result:
    def __init__(self, one=None):
        self.one = one

    def scalar_one_or_none(self):
        return self.one


class _Session:
    def __init__(self, result=None):
        self.result = result
        self.statement = None
        self.added = []

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.result)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)

    def add(self, row):
        self.added.append(row)


@pytest.mark.asyncio
async def test_active_transcript_query_is_scoped_to_owner_kind() -> None:
    session = _Session()
    await TranscriptRepository(session).get_or_create_active()
    compiled = session.statement.compile()
    assert "transcripts.kind" in str(compiled)
    assert "owner" in compiled.params.values()


@pytest.mark.asyncio
async def test_missing_owner_transcript_creates_owner_not_scheduled() -> None:
    session = _Session()
    transcript = await TranscriptRepository(session).get_or_create_active()
    row = next(item for item in session.added if isinstance(item, TranscriptRow))
    assert transcript.kind == "owner"
    assert row.kind == "owner"


@pytest.mark.asyncio
async def test_existing_owner_transcript_is_reused() -> None:
    row = TranscriptRow(id=uuid4(), kind="owner", created_at=datetime.now(UTC))
    session = _Session(result=row)
    transcript = await TranscriptRepository(session).get_or_create_active()
    assert transcript.id == row.id
    assert session.added == []
