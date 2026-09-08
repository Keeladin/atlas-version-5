from uuid import UUID, uuid4

import pytest
from atlas.memory.indexer import TranscriptIndexer, _chunk_rows, _turn_text
from atlas.memory.repository import MemorySearchRepository
from atlas.persistence.models import TranscriptIndexChunkRow, TranscriptRow, TurnRow
from sqlalchemy import func, select


def _row(sequence: int, actor: str, text: str) -> TurnRow:
    return TurnRow(
        id=uuid4(),
        transcript_id=uuid4(),
        sequence=sequence,
        actor=actor,
        blocks=[{"type": "text", "text": text}],
    )


def test_chunker_preserves_whole_turns_and_actor_labels() -> None:
    first = _row(1, "owner", "alpha " * 40)
    second = _row(2, "atlas", "beta " * 40)
    chunks = _chunk_rows([first, second], max_chars=200)
    assert len(chunks) == 2
    assert chunks[0][0] == [first]
    assert chunks[0][1].startswith("owner: alpha")
    assert chunks[1][1].startswith("atlas: beta")


def test_tool_index_text_keeps_summary_not_raw_detail() -> None:
    row = TurnRow(
        id=uuid4(), transcript_id=uuid4(), sequence=1, actor="tool",
        blocks=[{
            "type": "tool_observation",
            "operation": "demo.read",
            "phase": "succeeded",
            "summary": "Found the requested record",
            "detail": {"secret": "do-not-index"},
        }],
    )
    text = _turn_text(row)
    assert "demo.read [succeeded]" in text
    assert "Found the requested record" in text
    assert "do-not-index" not in text


@pytest.mark.asyncio
async def test_indexer_and_lexical_search_are_incremental_and_exclude_live_tail(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=6)
        session.add(transcript)
        await session.flush()
        texts = [
            (1, "owner", "We decided Caddy is host infrastructure outside Atlas Control."),
            (2, "atlas", "That keeps the reverse proxy outside the product control surface."),
            (3, "owner", "The memory index should retain exact transcript provenance."),
            (4, "atlas", "The searchable chunks will point back to canonical turn IDs."),
            (5, "owner", "The current topic is a calculator experiment."),
            (6, "atlas", "This newest exchange must stay out of the background index for now."),
        ]
        for sequence, actor, text in texts:
            session.add(TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=sequence,
                actor=actor, blocks=[{"type": "text", "text": text}],
            ))
        await session.commit()

    async with pg_factory() as session:
        result = await TranscriptIndexer(session, max_chars=220).run_once(active_tail_exchanges=1)
        await session.commit()
        assert result.transcripts_advanced == 1
        assert result.turns_processed == 4
        assert result.chunks_created >= 1

    async with pg_factory() as session:
        repository = MemorySearchRepository(session)
        matches = await repository.search("Caddy infrastructure", limit=5)
        coverage = await repository.coverage()
        assert coverage["transcripts_processed"] == 1
        assert coverage["chunks"] >= 1
        assert matches
        assert "Caddy" in str(matches[0]["content"])
        assert matches[0]["source_turn_ids"]
        excluded = await repository.search(
            "Caddy infrastructure", limit=5,
            exclude_chunk_ids=[UUID(str(matches[0]["chunk_id"]))],
        )
        assert all(item["chunk_id"] != matches[0]["chunk_id"] for item in excluded)
        assert await repository.search("calculator experiment", limit=5) == []

        count_before = (await session.execute(select(func.count()).select_from(TranscriptIndexChunkRow))).scalar_one()
        again = await TranscriptIndexer(session, max_chars=220).run_once(active_tail_exchanges=1)
        await session.commit()
        count_after = (await session.execute(select(func.count()).select_from(TranscriptIndexChunkRow))).scalar_one()
        assert again.turns_processed == 0
        assert count_after == count_before


