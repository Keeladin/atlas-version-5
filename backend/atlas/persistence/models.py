from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TranscriptRow(Base):
    __tablename__ = "transcripts"
    __table_args__ = (Index("uq_active_owner_transcript", "kind", unique=True,
        postgresql_where=text("kind = 'owner' AND closed_at IS NULL")),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), default="owner", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_summary: Mapped[str | None] = mapped_column(Text)
    summarized_through_turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    active_task_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    active_task_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    next_turn_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class TurnRow(Base):
    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("transcript_id", "sequence", name="uq_transcript_turn_sequence"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(BigInteger)
    blocks: Mapped[list[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TranscriptIndexChunkRow(Base):
    __tablename__ = "transcript_index_chunks"
    __table_args__ = (
        UniqueConstraint("transcript_id", "index_version", "start_sequence", "end_sequence",
            name="uq_transcript_index_chunk_source_range"),
        Index("ix_transcript_index_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_transcript_index_chunks_embedding_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), index=True)
    index_version: Mapped[str] = mapped_column(String(32), default="text-v1")
    start_sequence: Mapped[int] = mapped_column(BigInteger)
    end_sequence: Mapped[int] = mapped_column(BigInteger)
    source_turn_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    content: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(content, ''))", persisted=True),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1_536))
    embedding_model: Mapped[str | None] = mapped_column(String(128), index=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(BigInteger)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TranscriptIndexStateRow(Base):
    __tablename__ = "transcript_index_state"

    transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True
    )
    index_version: Mapped[str] = mapped_column(String(32), primary_key=True, default="text-v1")
    last_indexed_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContinuityCapsuleRow(Base):
    __tablename__ = "continuity_capsules"
    __table_args__ = (
        UniqueConstraint("transcript_id", "revision", name="uq_continuity_capsule_revision"),
        Index("ix_continuity_capsules_transcript_created", "transcript_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger)
    start_sequence: Mapped[int] = mapped_column(BigInteger)
    end_sequence: Mapped[int] = mapped_column(BigInteger)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DurableMemoryRow(Base):
    __tablename__ = "durable_memories"
    __table_args__ = (
        Index(
            "uq_active_durable_memory_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_durable_memories_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_durable_memories_embedding_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    record_kind: Mapped[str] = mapped_column(String(32), default="owner_directed", index=True)
    content: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    suppresses_recall: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(content, ''))", persisted=True),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1_536))
    embedding_model: Mapped[str | None] = mapped_column(String(128), index=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(BigInteger)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_transcript_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transcripts.id", ondelete="SET NULL"), index=True
    )
    source_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), index=True
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("durable_memories.id", ondelete="SET NULL"), index=True
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("durable_memories.id", ondelete="SET NULL"), index=True
    )
    forgotten_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryCommandRow(Base):
    __tablename__ = "memory_commands"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    arguments_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    source_transcript_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transcripts.id", ondelete="SET NULL"), index=True
    )
    source_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), index=True
    )
    target_memory_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("durable_memories.id", ondelete="SET NULL"), index=True
    )
    replacement_memory_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("durable_memories.id", ondelete="SET NULL"), index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"
    __table_args__ = (
        Index(
            "uq_pending_memory_candidate_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    durability: Mapped[str] = mapped_column(String(32), index=True)
    proposed_action: Mapped[str] = mapped_column(String(32), default="upsert")
    subject: Mapped[str | None] = mapped_column(String(160), index=True)
    namespace: Mapped[str | None] = mapped_column(String(160), index=True)
    evidence: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), index=True
    )
    source_turn_id: Mapped[UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), index=True
    )
    source_provider_evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), index=True
    )
    decision_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SharedResourceVersionRow(Base):
    __tablename__ = "shared_resource_versions"

    resource_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SharedWriteOperationRow(Base):
    __tablename__ = "shared_write_operations"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str] = mapped_column(String(255), index=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    expected_version: Mapped[int | None] = mapped_column(BigInteger)
    payload_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64), index=True)
    source_transcript_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transcripts.id", ondelete="SET NULL"), index=True
    )
    source_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), index=True
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    observed_version: Mapped[int] = mapped_column(BigInteger)
    committed_version: Mapped[int | None] = mapped_column(BigInteger)
    result_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(128))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegistryEntryRow(Base):
    __tablename__ = "registry_entries"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    family: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    provisioned: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    availability: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("uq_foreground_inference", "transcript_id", unique=True,
        postgresql_where=text("kind = 'foreground' AND inference_active")),
        UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_occurrence"))

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    transcript_id: Mapped[UUID | None] = mapped_column(ForeignKey("transcripts.id", ondelete="SET NULL"))
    workspace_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    intent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inference_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    inference_status: Mapped[str] = mapped_column(String(32), default="running", server_default="running")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    schedule_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class ActionRow(Base):
    __tablename__ = "actions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(255), index=True)
    target_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OwnerAttentionRow(Base):
    __tablename__ = "owner_attention"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    action_id: Mapped[UUID | None] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthCredentialRow(Base):
    __tablename__ = "auth_credentials"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    user_handle: Mapped[bytes] = mapped_column(LargeBinary)
    transports: Mapped[list[str]] = mapped_column(JSONB, default=list)
    device_type: Mapped[str | None] = mapped_column(String(32))
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthChallengeRow(Base):
    __tablename__ = "auth_challenges"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    user_handle: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledTaskRow(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    schedule_kind: Mapped[str] = mapped_column(String(16))
    schedule_value: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
