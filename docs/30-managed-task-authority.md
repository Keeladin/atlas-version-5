# Managed task authority separation

Managed tasks deliberately split three concerns that were previously easy to blur:

1. **Task continuity** says what Atlas is trying to finish and what evidence constitutes completion.
2. **Capability execution** performs a concrete operation such as reading a repository, starting Codex, writing a file, or restarting a service.
3. **Owner authority** decides whether that concrete operation is automatic, requires approval, or is forbidden.

The durable task contract owns only the first concern. The capability runtime owns the second. Atlas Control and the operation-authority repository own the third.

This means an owner can approve an end-to-end objective without being asked to reconfirm ordinary automatic steps, while still retaining the existing Ask me / Deny boundaries on consequential operations. A task may remember an approved scope, but that scope cannot elevate an operation's runtime authority.

The coding worker follows the same rule. It runs as the owner identity because source repositories are owner files, but it is exposed only through a structured Coding agent capability and a constrained systemd filesystem envelope. Privileged host changes are not part of that identity boundary; they remain separate structured host operations.
