import re
from pathlib import Path


def _metadata(path: Path) -> tuple[str, str | None]:
    text = path.read_text()
    revision_match = re.search(r'^revision:.*?= ["\']([^"\']+)["\']', text, re.MULTILINE)
    assert revision_match is not None, f"missing revision in {path}"
    down_match = re.search(r'^down_revision:.*?= (None|["\'][^"\']+["\'])', text, re.MULTILINE)
    assert down_match is not None, f"missing down_revision in {path}"
    raw = down_match.group(1)
    return revision_match.group(1), None if raw == "None" else raw.strip("\'\"")


def test_alembic_history_is_one_connected_linear_chain() -> None:
    versions = Path("migrations/versions")
    metadata = dict(_metadata(path) for path in versions.glob("*.py"))
    assert len(metadata) == len(list(versions.glob("*.py")))
    roots = [revision for revision, parent in metadata.items() if parent is None]
    parents = {parent for parent in metadata.values() if parent is not None}
    heads = [revision for revision in metadata if revision not in parents]
    assert len(roots) == 1
    assert len(heads) == 1

    seen = set()
    current = heads[0]
    while current is not None:
        assert current not in seen, "migration history contains a cycle"
        seen.add(current)
        current = metadata[current]
    assert seen == set(metadata), "migration history contains a disconnected branch"


def test_migration_history_contains_execution_reconciliation_state() -> None:
    texts = [path.read_text() for path in Path("migrations/versions").glob("*.py")]
    assert any("execution_started_at" in text for text in texts)


def test_latest_migration_contains_active_task_state() -> None:
    metadata = {revision: (parent, path) for path in Path("migrations/versions").glob("*.py") for revision, parent in [_metadata(path)]}
    parents = {parent for parent, _ in metadata.values() if parent is not None}
    head = next(revision for revision in metadata if revision not in parents)
    text = metadata[head][1].read_text()
    assert "active_task_state" in text
