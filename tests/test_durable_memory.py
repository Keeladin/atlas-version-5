from uuid import UUID, uuid4

import pytest
from atlas.memory.durable import (
    DELETED,
    FAILED,
    RETIRED,
    SUPERSEDED,
    MemoryMutationError,
)
from atlas.memory.indexer import TranscriptIndexer
from atlas.memory.reconciliation import MemoryReconciliationService
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryDeletionReceiptRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    SharedResourceVersionRow,
    SharedWriteOperationRow,
    TranscriptIndexChunkRow,
    TranscriptIndexStateRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import func, select
from test_memory_explicit_commands import _OwnerClaimModel


async def _publish(pg_factory, queued: dict) -> str:
    """Drive a queued explicit remember/correct through verification and publication."""
    assert queued["status"] == "queued", queued
    result = await MemoryReconciliationService(
        pg_factory, _OwnerClaimModel(pg_factory), lease_seconds=30, max_attempts=3
    ).run_once()
    assert result.reconciled == 1, result
    async with pg_factory() as session:
        obligation = await session.get(MemoryObligationRow, UUID(queued["obligation_id"]))
        assert obligation is not None
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "published"
        return str(obligation.resolution_json["memory_id"])


class _MemoryEmbeddingClient:
    model = "test-memory-embedding-v1"
    dimensions = 1_536

    async def embed(self, texts):
        vectors = []
        for text in texts:
            lowered = text.casefold()
            related = any(
                term in lowered
                for term in ("roses", "phrase", "starling", "bird", "feathered")
            )
            vectors.append(([1.0, 0.0] if related else [0.0, 1.0]) + [0.0] * 1_534)
        return vectors


async def _seed_transcript(
    pg_factory, owner_text: str, atlas_text: str | None = None
) -> tuple[UUID, UUID]:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2 if atlas_text else 1)
        session.add(transcript)
        await session.flush()
        owner_turn = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": owner_text}],
        )
        session.add(owner_turn)
        if atlas_text is not None:
            session.add(TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=2, actor="atlas",
                blocks=[{"type": "text", "text": atlas_text}],
            ))
        await session.commit()
        return transcript.id, owner_turn.id


async def _append_owner_turn(pg_factory, transcript_id: UUID, text: str) -> UUID:
    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        assert transcript is not None
        sequence = int(transcript.next_turn_sequence or 0) + 1
        transcript.next_turn_sequence = sequence
        turn = TurnRow(
            id=uuid4(), transcript_id=transcript_id, sequence=sequence, actor="owner",
            blocks=[{"type": "text", "text": text}],
        )
        session.add(turn)
        await session.commit()
        return turn.id


async def _index_all(pg_factory) -> None:
    async with pg_factory() as session:
        await TranscriptIndexer(session, max_chars=4_000).run_once(active_tail_exchanges=0)
        await session.commit()


@pytest.mark.asyncio
async def test_remember_is_durable_idempotent_and_owner_sourced(pg_factory) -> None:
    _, owner_turn_id = await _seed_transcript(
        pg_factory, "Remember that my workshop bird is a starling."
    )
    service = MemoryService(pg_factory)

    first = await service.remember({"content": "My workshop bird is a starling."})
    assert first["status"] == "queued"
    assert "memory" not in first
    first_id = await _publish(pg_factory, first)
    second = await service.remember({"content": "  my workshop bird is a STARLING.  "})
    second_id = await _publish(pg_factory, second)
    assert first_id == second_id

    async with pg_factory() as session:
        memory = await session.get(DurableMemoryRow, UUID(first_id))
        assert memory is not None
        assert memory.source_turn_id == owner_turn_id
        memories = (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one()
        commands = list((await session.execute(select(MemoryCommandRow))).scalars())
        assert memories == 1
        assert len(commands) == 2
        assert {row.status for row in commands} == {"applied"}
        assert {str(row.replacement_memory_id) for row in commands} == {first_id}


@pytest.mark.asyncio
async def test_retire_legacy_phrase_preserves_transcript_but_blocks_recall(pg_factory) -> None:
    phrase = "Roses are red, violets are blue."
    transcript_id, owner_turn_id = await _seed_transcript(
        pg_factory,
        f"Hey remember this phrase: {phrase}",
        f"Got it: {phrase}",
    )
    await _index_all(pg_factory)
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())

    before = await service.search({"query": "phrase roses violets", "limit": 5})
    assert any("Roses are red" in str(item["content"]) for item in before["results"])

    retired = await service.retire({"content": phrase})
    assert retired["status"] == "applied"
    assert retired["result"] == "retire_guard_created"

    after = await service.search({"query": "phrase roses violets", "limit": 5})
    assert after["durable_memories"] == []
    assert all("Roses are red" not in str(item["content"]) for item in after["results"])
    assert after["memory_policy"]["suppression_guards"] == 1

    async with pg_factory() as session:
        original = await session.get(TurnRow, owner_turn_id)
        assert original is not None
        assert phrase in str(original.blocks)
        transcript = await session.get(TranscriptRow, transcript_id)
        assert transcript is not None
        guard = (
            await session.execute(
                select(DurableMemoryRow).where(DurableMemoryRow.status == RETIRED)
            )
        ).scalar_one()
        assert guard.content == phrase
        assert guard.suppresses_recall is True


