"""Small, dependency-light performance benchmark tools for Rotor.

The package intentionally uses :mod:`httpx` directly instead of the optional
OpenAI/Anthropic SDKs.  This keeps benchmark runs reproducible and makes it
possible to exercise a local ASGI application without a network connection.
"""

from .metrics import RequestResult, summarize_results, percentile

__all__ = ["RequestResult", "summarize_results", "percentile"]

