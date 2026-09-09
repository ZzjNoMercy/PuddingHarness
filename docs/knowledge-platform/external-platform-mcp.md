# External Platform MCP boundary

Phase 8.4 defines the new-path discovery contract for an independently running
Platform. PuddingClaw reads the user-owned `knowledge_platform.mcp` section and
produces a descriptor for the external `platform` MCP server. The descriptor
contains an HTTP(S) endpoint, a secret reference (`${ENV_NAME}` or `vault://`),
the pinned protocol version, and an allowlist of public Platform operations.

The discovery module does not resolve secrets, contact the server, register a
local business Tool, or import the legacy `mcp_clients` registry. Legacy MCP
tools remain available only through an explicit rollback/compatibility path.

After discovery, the host may inject a credential resolver and pass the
resulting generic client configuration to its MCP client. The resolver is the
only component allowed to materialize the opaque token. The resulting
inventory is accepted by the framework-neutral Agent capability surface only
when it has a non-empty trusted issuer, unique capability identifiers, and no
authorized write capability.

Example configuration:

```json
{
  "knowledge_platform": {
    "mcp": {
      "enabled": true,
      "endpoint": "https://platform.example.invalid/mcp",
      "auth_ref": "${PUDDINGKNOWLEDGE_MCP_TOKEN}"
    }
  }
}
```

This document records a protocol boundary, not proof that a production Platform
endpoint exists or is reachable.