@pytest.mark.asyncio
async def test_re_remember_after_retire_clears_suppression(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember that the signal phrase is roses are red.")
    service = MemoryService(pg_factory)
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "The signal phrase is roses are red."})
    )

    retired = await service.retire({"memory_id": memory_id})
    assert retired["result"] == "retired"
    restored = await service.restore({"memory_id": memory_id})
    assert restored["result"] == "restored"
    assert restored["memory"]["memory_id"] == memory_id

    searched = await service.search({"query": "signal phrase roses", "limit": 5})
    assert len(searched["durable_memories"]) == 1
    assert searched["durable_memories"][0]["content"] == "The signal phrase is roses are red."
    assert searched["memory_policy"]["suppression_guards"] == 0

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        assert sum(row.status == "active" for row in rows) == 1
        restored_row = next(row for row in rows if str(row.id) == memory_id)
        assert restored_row.status == "active"
        assert restored_row.suppresses_recall is False


@pytest.mark.asyncio
async def test_legacy_correction_supersedes_old_transcript_recall(pg_factory) -> None:
    old = "My preferred bird is a starling."
    new = "My preferred bird is a raven."
    await _seed_transcript(pg_factory, old, "I will remember that preference.")
    await _index_all(pg_factory)
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())

    corrected = await service.correct({"old_content": old, "content": new})
    assert corrected["status"] == "queued"
    new_id = await _publish(pg_factory, corrected)

    searched = await service.search({"query": "preferred bird", "limit": 5})
    assert searched["durable_memories"][0]["content"] == new

    assert all(old not in str(item["content"]) for item in searched["results"])
    assert searched["memory_policy"]["suppression_guards"] == 1

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        guard = next(row for row in rows if row.content == old)
        replacement = next(row for row in rows if row.content == new)
        assert guard.status == SUPERSEDED
        assert guard.suppresses_recall is True
        assert replacement.status == "active"
        assert str(replacement.id) == new_id


@pytest.mark.asyncio
async def test_failed_memory_command_is_auditable(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Retire the impossible memory target.")
    service = MemoryService(pg_factory)

    with pytest.raises(MemoryMutationError):
        await service.retire({"memory_id": str(uuid4())})

    ledger = await service.commands({})
    assert ledger["state"]["failed_commands"] == 1
    assert ledger["state"]["pending_commands"] == 0
    assert ledger["commands"][0]["operation"] == "retire"
    assert ledger["commands"][0]["status"] == FAILED
    assert "target memory does not exist" in str(ledger["commands"][0]["error"])


@pytest.mark.asyncio
async def test_durable_memory_embeddings_join_background_maintenance(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember my preferred bird.")
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "My preferred bird is a starling."})
    )

    maintenance = await service.index_once(active_tail_exchanges=10)
    assert maintenance["embedding_status"] == "ready"
    assert maintenance["durable_memories_embedded"] == 1
    assert maintenance["durable_embedding_batches"] == 1

    searched = await service.search({"query": "feathered companion", "limit": 5})
    assert searched["durable_memories"]
    item = searched["durable_memories"][0]
    assert item["memory_id"] == memory_id
    assert item["lexical_rank"] is None
    assert "semantic" in item["retrieval_sources"]
    assert float(item["semantic_similarity"]) > 0.99

    second = await service.index_once(active_tail_exchanges=10)
    assert second["durable_memories_embedded"] == 0


