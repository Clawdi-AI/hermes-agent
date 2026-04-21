"""
Drop-in patches for discord.py that let it point at a reverse-proxy
(e.g. msg-router) instead of Discord's first-party hosts.

Problem
-------
discord.py hardcodes two endpoints:

- REST:    ``discord.http.Route.BASE = "https://discord.com/api/v10"``
- Gateway: ``discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL("wss://gateway.discord.gg/")``

There is no per-``Client`` override for either. The ``DISCORD_PROXY``
support that discord.py already has is an HTTP/SOCKS proxy — it routes
traffic but doesn't rewrite hostnames, so it's useless when the proxy
target lives on a different host/scheme (``ws://``, local port, etc.).

Shape of the patch
------------------
These functions monkey-patch the two class attributes at import-time
before any ``Client``/``Bot`` is constructed. They are idempotent and
safe to call with no arguments (no-op). Intended entry point:
``DiscordAdapter.connect()`` → ``maybe_apply_from_config(self.config.extra)``.

Upstream plan
-------------
A discord.py PR that adds first-class per-``Client`` ``base_url`` /
``gateway_url`` kwargs would obsolete this file. Until that lands, keep
this monkey-patch self-contained so rebases against upstream Hermes
don't conflict.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PATCHED: bool = False
_ORIGINAL_ROUTE_BASE: Optional[str] = None
_ORIGINAL_DEFAULT_GATEWAY: Optional[Any] = None


def apply_msgrouter_patch(
    rest_base: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> None:
    """Patch ``discord.http.Route.BASE`` and ``discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY``.

    Call before any ``discord.Client``/``commands.Bot`` is constructed.
    Either argument may be ``None`` to leave that endpoint unpatched.
    Idempotent: a second call with the same values is a no-op.
    """
    global _PATCHED, _ORIGINAL_ROUTE_BASE, _ORIGINAL_DEFAULT_GATEWAY

    if rest_base is None and gateway_url is None:
        return

    import yarl
    import discord.http
    import discord.gateway

    if _ORIGINAL_ROUTE_BASE is None:
        _ORIGINAL_ROUTE_BASE = discord.http.Route.BASE
    if _ORIGINAL_DEFAULT_GATEWAY is None:
        _ORIGINAL_DEFAULT_GATEWAY = discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY

    if rest_base:
        normalized = rest_base.rstrip("/")
        if discord.http.Route.BASE != normalized:
            logger.info(
                "[discord_patches] Overriding Route.BASE %s → %s",
                discord.http.Route.BASE, normalized,
            )
            discord.http.Route.BASE = normalized

    if gateway_url:
        url = yarl.URL(gateway_url)
        if discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY != url:
            logger.info(
                "[discord_patches] Overriding DEFAULT_GATEWAY %s → %s",
                discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY, url,
            )
            discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = url

    _PATCHED = True


def restore_original() -> None:
    """Undo the patch. Intended for test teardown."""
    global _PATCHED, _ORIGINAL_ROUTE_BASE, _ORIGINAL_DEFAULT_GATEWAY

    if not _PATCHED:
        return

    import discord.http
    import discord.gateway

    if _ORIGINAL_ROUTE_BASE is not None:
        discord.http.Route.BASE = _ORIGINAL_ROUTE_BASE
    if _ORIGINAL_DEFAULT_GATEWAY is not None:
        discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = _ORIGINAL_DEFAULT_GATEWAY

    _PATCHED = False
    _ORIGINAL_ROUTE_BASE = None
    _ORIGINAL_DEFAULT_GATEWAY = None


def maybe_apply_from_config(extra: Dict[str, Any]) -> None:
    """Read ``base_url`` / ``gateway_url`` from a platform config ``extra``
    dict (plus ``DISCORD_REST_BASE_URL`` / ``DISCORD_GATEWAY_URL`` env fallbacks)
    and patch discord.py accordingly.

    Config keys (both optional):
        extra.base_url     — REST endpoint (e.g. "http://msg-router:4000/api/v10")
        extra.gateway_url  — WS endpoint   (e.g. "ws://msg-router:4000/")

    Env fallbacks (both optional):
        DISCORD_REST_BASE_URL — takes precedence only when extra.base_url unset.
        DISCORD_GATEWAY_URL   — takes precedence only when extra.gateway_url unset.
    """
    rest_base = extra.get("base_url") or os.getenv("DISCORD_REST_BASE_URL") or None
    gateway_url = extra.get("gateway_url") or os.getenv("DISCORD_GATEWAY_URL") or None
    apply_msgrouter_patch(rest_base=rest_base, gateway_url=gateway_url)