class _FakeEmbeddingClient:
    model = "test-embedding-v1"
    dimensions = 1_536

    async def embed(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "future" in lowered or "picture" in lowered or "portrait" in lowered:
                vectors.append([1.0, 0.0] + [0.0] * 1_534)
            else:
                vectors.append([0.0, 1.0] + [0.0] * 1_534)
        return vectors


@pytest.mark.asyncio
async def test_semantic_search_recovers_related_wording_without_lexical_match(pg_factory) -> None:
    from atlas.memory.embeddings import TranscriptEmbeddingIndexer

    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=4)
        session.add(transcript)
        await session.flush()
        session.add_all([
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
                blocks=[{"type": "text", "text": "I showed you Jaco_Future.jpg."}],
            ),
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=2, actor="atlas",
                blocks=[{"type": "text", "text": "It was a stylized sci-fi portrait with dark hair and futuristic armour. " + "futuristic armour detail " * 8}],
            ),
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=3, actor="owner",
                blocks=[{"type": "text", "text": "Caddy stays outside the Atlas runtime. " + "host infrastructure boundary " * 14}],
            ),
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=4, actor="atlas",
                blocks=[{"type": "text", "text": "The reverse proxy remains host infrastructure."}],
            ),
        ])
        await session.commit()

    async with pg_factory() as session:
        indexed = await TranscriptIndexer(session, max_chars=500).run_once(active_tail_exchanges=0)
        await session.commit()
        assert indexed.chunks_created >= 2

    semantic = await TranscriptEmbeddingIndexer(pg_factory, _FakeEmbeddingClient()).run_once(
        batch_size=2, max_chunks=10
    )
    assert semantic["chunks_embedded"] == indexed.chunks_created
    second_semantic = await TranscriptEmbeddingIndexer(
        pg_factory, _FakeEmbeddingClient()
    ).run_once(batch_size=2, max_chunks=10)
    assert second_semantic["chunks_embedded"] == 0

    async with pg_factory() as session:
        repository = MemorySearchRepository(session)
        query_vector = [1.0, 0.0] + [0.0] * 1_534
        matches = await repository.search(
            "picture",
            limit=5,
            query_embedding=query_vector,
            embedding_model=_FakeEmbeddingClient.model,
        )
        assert matches
        assert "Jaco_Future.jpg" in str(matches[0]["content"])
        assert matches[0]["lexical_rank"] is None
        assert "semantic" in matches[0]["retrieval_sources"]
        assert float(matches[0]["semantic_similarity"]) > 0.99
        coverage = await repository.coverage(
            embedding_model=_FakeEmbeddingClient.model,
            embedding_dimensions=_FakeEmbeddingClient.dimensions,
        )
        assert coverage["embedded_chunks"] == indexed.chunks_created


class _FailingEmbeddingClient:
    model = "test-embedding-v1"
    dimensions = 1_536

    async def embed(self, texts):
        from atlas.memory.embeddings import EmbeddingError

        raise EmbeddingError("fixture provider unavailable")


@pytest.mark.asyncio
async def test_memory_search_falls_back_to_lexical_when_embedding_provider_fails(pg_factory) -> None:
    from atlas.memory.service import MemoryService

    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2)
        session.add(transcript)
        await session.flush()
        session.add_all([
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
                blocks=[{"type": "text", "text": "Remember that Caddy is our reverse proxy."}],
            ),
            TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=2, actor="atlas",
                blocks=[{"type": "text", "text": "Caddy stays outside the Atlas runtime."}],
            ),
        ])
        await session.commit()

    async with pg_factory() as session:
        await TranscriptIndexer(session).run_once(active_tail_exchanges=0)
        await session.commit()

    result = await MemoryService(pg_factory, embedder=_FailingEmbeddingClient()).search(
        {"query": "Caddy", "limit": 5}
    )
    assert result["retrieval"]["mode"] == "lexical_fallback"
    assert result["results"]
    assert "Caddy" in str(result["results"][0]["content"])
    assert "lexical" in result["results"][0]["retrieval_sources"]