@pytest.mark.asyncio
async def test_retirement_clears_vector_and_does_not_reembed_guard(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember my temporary bird preference.")
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())
    memory_id = UUID(await _publish(
        pg_factory, await service.remember({"content": "My temporary bird is a starling."})
    ))

    first = await service.index_once(active_tail_exchanges=10)
    assert first["durable_memories_embedded"] == 1
    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, memory_id)
        assert row is not None and row.embedding is not None

    await service.retire({"memory_id": str(memory_id)})
    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, memory_id)
        assert row is not None
        assert row.status == RETIRED
        assert row.embedding is None
        assert row.embedding_model is None

    second = await service.index_once(active_tail_exchanges=10)
    assert second["durable_memories_embedded"] == 0


@pytest.mark.asyncio
async def test_retire_with_stale_memory_id_does_not_fallback_to_content(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Retire a stale memory reference.")
    service = MemoryService(pg_factory)

    with pytest.raises(MemoryMutationError):
        await service.retire({
            "memory_id": str(uuid4()),
            "content": "This must not become a fallback tombstone.",
        })

    ledger = await service.commands({})
    assert ledger["state"]["failed_commands"] == 1
    async with pg_factory() as session:
        memories = (
            await session.execute(select(func.count()).select_from(DurableMemoryRow))
        ).scalar_one()
        assert memories == 0


@pytest.mark.asyncio
async def test_repeated_legacy_retire_reuses_existing_tombstone(pg_factory) -> None:
    phrase = "Roses are red, violets are blue."
    await _seed_transcript(pg_factory, f"Remember this phrase: {phrase}")
    service = MemoryService(pg_factory)

    first = await service.retire({"content": phrase})
    second = await service.retire({"content": phrase})

    assert first["result"] == "retire_guard_created"
    assert second["result"] == "already_retired"
    assert first["memory_id"] == second["memory_id"]

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        assert len(rows) == 1
        assert rows[0].status == RETIRED
        assert rows[0].suppresses_recall is True

@pytest.mark.asyncio
async def test_retire_invalidates_derived_continuity_capsules(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(pg_factory, "Remember the temporary continuity detail.")
    async with pg_factory() as session:
        session.add(ContinuityCapsuleRow(
            transcript_id=transcript_id, revision=1, start_sequence=1, end_sequence=1,
            summary="A paraphrased continuity detail that must be rebuilt after retirement.",
        ))
        await session.commit()

    service = MemoryService(pg_factory)
    await service.retire({"content": "the temporary continuity detail"})

    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(ContinuityCapsuleRow)
        )).scalar_one() == 0

