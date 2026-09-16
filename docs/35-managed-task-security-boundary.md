# Managed task coding security boundary

Atlas itself remains the isolated `atlas-v5` service account. The Coding agent is the only managed-task component that crosses into the owner file identity, and it does so through a group-restricted Unix socket.

The coding service runs as `jaco` with `NoNewPrivileges=yes`, `ProtectSystem=strict`, and `ProtectHome=read-only`. Writable paths are explicitly limited to approved project/workspace roots plus Codex state/cache directories. The Python bridge resolves the requested work directory and rejects paths, including symlink escapes, outside `ATLAS_CODING_ROOTS`.

This boundary intentionally does not provide sudo or a generic root shell. Privileged host work remains a separate structured capability governed by Atlas Control.
