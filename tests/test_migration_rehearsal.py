"""Migration/restore rehearsal: restore a pre-25a13 snapshot into a disposable target,
migrate to head, and verify invariants, day-one state, artifacts and recall."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from atlas.artifacts.store import ArtifactStore
from atlas.config import Settings
from atlas.maintenance import rehearsal
from atlas.maintenance.backup import backup
from atlas.memory.durable import memory_fingerprint
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def _database_url() -> str:
    url = os.environ.get("ATLAS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set ATLAS_TEST_DATABASE_URL to a disposable PostgreSQL database")
    return url


class _Databases:
    def __init__(self, url: str, names: list[str]) -> None:
        self.admin_url = make_url(url)
        self.admin = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        self.names = names
        with self.admin.connect() as connection:
            for name in names:
                connection.execute(text(f"CREATE DATABASE {name}"))

    def url(self, name: str) -> str:
        return self.admin_url.set(database=name).render_as_string(hide_password=False)

    def drop(self) -> None:
        with self.admin.connect() as connection:
            for name in self.names:
                connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        self.admin.dispose()


def _insert(connection, table: str, values: dict[str, object]) -> None:
    columns = set(connection.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
        {"table": table},
    ).scalars())
    present = {key: value for key, value in values.items() if key in columns}
    names = ", ".join(present)
    params = ", ".join(f":{key}" for key in present)
    connection.execute(text(f"INSERT INTO {table} ({names}) VALUES ({params})"), present)


def _seed_pre25a13_snapshot(tmp_path: Path, source_url: str) -> tuple[Settings, Path, dict[str, object]]:
    engine = create_engine(source_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        upgraded = rehearsal.alembic_run("upgrade", "25a12", dsn=source_url, root=ROOT)
        assert upgraded.returncode == 0, upgraded.stderr
        source = Settings(
            database_url=source_url, database_url_file=None,
            artifact_dir=tmp_path / "source-artifacts",
            project_checkpoint_root=tmp_path / "source-checkpoints",
            auth_enrolled_marker_file=tmp_path / "source-auth" / "enrolled",
        )
        artifact = ArtifactStore(source.artifact_dir).put(
            b"Exact artifact evidence\x00\xff", media_type="application/octet-stream", source="fixture",
        )
        source.project_checkpoint_root.mkdir()
        (source.project_checkpoint_root / "change.zip").write_bytes(b"Fixture pending change bundle")
        source.auth_enrolled_marker_file.parent.mkdir()
        source.auth_enrolled_marker_file.write_text("enrolled")
        memories = {
            "owner": ("Jaco prefers detailed shift reports.", "owner_directed", "active"),
            "derived": ("Jaco prefers local-first tools.", "derived", "active"),
            "retired": ("Jaco used to prefer paper logbooks.", "owner_directed", "retired"),
        }
        ids: dict[str, object] = {}
        with engine.begin() as connection:
            transcript_id = uuid4()
            _insert(connection, "transcripts", {
                "id": transcript_id, "kind": "owner", "title": "Rehearsal fixture",
                "next_turn_sequence": 3, "content_revision": 2, "active_task_state": "{}",
            })
            for sequence, actor, body in ((1, "owner", "Remember that I prefer detailed shift reports."),
                                          (2, "atlas", "Noted.")):
                _insert(connection, "turns", {
                    "id": uuid4(), "transcript_id": transcript_id, "sequence": sequence,
                    "actor": actor, "blocks": json.dumps([{"type": "text", "text": body}]),
                })
            for key, (content, record_kind, status) in memories.items():
                memory_id = uuid4()
                ids[key] = memory_id
                _insert(connection, "durable_memories", {
                    "id": memory_id, "status": status, "record_kind": record_kind,
                    "memory_kind": "preference", "scope": "cross_chat", "scope_key": "owner",
                    "durability": "long_term", "subject": "Jaco", "content": content,
                    "fingerprint": memory_fingerprint(content),
                    "suppresses_recall": status == "retired",
                    "retired_at": ("now()" if status == "retired" else None),
                    "source_transcript_id": transcript_id,
                })
            if ids.get("retired") is not None:
                connection.execute(
                    text("UPDATE durable_memories SET retired_at = now() WHERE id = :id"),
                    {"id": ids["retired"]},
                )
            _insert(connection, "artifacts", {
                "id": artifact.id, "kind": artifact.kind.value, "filename": artifact.filename,
                "media_type": artifact.media_type, "storage_key": artifact.storage_key,
                "sha256": artifact.sha256, "size_bytes": artifact.size_bytes,
                "source": artifact.source, "provenance": json.dumps({"fixture": True}),
            })
    finally:
        engine.dispose()
    snapshot = tmp_path / "snapshot"
    assert backup(source, snapshot)["status"] == "complete"
    return source, snapshot, ids


def _target(tmp_path: Path, url: str, label: str) -> Settings:
    return Settings(
        database_url=url, database_url_file=None,
        artifact_dir=tmp_path / f"{label}-artifacts",
        project_checkpoint_root=tmp_path / f"{label}-checkpoints",
        auth_enrolled_marker_file=tmp_path / f"{label}-auth" / "enrolled",
    )


_HERMETIC = {
    "production_url_file": Path("/nonexistent/atlas-rehearsal/database-url"),
    "production_state_root": Path("/nonexistent/atlas-rehearsal/state"),
}


def test_rehearsal_restores_pre_25a13_snapshot_and_verifies_day_one_state(tmp_path):
    databases = _Databases(_database_url(), [f"rehearsal_source_{uuid4().hex}", f"rehearsal_target_{uuid4().hex}"])
    try:
        _, snapshot, _ = _seed_pre25a13_snapshot(tmp_path, databases.url(databases.names[0]))
        target = _target(tmp_path, databases.url(databases.names[1]), "target")
        report_path = tmp_path / "report.json"
        report = rehearsal.rehearse(target, snapshot, report_path=report_path, root=ROOT, **_HERMETIC)
        assert report["failures"] == []
        assert report["status"] == "passed"
        assert report["runtime_started"] is False
        assert report["alembic"]["before"] == "25a12"
        assert report["alembic"]["after"] == "25a13"
        assert report["alembic"]["upgrade_returncode"] == 0
        assert report["alembic"]["check_returncode"] == 0
        assert report["counts_before"]["durable_memories_by_status"] == {"active": 2, "retired": 1}
        assert "durable_memories_by_grounding_origin" not in report["counts_before"]
        assert report["counts_after"]["durable_memories_by_grounding_origin"] == {
            "legacy_unverified / legacy_pre25a13": 3
        }
        assert report["counts_after"]["memory_obligations_by_kind_status"] == {
            "memory_review / pending": 2
        }
        assert report["counts_after"]["transcripts_by_kind_retention"] == {"owner / standard": 1}
        assert report["counts_after"]["artifacts"] == 1
        assert report["counts_after"]["turns"] == 2
        assert set(report["invariants"]["violations"]) == set(rehearsal.INVARIANT_QUERIES)
        assert all(count == 0 for count in report["invariants"]["violations"].values())
        assert report["invariants"]["missing_constraints"] == []
        assert report["invariants"]["unvalidated_constraints"] == []
        assert report["day_one"]["passed"] is True
        assert report["day_one"]["pending_reviews"] == 2
        assert report["day_one"]["verified_rows"] == 0
        assert report["artifacts_verified"] is True
        assert report["recall_probe"]["active_results"] == 0
        assert report["recall_probe"]["historical_results"] == 0
        assert report["recall_probe"]["legacy_unverified_memories"] == 3
        assert report["target"]["database"] == databases.names[1]
        assert "password" not in json.dumps(report).casefold()
        written = json.loads(report_path.read_text())
        assert written["status"] == "passed"
        # Restored directories are removed after a run unless --keep is passed.
        assert not target.artifact_dir.exists()
        assert not target.auth_enrolled_marker_file.exists()
    finally:
        databases.drop()


def test_rehearsal_downgrade_branch_reports_refusal_after_review_resolution(tmp_path):
    databases = _Databases(_database_url(), [
        f"rehearsal_source_{uuid4().hex}", f"rehearsal_target_{uuid4().hex}", f"rehearsal_down_{uuid4().hex}",
    ])
    try:
        _, snapshot, _ = _seed_pre25a13_snapshot(tmp_path, databases.url(databases.names[0]))
        target = _target(tmp_path, databases.url(databases.names[1]), "target")
        downgrade = _target(tmp_path, databases.url(databases.names[2]), "downgrade")
        report = rehearsal.rehearse(
            target, snapshot, root=ROOT, keep=True, rehearse_downgrade=True,
            downgrade_settings=downgrade, **_HERMETIC,
        )
        assert report["failures"] == []
        branch = report["downgrade"]
        assert branch["clean_downgrade_returncode"] == 0
        assert branch["clean_downgrade_version"] == "25a12"
        assert branch["review_resolved_for_refusal_check"] == 1
        assert branch["refused_downgrade_returncode"] != 0
        assert branch["refused_downgrade_message_present"] is True
        assert branch["version_after_refusal"] == "25a13"
        assert target.artifact_dir.exists()
        with pytest.raises(rehearsal.RehearsalRefused, match="second disposable database"):
            rehearsal.rehearse(
                target, snapshot, root=ROOT, rehearse_downgrade=True, downgrade_settings=target, **_HERMETIC,
            )
    finally:
        databases.drop()


def test_rehearsal_refuses_production_targets(tmp_path):
    state_root = tmp_path / "prod-state"
    url_file = tmp_path / "prod-database-url"
    url_file.write_text("postgresql+psycopg://atlas_v5:secret@127.0.0.1/atlas_live\n")
    guard = {"production_url_file": url_file, "production_state_root": state_root}

    named = Settings(
        database_url="postgresql+psycopg://x@127.0.0.1/atlas_v5", database_url_file=None,
        artifact_dir=tmp_path / "a", project_checkpoint_root=tmp_path / "c",
        auth_enrolled_marker_file=tmp_path / "m",
    )
    with pytest.raises(rehearsal.RehearsalRefused, match="production name"):
        rehearsal.refuse_production_target(named, **guard)

    same_as_secret = Settings(
        database_url="postgresql+psycopg://other@127.0.0.1/atlas_live", database_url_file=None,
        artifact_dir=tmp_path / "a", project_checkpoint_root=tmp_path / "c",
        auth_enrolled_marker_file=tmp_path / "m",
    )
    with pytest.raises(rehearsal.RehearsalRefused, match="production database"):
        rehearsal.refuse_production_target(same_as_secret, **guard)

    inside_state = Settings(
        database_url="postgresql+psycopg://x@127.0.0.1/atlas_v5_rehearsal", database_url_file=None,
        artifact_dir=state_root / "artifacts" / "rehearsal", project_checkpoint_root=tmp_path / "c",
        auth_enrolled_marker_file=tmp_path / "m",
    )
    with pytest.raises(rehearsal.RehearsalRefused, match="inside production state"):
        rehearsal.refuse_production_target(inside_state, **guard)

    disposable = Settings(
        database_url="postgresql+psycopg://x@127.0.0.1/atlas_v5_rehearsal", database_url_file=None,
        artifact_dir=tmp_path / "a", project_checkpoint_root=tmp_path / "c",
        auth_enrolled_marker_file=tmp_path / "m",
    )
    rehearsal.refuse_production_target(disposable, **guard)
    with pytest.raises(rehearsal.RehearsalRefused):
        rehearsal.rehearse(named, tmp_path / "missing-snapshot", root=ROOT, **guard)
