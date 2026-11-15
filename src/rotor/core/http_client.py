"""Shared httpx client limits for upstream provider calls."""

from httpx import Limits

# Upstream providers (especially ChatGPT Codex and aggregators) intermittently
# reset reused keep-alive connections, surfacing as
# ``RemoteProtocolError: Server disconnected without sending a response``.
# Each gateway request is long-lived (SSE, 10-60s), so connection reuse buys
# little and risks racing a half-closed socket. Disable keep-alive reuse: every
# upstream attempt gets a fresh TCP connection.
UPSTREAM_LIMITS = Limits(max_connections=100, max_keepalive_connections=0)
