"""Site adapters (SDD §3.7).

The generic adapter observes the network, handles popups and extracts the title. A
site adapter, registered by domain, can add how to start the player (play selectors),
which overlays to close first, and where the title is. Both feed the same playlist
parsing, session isolation and upstream request flow. A ``play_selector`` from the
site's config section (SDD §3.8) is tried before the adapter's own selectors.
"""

from __future__ import annotations

import dataclasses

from hls_harvester.adapters.generic import GENERIC, Adapter

# Site adapters by domain; a key matches the host and its subdomains. None yet: they are
# added from real-site trials in M4.
SITE_ADAPTERS: dict[str, Adapter] = {}


def adapter_for(host: str | None, *, play_selector: str | None = None) -> Adapter:
    """The adapter for ``host``: a registered site adapter, else the generic one."""
    adapter = GENERIC
    if host:
        host = host.lower().rstrip(".")
        matches = [d for d in SITE_ADAPTERS if host == d or host.endswith("." + d)]
        if matches:
            adapter = SITE_ADAPTERS[max(matches, key=len)]
    if play_selector:
        adapter = dataclasses.replace(
            adapter, play_selectors=(play_selector, *adapter.play_selectors)
        )
    return adapter


__all__ = ["GENERIC", "Adapter", "adapter_for"]
