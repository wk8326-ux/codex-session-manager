"""Shared outbound-probe policy.

Both the channel watchdog and the tunnel supervisor talk to endpoints whose
reachability we are trying to measure. Inheriting the Windows system proxy
makes that measurement meaningless:

* A proxy that is down turns into ``network_error`` for a channel that is
  actually healthy, and the user is sent chasing the wrong problem.
* A proxy that answers 502 produces exactly the same detail string as the
  channel's own 502, so the two causes cannot be told apart.
* Measured on this machine, the same channel probe took 2002ms through the
  system proxy and 231ms direct, an 8.7x difference that also inflated every
  supervision pass.

Probes therefore connect directly unless a caller explicitly opts in to the
system proxy.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit
from urllib.request import ProxyHandler


def is_loopback_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def probe_proxy_handler(url: str, *, use_system_proxy: bool = False) -> ProxyHandler | None:
    """Return the proxy handler a probe should use, or ``None`` to inherit.

    ``ProxyHandler({})`` disables proxying for every scheme. Returning ``None``
    means the caller should use an opener that keeps the system proxy settings.
    """

    if use_system_proxy:
        return None
    return ProxyHandler({})