@pytest.mark.asyncio
async def test_re_remember_invalidates_capsules_after_clearing_retirement_guard(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(pg_factory, "Remember the temporary continuity detail.")
    service = MemoryService(pg_factory)
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "The temporary continuity detail."})
    )
    await service.retire({"memory_id": memory_id})

    async with pg_factory() as session:
        session.add(ContinuityCapsuleRow(
            transcript_id=transcript_id, revision=1, start_sequence=1, end_sequence=1,
            summary="The suppressed detail is absent from this derived handoff.",
        ))
        await session.commit()

    await service.restore({"memory_id": memory_id})

    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(ContinuityCapsuleRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_delete_removes_live_payload_preserves_identity_and_rebuilds_redacted_source(pg_factory) -> None:
    from atlas.memory.candidates import MemoryCandidateIntake

    content = "My workshop bird is a starling."
    transcript_id, owner_turn_id = await _seed_transcript(
        pg_factory,
        f"Remember this exactly: {content} Torque note remains 420 Nm.",
    )
    await _index_all(pg_factory)
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())
    memory_id = await _publish(
        pg_factory, await service.remember({"content": content, "kind": "fact"})
    )
    await service.index_once(active_tail_exchanges=10)

    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        assert transcript is not None
        transcript.next_turn_sequence = 2
        provider_evidence = TurnRow(
            id=uuid4(), transcript_id=transcript_id, sequence=2, actor="tool",
            blocks=[{
                "type": "tool_observation",
                "operation": "provider.openai",
                "phase": "observed",
                "summary": "provider.openai · observed",
                "detail": {
                    "memory_candidates": [{
                        "content": "The workshop bird is a starling.",
                        "evidence": content,
                    }]
                },
                "provenance": {},
            }],
        )
        session.add(provider_evidence)
        await session.commit()
        provider_evidence_id = provider_evidence.id

    await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{
            "kind": "fact",
            "content": "The workshop bird is a starling.",
            "scope": "cross_chat",
            "confidence": 0.9,
            "durability": "long_term",
            "proposed_action": "upsert",
            "evidence_refs": [
                {"turn_id": str(owner_turn_id), "span_ref": "text:0"}
            ],
        }],
        source_transcript_id=transcript_id,
        source_turn_id=owner_turn_id,
        source_provider_evidence_id=provider_evidence_id,
    )
    async with pg_factory() as session:
        session.add(MemoryCommandRow(
            operation="legacy_fixture",
            status="applied",
            arguments_json={"content": content},
            source_transcript_id=transcript_id,
            source_turn_id=owner_turn_id,
        ))
        await session.commit()

    deleted = await service.delete({"memory_id": memory_id})
    assert deleted["result"] == "deleted"
    assert deleted["memory_id"] == memory_id
    assert deleted["scope"] == "memory_and_sources"

    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, UUID(memory_id))
        assert row is not None
        assert row.status == DELETED
        assert row.content is None
        assert row.fingerprint is None
        assert row.embedding is None
        assert row.embedding_model is None
        assert row.embedding_dimensions is None
        assert row.embedded_at is None
        assert row.deleted_at is not None
        assert row.deletion_operation_id is not None
        assert row.suppresses_recall is True

        turn = await session.get(TurnRow, owner_turn_id)
        assert turn is not None
        rendered = str(turn.blocks)
        assert content not in rendered
        assert "[Content deleted by owner]" in rendered
        assert "Torque note remains 420 Nm." in rendered

        candidates = list((await session.execute(select(MemoryCandidateRow))).scalars())
        assert len(candidates) == 2
        for candidate in candidates:
            assert candidate.status == "invalidated"
            assert candidate.content is None
            assert candidate.fingerprint is None
            assert candidate.evidence is None
        obligation = (await session.execute(
            select(MemoryObligationRow).where(MemoryObligationRow.kind == "explicit_remember")
        )).scalar_one()
        assert obligation.status == "resolved"
        assert content not in str(obligation.resolution_json)
        evidence_turn = await session.get(TurnRow, provider_evidence_id)
        assert evidence_turn is not None
        assert "starling" not in str(evidence_turn.blocks).casefold()
        assert evidence_turn.deleted_at is not None
        assert evidence_turn.deletion_operation_id == row.deletion_operation_id

        legacy = (await session.execute(
            select(MemoryCommandRow).where(MemoryCommandRow.operation == "legacy_fixture")
        )).scalar_one()
        assert content not in str(legacy.arguments_json)

        receipt = (await session.execute(select(MemoryDeletionReceiptRow))).scalar_one()
        assert content not in str(receipt.affected_json)
        write = await session.get(SharedWriteOperationRow, row.deletion_operation_id)
        assert write is not None
        assert content not in str(write.result_json)

        chunks = (await session.execute(
            select(func.count()).select_from(TranscriptIndexChunkRow)
        )).scalar_one()
        assert chunks == 0
        state = (await session.execute(select(TranscriptIndexStateRow))).scalar_one()
        assert state.last_indexed_sequence == 0

    await _index_all(pg_factory)
    retained = await service.search({"query": "torque 420", "limit": 5})
    assert any("Torque note remains 420 Nm" in str(item["content"]) for item in retained["results"])
    erased = await service.search({"query": "workshop bird starling", "limit": 5})
    assert erased["durable_memories"] == []
    assert all(content not in str(item["content"]) for item in erased["results"])

    await _append_owner_turn(
        pg_factory, transcript_id, f"Remember it again: {content}"
    )
    reintroduced_id = await _publish(pg_factory, await service.remember({"content": content}))
    assert reintroduced_id != memory_id


