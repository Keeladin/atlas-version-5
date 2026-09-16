# Managed task deployment order

The deployment path installs the MCP configuration and socket units after database migration while the old Atlas runtime is stopped. The host-operations socket and coding-agent socket are smoke-tested as the `atlas-v5` identity. The managed-task service is enabled during installation but is not started independently inside the maintenance window; it starts with `atlas-v5.service`.

The coding worker is socket activated and remains a persistent service rather than a per-request process so a Codex child may outlive the MCP request that launched it. The listener socket is group-readable/writable by `atlas-v5`, while the persistent service runs as `jaco` inside its systemd write-path envelope.
