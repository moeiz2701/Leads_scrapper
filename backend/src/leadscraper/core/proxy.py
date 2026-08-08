"""Egress routing (§7.1).

A PK residential proxy is the only genuinely required paid input, and it exists
for **correctness, not evasion**: Google Maps geo-ranks its results, so a US
egress IP answers "salons in Lahore" with the wrong businesses. That distinction
matters for how this module behaves — when no proxy is configured we do not
quietly fall back to direct for Maps and hand you a plausible-looking but wrong
dataset. We refuse to run that source and say why.

Everything else in the core pipeline (business websites, BusinessList, UrduPoint,
PakPlay, Turfy, Zameen) is geo-neutral and runs fine on ``PROXY_MODE=direct``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from leadscraper.config import Settings, get_settings


class ProxyMode(StrEnum):
    DIRECT = "direct"
    RESIDENTIAL = "residential"


class ProxyNotConfiguredError(RuntimeError):
    """Raised when a source that requires PK egress is run without a proxy."""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Resolved egress settings for one source, in both client dialects."""

    mode: ProxyMode
    url: str | None

    @property
    def is_direct(self) -> bool:
        return self.mode is ProxyMode.DIRECT or not self.url

    def httpx_proxy(self) -> str | None:
        """Value for ``httpx.AsyncClient(proxy=...)``."""
        return None if self.is_direct else self.url

    def playwright_proxy(self) -> dict[str, str] | None:
        """Value for Playwright's ``launch(proxy=...)``.

        Playwright wants credentials split out of the URL rather than inline.
        """
        if self.is_direct:
            return None
        from urllib.parse import urlsplit

        parts = urlsplit(self.url or "")
        server = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            server = f"{server}:{parts.port}"
        proxy: dict[str, str] = {"server": server}
        if parts.username:
            proxy["username"] = parts.username
        if parts.password:
            proxy["password"] = parts.password
        return proxy


def resolve_proxy(source: str, settings: Settings | None = None) -> ProxyConfig:
    """Return the egress config for ``source``.

    Raises ``ProxyNotConfiguredError`` when the source is listed in
    ``PROXY_REQUIRED_SOURCES`` but no proxy is available — silently returning
    wrong-country data is worse than failing loudly.
    """
    settings = settings or get_settings()
    configured = settings.proxy_mode == ProxyMode.RESIDENTIAL and bool(settings.proxy_url)

    if source in settings.proxy_required_source_set and not configured:
        raise ProxyNotConfiguredError(
            f"Source {source!r} requires a PK residential proxy but PROXY_MODE="
            f"{settings.proxy_mode!r}. Results would be geo-ranked for the wrong "
            f"country and silently incorrect (see implementation.md §7.1). "
            f"Set PROXY_MODE=residential and PROXY_URL, or disable this source."
        )

    if configured:
        return ProxyConfig(mode=ProxyMode.RESIDENTIAL, url=settings.proxy_url)
    return ProxyConfig(mode=ProxyMode.DIRECT, url=None)


def proxy_available(settings: Settings | None = None) -> bool:
    """Cheap check for the UI — should the Google Maps toggle be selectable?"""
    settings = settings or get_settings()
    return settings.proxy_mode == ProxyMode.RESIDENTIAL and bool(settings.proxy_url)