@pytest.mark.asyncio
async def test_delete_rolls_back_when_supporting_passage_cannot_be_isolated(pg_factory) -> None:
    _transcript_id, owner_turn_id = await _seed_transcript(
        pg_factory, "Remember that I live in London."
    )
    service = MemoryService(pg_factory)
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "My current city is London."})
    )

    with pytest.raises(MemoryMutationError):
        await service.delete({"memory_id": memory_id})

    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, UUID(memory_id))
        assert row is not None
        assert row.status == "active"
        assert row.content == "My current city is London."
        turn = await session.get(TurnRow, owner_turn_id)
        assert turn is not None
        assert "I live in London" in str(turn.blocks)
        assert turn.deleted_at is None
        receipts = (await session.execute(
            select(func.count()).select_from(MemoryDeletionReceiptRow)
        )).scalar_one()
        assert receipts == 0

    ledger = await service.commands({})
    assert ledger["state"]["failed_commands"] == 1
    assert ledger["commands"][0]["operation"] == "delete"


@pytest.mark.asyncio
async def test_superseded_is_historical_but_retired_is_not_recallable(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(
        pg_factory, "Remember that I live in London."
    )
    service = MemoryService(pg_factory)
    old_id = await _publish(pg_factory, await service.remember({"content": "I live in London."}))
    await _append_owner_turn(pg_factory, transcript_id, "I moved and now live in Lisbon.")

    corrected = await service.correct({
        "memory_id": old_id,
        "content": "I live in Lisbon.",
        "change_type": "change_over_time",
    })
    new_id = await _publish(pg_factory, corrected)

    current = await service.search({"query": "live", "limit": 5})
    assert any(item["memory_id"] == new_id for item in current["durable_memories"])
    assert current["historical_memories"] == []

    historical = await service.search({
        "query": "London", "limit": 5, "include_historical": True
    })
    assert any(item["memory_id"] == old_id for item in historical["historical_memories"])
    old = next(item for item in historical["historical_memories"] if item["memory_id"] == old_id)
    assert old["status"] == SUPERSEDED
    assert old["valid_to"] is not None

    await service.retire({"memory_id": new_id})
    after_retire = await service.search({
        "query": "Lisbon", "limit": 5, "include_historical": True
    })
    assert all(item["memory_id"] != new_id for item in after_retire["durable_memories"])
    assert all(item["memory_id"] != new_id for item in after_retire["historical_memories"])


@pytest.mark.asyncio
async def test_owner_lifecycle_commands_advance_one_memory_revision(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember the revision test fact.")
    service = MemoryService(pg_factory)
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "The revision test fact."})
    )
    async with pg_factory() as session:
        revision = await session.get(SharedResourceVersionRow, ("memory_state", "owner"))
        assert revision is not None and revision.version == 1

    retired = await service.retire({"memory_id": memory_id})
    assert retired["memory_revision"] == 2
    restored = await service.restore({"memory_id": memory_id})
    assert restored["memory_revision"] == 3
    deleted = await service.delete({"memory_id": memory_id, "scope": "memory_only"})
    assert deleted["memory_revision"] == 4

    async with pg_factory() as session:
        revision = await session.get(SharedResourceVersionRow, ("memory_state", "owner"))
        assert revision is not None
        assert revision.version == 4


