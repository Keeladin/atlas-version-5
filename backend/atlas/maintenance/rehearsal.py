"""Migration/restore rehearsal into a DISPOSABLE target.

Restores an offline snapshot into an empty database and empty state directories,
runs ``alembic upgrade head`` and ``alembic check`` there, verifies schema invariants,
day-one expectations of migration 25a13, artifact integrity and ordinary recall, and
writes a JSON report. It refuses production targets, never starts the runtime, and
never imports the API. Run it from the release you are about to deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import atlas
from atlas.config import Settings

from .backup import restore, verify_artifacts

PRODUCTION_URL_FILE = Path("/etc/atlas-v5/secrets/database-url")
PRODUCTION_DATABASE_NAMES = frozenset({"atlas_v5"})
PRODUCTION_STATE_ROOT = Path("/var/lib/atlas-v5")
EXPECTED_HEAD = "25a13"
DOWNGRADE_TARGET = "25a12"

_RECONCILABLE = "('pending', 'leased', 'retained_short_term', 'awaiting_owner')"

# Each query counts rows that violate the named constraint or partial unique index.
INVARIANT_QUERIES: dict[str, str] = {
    "ck_verified_memory_has_grounding_reference": (
        "SELECT count(*) FROM durable_memories WHERE grounding_status = 'verified' "
        "AND verification_record_id IS NULL AND owner_assertion_turn_id IS NULL"
    ),
    "ck_deleted_memory_payload_shape": (
        "SELECT count(*) FROM durable_memories WHERE status = 'deleted' AND NOT ("
        "content IS NULL AND fingerprint IS NULL AND embedding IS NULL AND embedding_model IS NULL "
        "AND embedding_dimensions IS NULL AND embedded_at IS NULL AND deleted_at IS NOT NULL "
        "AND deletion_operation_id IS NOT NULL AND suppresses_recall IS TRUE)"
    ),
    "ck_nondeleted_memory_has_payload": (
        "SELECT count(*) FROM durable_memories WHERE status <> 'deleted' "
        "AND (content IS NULL OR fingerprint IS NULL)"
    ),
    "uq_active_durable_memory_fingerprint": (
        "SELECT count(*) FROM (SELECT fingerprint FROM durable_memories WHERE status = 'active' "
        "AND fingerprint IS NOT NULL GROUP BY fingerprint HAVING count(*) > 1) duplicates"
    ),
    "uq_durable_memories_originating_candidate_id": (
        "SELECT count(*) FROM (SELECT originating_candidate_id FROM durable_memories "
        "WHERE originating_candidate_id IS NOT NULL GROUP BY originating_candidate_id "
        "HAVING count(*) > 1) duplicates"
    ),
    "ck_reconcilable_candidate_has_payload": (
        f"SELECT count(*) FROM memory_candidates WHERE status IN {_RECONCILABLE} "
        "AND (content IS NULL OR fingerprint IS NULL)"
    ),
    "uq_reconcilable_memory_candidate_evidence": (
        "SELECT count(*) FROM (SELECT evidence_set_hash FROM memory_candidates "
        f"WHERE status IN {_RECONCILABLE} AND evidence_set_hash IS NOT NULL "
        "GROUP BY evidence_set_hash HAVING count(*) > 1) duplicates"
    ),
    "ck_memory_review_dependency_protected": (
        "SELECT count(*) FROM transcripts WHERE kind = 'memory_review' "
        "AND retention_policy <> 'dependency_protected'"
    ),
    "ck_memory_provenance_has_source": (
        "SELECT count(*) FROM memory_provenance WHERE source_turn_id IS NULL "
        "AND source_candidate_id IS NULL AND source_memory_id IS NULL"
    ),
}
CHECK_CONSTRAINT_NAMES = (
    "ck_verified_memory_has_grounding_reference",
    "ck_deleted_memory_payload_shape",
    "ck_nondeleted_memory_has_payload",
    "ck_reconcilable_candidate_has_payload",
    "ck_memory_review_dependency_protected",
    "ck_memory_provenance_has_source",
)
UNIQUE_INDEX_NAMES = (
    "uq_active_durable_memory_fingerprint",
    "uq_durable_memories_originating_candidate_id",
    "uq_reconcilable_memory_candidate_evidence",
)


class RehearsalRefused(ValueError):
    """The target looks like production or is otherwise unsafe to use."""


def app_root() -> Path:
    return Path(atlas.__file__).resolve().parents[2]


def refuse_production_target(
    settings: Settings,
    *,
    production_url_file: Path = PRODUCTION_URL_FILE,
    production_database_names: frozenset[str] = PRODUCTION_DATABASE_NAMES,
    production_state_root: Path = PRODUCTION_STATE_ROOT,
) -> None:
    target = make_url(settings.database_dsn)
    if (target.database or "") in production_database_names:
        raise RehearsalRefused(f"target database {target.database!r} is a production name")
    if production_url_file.is_file():
        try:
            production = make_url(production_url_file.read_text().strip())
        except (OSError, ValueError):
            production = None
        if production is not None and (
            (production.host, production.port or 5432, production.database)
            == (target.host, target.port or 5432, target.database)
        ):
            raise RehearsalRefused("target database is the production database")
    guarded = [
        production_state_root / "artifacts",
        production_state_root / "project-checkpoints",
        production_state_root / "auth",
        production_state_root / "runtime",
    ]
    for label, path in (
        ("artifact-dir", settings.artifact_dir),
        ("checkpoint-dir", settings.project_checkpoint_root),
        ("enrolled-marker", settings.auth_enrolled_marker_file),
    ):
        resolved = Path(path).resolve()
        for root in guarded:
            if resolved == root or resolved.is_relative_to(root):
                raise RehearsalRefused(f"{label} {path} lies inside production state")


def release_identity(root: Path | None = None) -> dict[str, object]:
    root = root or app_root()
    identity: dict[str, object] = {"app_root": str(root)}
    release_file = root / "RELEASE"
    if release_file.is_file():
        identity["release"] = release_file.read_text().strip()
    elif (root / ".git").exists():
        described = subprocess.run(
            ["git", "describe", "--always", "--dirty"], cwd=root, capture_output=True, text=True,
            check=False,
        )
        identity["release"] = described.stdout.strip() if described.returncode == 0 else None
    else:
        identity["release"] = None
    return identity


def alembic_run(*args: str, dsn: str, root: Path | None = None) -> subprocess.CompletedProcess:
    root = root or app_root()
    env = {**os.environ, "ATLAS_DATABASE_URL": dsn}
    env.pop("ATLAS_DATABASE_URL_FILE", None)
    env.pop("PGOPTIONS", None)
    backend = str(root / "backend")
    env["PYTHONPATH"] = backend + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root, env=env, capture_output=True, text=True, check=False,
    )


def alembic_current(dsn: str, root: Path | None = None) -> str | None:
    engine = create_engine(dsn)
    try:
        with engine.connect() as connection:
            exists = connection.execute(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'alembic_version'"
            )).scalar_one()
            if not exists:
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    finally:
        engine.dispose()


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )).scalars())


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
            {"table": table},
        ).scalars())


def _grouped(engine: Engine, sql: str) -> dict[str, int]:
    with engine.connect() as connection:
        return {" / ".join(str(part) for part in row[:-1]): int(row[-1]) for row in connection.execute(text(sql))}


def count_snapshot(engine: Engine) -> dict[str, object]:
    tables = _tables(engine)
    counts: dict[str, object] = {}
    if "durable_memories" in tables:
        counts["durable_memories_by_status"] = _grouped(
            engine, "SELECT status, count(*) FROM durable_memories GROUP BY status ORDER BY status"
        )
        columns = _columns(engine, "durable_memories")
        if {"grounding_status", "origin"} <= columns:
            counts["durable_memories_by_grounding_origin"] = _grouped(
                engine,
                "SELECT grounding_status, origin, count(*) FROM durable_memories "
                "GROUP BY grounding_status, origin ORDER BY grounding_status, origin",
            )
    if "memory_candidates" in tables:
        counts["memory_candidates_by_status"] = _grouped(
            engine, "SELECT status, count(*) FROM memory_candidates GROUP BY status ORDER BY status"
        )
    if "memory_obligations" in tables:
        counts["memory_obligations_by_kind_status"] = _grouped(
            engine,
            "SELECT kind, status, count(*) FROM memory_obligations GROUP BY kind, status ORDER BY kind, status",
        )
    if "memory_conflicts" in tables:
        counts["memory_conflicts_by_status"] = _grouped(
            engine, "SELECT status, count(*) FROM memory_conflicts GROUP BY status ORDER BY status"
        )
    if "transcripts" in tables:
        columns = _columns(engine, "transcripts")
        if "retention_policy" in columns:
            counts["transcripts_by_kind_retention"] = _grouped(
                engine,
                "SELECT kind, retention_policy, count(*) FROM transcripts GROUP BY kind, retention_policy ORDER BY kind, retention_policy",
            )
        else:
            counts["transcripts_by_kind"] = _grouped(
                engine, "SELECT kind, count(*) FROM transcripts GROUP BY kind ORDER BY kind"
            )
    with engine.connect() as connection:
        if "turns" in tables:
            counts["turns"] = int(connection.execute(text("SELECT count(*) FROM turns")).scalar_one())
            counts["turns_deleted"] = int(connection.execute(
                text("SELECT count(*) FROM turns WHERE deleted_at IS NOT NULL")
            ).scalar_one())
        if "artifacts" in tables:
            counts["artifacts"] = int(connection.execute(text("SELECT count(*) FROM artifacts")).scalar_one())
    return counts


def invariant_violations(engine: Engine) -> dict[str, object]:
    violations: dict[str, int] = {}
    with engine.connect() as connection:
        for name, sql in INVARIANT_QUERIES.items():
            violations[name] = int(connection.execute(text(sql)).scalar_one())
        present_checks = set(connection.execute(text(
            "SELECT conname FROM pg_constraint WHERE contype = 'c'"
        )).scalars())
        present_indexes = set(connection.execute(text("SELECT indexname FROM pg_indexes")).scalars())
        unvalidated = list(connection.execute(text(
            "SELECT conname FROM pg_constraint WHERE NOT convalidated ORDER BY conname"
        )).scalars())
    missing = [name for name in CHECK_CONSTRAINT_NAMES if name not in present_checks]
    missing += [name for name in UNIQUE_INDEX_NAMES if name not in present_indexes]
    return {
        "violations": violations,
        "missing_constraints": missing,
        "unvalidated_constraints": unvalidated,
    }


def day_one_expectations(engine: Engine, before: dict[str, object]) -> dict[str, object]:
    with engine.connect() as connection:
        verified = int(connection.execute(text(
            "SELECT count(*) FROM durable_memories WHERE grounding_status = 'verified'"
        )).scalar_one())
        not_legacy = int(connection.execute(text(
            "SELECT count(*) FROM durable_memories WHERE grounding_status <> 'legacy_unverified' "
            "OR origin <> 'legacy_pre25a13'"
        )).scalar_one())
        active = int(connection.execute(text(
            "SELECT count(*) FROM durable_memories WHERE status = 'active'"
        )).scalar_one())
        pending_reviews = int(connection.execute(text(
            "SELECT count(*) FROM memory_obligations WHERE kind = 'memory_review' AND status = 'pending'"
        )).scalar_one())
        distinct_review_subjects = int(connection.execute(text(
            "SELECT count(DISTINCT subject_id) FROM memory_obligations "
            "WHERE kind = 'memory_review' AND status = 'pending'"
        )).scalar_one())
        reviews_on_non_active = int(connection.execute(text(
            "SELECT count(*) FROM memory_obligations o JOIN durable_memories m ON m.id = o.subject_id "
            "WHERE o.kind = 'memory_review' AND o.subject_type = 'durable_memory' AND m.status <> 'active'"
        )).scalar_one())
    active_before = int(
        (before.get("durable_memories_by_status") or {}).get("active", 0)  # type: ignore[union-attr]
    )
    checks = {
        "no_verified_rows": verified == 0,
        "all_rows_legacy_pre25a13": not_legacy == 0,
        "one_pending_review_per_active_memory": pending_reviews == active == active_before == distinct_review_subjects,
        "no_review_for_non_active_memory": reviews_on_non_active == 0,
    }
    return {
        "verified_rows": verified,
        "rows_not_legacy": not_legacy,
        "active_memories": active,
        "active_memories_before": active_before,
        "pending_reviews": pending_reviews,
        "reviews_on_non_active": reviews_on_non_active,
        "checks": checks,
        "passed": all(checks.values()),
    }


async def _recall_probe(dsn: str) -> dict[str, object]:
    from atlas.memory.durable import DurableMemoryRepository

    engine = create_async_engine(dsn)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            repository = DurableMemoryRepository(session)
            longest = (await session.execute(text(
                "SELECT content FROM durable_memories WHERE content IS NOT NULL "
                "ORDER BY length(content) DESC LIMIT 1"
            ))).scalar_one_or_none()
            probes = ["the"]
            if longest:
                probes.append(" ".join(str(longest).split()[:3]))
            active_hits = 0
            historical_hits = 0
            for query in probes:
                active_hits += len(await repository.search_active(query, limit=10))
                historical_hits += len(await repository.search_historical(query, limit=10))
            state = await repository.state()
            return {
                "probes": probes,
                "active_results": active_hits,
                "historical_results": historical_hits,
                "legacy_unverified_memories": int(state.get("legacy_unverified_memories") or 0),
            }
    finally:
        await engine.dispose()


def recall_probe(dsn: str) -> dict[str, object]:
    return asyncio.run(_recall_probe(dsn))


def _downgrade_rehearsal(
    settings: Settings, snapshot: Path, *, root: Path, failures: list[str]
) -> dict[str, object]:
    dsn = settings.database_dsn
    report: dict[str, object] = {}
    restore(settings, snapshot)
    upgraded = alembic_run("upgrade", "head", dsn=dsn, root=root)
    if upgraded.returncode != 0:
        failures.append("downgrade rehearsal: upgrade head failed")
        report["upgrade"] = upgraded.stderr[-2000:]
        return report
    downgraded = alembic_run("downgrade", DOWNGRADE_TARGET, dsn=dsn, root=root)
    report["clean_downgrade_returncode"] = downgraded.returncode
    report["clean_downgrade_version"] = alembic_current(dsn, root)
    if downgraded.returncode != 0 or report["clean_downgrade_version"] != DOWNGRADE_TARGET:
        failures.append("downgrade rehearsal: clean downgrade did not reach the previous revision")
    reupgraded = alembic_run("upgrade", "head", dsn=dsn, root=root)
    if reupgraded.returncode != 0:
        failures.append("downgrade rehearsal: re-upgrade failed")
        return report
    engine = create_engine(dsn)
    try:
        with engine.begin() as connection:
            resolved = connection.execute(text(
                "UPDATE memory_obligations SET status = 'resolved', resolution_code = 'confirmed', "
                "resolved_at = now() WHERE id = (SELECT id FROM memory_obligations "
                "WHERE kind = 'memory_review' AND status = 'pending' LIMIT 1)"
            )).rowcount
    finally:
        engine.dispose()
    report["review_resolved_for_refusal_check"] = int(resolved or 0)
    refused = alembic_run("downgrade", DOWNGRADE_TARGET, dsn=dsn, root=root)
    combined = (refused.stdout or "") + (refused.stderr or "")
    report["refused_downgrade_returncode"] = refused.returncode
    report["refused_downgrade_message_present"] = "downgrade refused" in combined
    report["version_after_refusal"] = alembic_current(dsn, root)
    if resolved and (
        refused.returncode == 0
        or "downgrade refused" not in combined
        or report["version_after_refusal"] != EXPECTED_HEAD
    ):
        failures.append("downgrade rehearsal: downgrade was not refused after a review resolution")
    return report


def rehearse(
    settings: Settings,
    snapshot: Path,
    *,
    report_path: Path | None = None,
    keep: bool = False,
    rehearse_downgrade: bool = False,
    downgrade_settings: Settings | None = None,
    root: Path | None = None,
    production_url_file: Path = PRODUCTION_URL_FILE,
    production_database_names: frozenset[str] = PRODUCTION_DATABASE_NAMES,
    production_state_root: Path = PRODUCTION_STATE_ROOT,
) -> dict[str, object]:
    root = root or app_root()
    snapshot = snapshot.resolve()
    failures: list[str] = []
    report: dict[str, object] = {
        "status": "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "release": release_identity(root),
        "snapshot": {"path": str(snapshot)},
        "runtime_started": False,
    }
    guard = {
        "production_url_file": production_url_file,
        "production_database_names": production_database_names,
        "production_state_root": production_state_root,
    }
    refuse_production_target(settings, **guard)
    if rehearse_downgrade:
        if downgrade_settings is None:
            raise RehearsalRefused("downgrade rehearsal needs its own disposable database and directories")
        refuse_production_target(downgrade_settings, **guard)
        if make_url(downgrade_settings.database_dsn).database == make_url(settings.database_dsn).database:
            raise RehearsalRefused("downgrade rehearsal must use a second disposable database")
    manifest_path = snapshot / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        report["snapshot"].update(
            created_at=manifest.get("created_at"), files=len(manifest.get("files") or {})
        )
    dsn = settings.database_dsn
    report["target"] = {
        "database": make_url(dsn).database,
        "artifact_dir": str(settings.artifact_dir),
        "checkpoint_dir": str(settings.project_checkpoint_root),
        "enrolled_marker": str(settings.auth_enrolled_marker_file),
    }
    created_paths = [
        Path(settings.artifact_dir), Path(settings.project_checkpoint_root),
        Path(settings.auth_enrolled_marker_file),
    ]
    engine: Engine | None = None
    try:
        restore(settings, snapshot)
        report["alembic"] = {"before": alembic_current(dsn, root)}
        engine = create_engine(dsn)
        report["counts_before"] = count_snapshot(engine)
        upgraded = alembic_run("upgrade", "head", dsn=dsn, root=root)
        report["alembic"]["upgrade_returncode"] = upgraded.returncode
        if upgraded.returncode != 0:
            report["alembic"]["upgrade_output"] = (upgraded.stderr or upgraded.stdout)[-4000:]
            failures.append("alembic upgrade head failed")
            return report
        report["alembic"]["after"] = alembic_current(dsn, root)
        if report["alembic"]["after"] != EXPECTED_HEAD:
            failures.append(f"alembic head is {report['alembic']['after']}, expected {EXPECTED_HEAD}")
        checked = alembic_run("check", dsn=dsn, root=root)
        report["alembic"]["check_returncode"] = checked.returncode
        report["alembic"]["check_output"] = ((checked.stdout or "") + (checked.stderr or ""))[-4000:]
        if checked.returncode != 0:
            failures.append("alembic check reported schema drift")
        report["counts_after"] = count_snapshot(engine)
        invariants = invariant_violations(engine)
        report["invariants"] = invariants
        violated = {name: count for name, count in invariants["violations"].items() if count}
        if violated:
            failures.append(f"invariant violations: {sorted(violated)}")
        if invariants["missing_constraints"]:
            failures.append(f"missing constraints: {invariants['missing_constraints']}")
        if invariants["unvalidated_constraints"]:
            failures.append(f"unvalidated constraints: {invariants['unvalidated_constraints']}")
        day_one = day_one_expectations(engine, report["counts_before"])
        report["day_one"] = day_one
        if not day_one["passed"]:
            failures.append("day-one expectations of 25a13 not met")
        try:
            verify_artifacts(dsn, Path(settings.artifact_dir))
            report["artifacts_verified"] = True
        except (ValueError, OSError, SQLAlchemyError) as exc:
            report["artifacts_verified"] = False
            failures.append(f"artifact verification failed: {exc}")
        probe = recall_probe(dsn)
        report["recall_probe"] = probe
        if probe["active_results"] or probe["historical_results"]:
            failures.append("ordinary recall returned rows before any review or grounding")
        if rehearse_downgrade and downgrade_settings is not None:
            created_paths += [
                Path(downgrade_settings.artifact_dir),
                Path(downgrade_settings.project_checkpoint_root),
                Path(downgrade_settings.auth_enrolled_marker_file),
            ]
            report["downgrade"] = _downgrade_rehearsal(
                downgrade_settings, snapshot, root=root, failures=failures
            )
    finally:
        if engine is not None:
            engine.dispose()
        report["failures"] = failures
        report["status"] = "passed" if not failures else "failed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        if not keep:
            for path in created_paths:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.is_file():
                    path.unlink(missing_ok=True)
            report["cleanup"] = "restored directories removed; drop the rehearsal database yourself"
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--enrolled-marker", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep", action="store_true", help="Keep restored directories after the run")
    parser.add_argument("--rehearse-downgrade", action="store_true")
    parser.add_argument("--downgrade-database-url-file", type=Path)
    parser.add_argument("--downgrade-state-dir", type=Path, help="Empty directory for the downgrade copy")
    args = parser.parse_args()
    settings = Settings(
        database_url_file=args.database_url_file,
        artifact_dir=args.artifact_dir,
        project_checkpoint_root=args.checkpoint_dir,
        auth_enrolled_marker_file=args.enrolled_marker,
    )
    downgrade_settings = None
    if args.rehearse_downgrade:
        if args.downgrade_database_url_file is None or args.downgrade_state_dir is None:
            parser.error("--rehearse-downgrade needs --downgrade-database-url-file and --downgrade-state-dir")
        downgrade_settings = Settings(
            database_url_file=args.downgrade_database_url_file,
            artifact_dir=args.downgrade_state_dir / "artifacts",
            project_checkpoint_root=args.downgrade_state_dir / "project-checkpoints",
            auth_enrolled_marker_file=args.downgrade_state_dir / "auth" / "enrolled",
        )
    try:
        report = rehearse(
            settings, args.snapshot, report_path=args.report, keep=args.keep,
            rehearse_downgrade=args.rehearse_downgrade, downgrade_settings=downgrade_settings,
        )
    except (SQLAlchemyError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"Rehearsal aborted: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