@pytest.mark.asyncio
async def test_delete_rebuilds_from_start_of_invalidated_multi_turn_chunk(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(
        pg_factory, "Calibration alpha remains useful and unrelated."
    )
    source_turn_id = await _append_owner_turn(
        pg_factory, transcript_id, "Remember this exact fact: The temporary code is ORBIT-7."
    )
    await _index_all(pg_factory)

    async with pg_factory() as session:
        chunks = list((await session.execute(select(TranscriptIndexChunkRow))).scalars())
        assert len(chunks) == 1
        assert chunks[0].start_sequence == 1
        assert chunks[0].end_sequence == 2

    service = MemoryService(pg_factory)
    memory_id = await _publish(
        pg_factory, await service.remember({"content": "The temporary code is ORBIT-7."})
    )
    deleted = await service.delete({"memory_id": memory_id})
    assert deleted["result"] == "deleted"

    async with pg_factory() as session:
        source = await session.get(TurnRow, source_turn_id)
        assert source is not None
        assert "ORBIT-7" not in str(source.blocks)
        state = (await session.execute(select(TranscriptIndexStateRow))).scalar_one()
        assert state.last_indexed_sequence == 0

    await _index_all(pg_factory)
    result = await service.search({"query": "calibration alpha", "limit": 5})
    assert any("Calibration alpha remains useful" in str(item["content"]) for item in result["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["correct", "retire", "delete"])
async def test_id_selected_lifecycle_mutation_suppresses_old_context(pg_factory, operation) -> None:
    phrase = "The temporary signal is ORBIT-19."
    await _seed_transcript(pg_factory, f"Remember this: {phrase}")
    service = MemoryService(pg_factory)
    memory_id = await _publish(pg_factory, await service.remember({"content": phrase}))
    arguments = {"memory_id": memory_id}
    if operation == "correct":
        arguments["content"] = "The replacement signal is ORBIT-20."
    result = await getattr(service, operation)(arguments)
    # A queued correction suppresses the challenged wording immediately, even
    # though the durable change waits for verification.
    assert result["status"] == ("queued" if operation == "correct" else "applied")
    assert result["_context_suppression"]["contents"] == [phrase]


@pytest.mark.asyncio
async def test_owner_assertion_is_path_sensitive_purge_barrier(pg_factory) -> None:
    service = MemoryService(pg_factory)
    assertion_text = "Jaco confirms the independently grounded preference."
    async with pg_factory() as session:
        review = TranscriptRow(
            kind="memory_review", retention_policy="dependency_protected",
            next_turn_sequence=1, content_revision=1,
        )
        session.add(review)
        await session.flush()
        assertion = TurnRow(
            transcript_id=review.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": assertion_text}],
        )
        session.add(assertion)
        await session.flush()

        root = DurableMemoryRow(
            status="active", record_kind="derived", origin="import",
            grounding_status="legacy_unverified", scope="cross_chat",
            durability="long_term", content="Imported root", fingerprint="a" * 64,
            suppresses_recall=False,
        )
        barrier = DurableMemoryRow(
            status="active", record_kind="derived", origin="owner_review_confirmation",
            grounding_status="verified", owner_assertion_turn_id=assertion.id,
            source_transcript_id=review.id, source_turn_id=assertion.id,
            scope="cross_chat", durability="long_term",
            content=assertion_text, fingerprint="b" * 64, suppresses_recall=False,
        )
        alternate = DurableMemoryRow(
            status="active", record_kind="derived", origin="import",
            grounding_status="legacy_unverified", scope="cross_chat",
            durability="long_term", content="Alternate imported branch",
            fingerprint="c" * 64, suppresses_recall=False,
        )
        downstream = DurableMemoryRow(
            status="active", record_kind="derived", origin="derived",
            grounding_status="legacy_unverified", scope="cross_chat",
            durability="long_term", content="Downstream conclusion",
            fingerprint="d" * 64, suppresses_recall=False,
        )
        session.add_all([root, barrier, alternate, downstream])
        await session.flush()
        session.add_all([
            MemoryProvenanceRow(
                memory_id=barrier.id, relationship="derived_from", source_memory_id=root.id
            ),
            MemoryProvenanceRow(
                memory_id=alternate.id, relationship="derived_from", source_memory_id=root.id
            ),
            MemoryProvenanceRow(
                memory_id=downstream.id, relationship="derived_from", source_memory_id=barrier.id
            ),
            MemoryProvenanceRow(
                memory_id=downstream.id, relationship="derived_from", source_memory_id=alternate.id
            ),
        ])
        await session.commit()
        ids = root.id, barrier.id, alternate.id, downstream.id

    result = await service.delete({"memory_id": str(ids[0]), "scope": "memory_only"})
    assert str(ids[1]) in result["affected"]["preserved_memory_ids"]

    async with pg_factory() as session:
        root, barrier, alternate, downstream = [
            await session.get(DurableMemoryRow, memory_id) for memory_id in ids
        ]
        assert root.status == "deleted"
        assert barrier.status == "active"
        assert alternate.status == "deleted"
        # The barrier blocks root -> barrier -> downstream, but the independent
        # root -> alternate -> downstream route still makes downstream purgeable.
        assert downstream.status == "deleted"
        obsolete_edge_count = (await session.execute(
            select(func.count()).select_from(MemoryProvenanceRow).where(
                MemoryProvenanceRow.memory_id == barrier.id,
                MemoryProvenanceRow.source_memory_id == ids[0],
            )
        )).scalar_one()
        assert obsolete_edge_count == 0
        assertion_turn = await session.get(TurnRow, barrier.owner_assertion_turn_id)
        assert assertion_turn is not None and assertion_turn.deleted_at is None
