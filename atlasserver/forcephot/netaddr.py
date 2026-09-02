"""Client IP addresses: which one a request really came from, and which ones are public.

Deliberately free of imports beyond the standard library, and of Django: the callers include
webhooks (which decides whether the server may be pointed at an address) and the GeoIP lookup in
views (which decides whether an address is worth looking up), and webhooks is a security boundary
that should not have to import the plotting stack to ask these questions. `misc` would be the
obvious home, but it imports plot_atlas_fp, and so matplotlib and astropy, at module scope.
"""

import ipaddress
import typing as t


def address_is_public(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP address is a routable public one.

    One definition rather than one per caller. The users need to agree: the callback sender must
    not be pointed at an address only the server can reach, and the GeoIP lookup must not be
    attempted for one, since the database cannot place it. They drifted apart once before, when
    the lookup carried a hand-written list of prefixes that missed 172.16/12 and every IPv6 form.

    is_global is False for loopback, link-local (including the cloud metadata range), private,
    reserved, multicast and unspecified addresses.
    """
    return ipaddress.ip_address(address).is_global


def _unbracket(hop: str) -> str:
    """Return an X-Forwarded-For entry with any [addr]:port wrapper removed.

    nginx and several load balancers write an IPv6 address with a port in the bracketed form
    "[2001:db8::1]:443", which ipaddress.ip_address() rejects. Only this form is unwrapped: a bare
    "1.2.3.4:8080" cannot be told from an IPv6 address without brackets, so it is left alone and
    simply fails to parse rather than being guessed at.
    """
    if hop.startswith("[") and "]" in hop:
        return hop[1 : hop.index("]")]

    return hop


def client_ip(request: t.Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address a request came from, or None if it cannot be established.

    The policy is client_address below; NUM_PROXIES in REST_FRAMEWORK is set from the same
    TRUSTED_PROXY_COUNT, so the throttle's idea of the client cannot drift from this one.
    """
    from django.conf import settings

    return client_address(
        remote_addr=request.META.get("REMOTE_ADDR"),
        forwarded_for=request.META.get("HTTP_X_FORWARDED_FOR"),
        trusted_proxy_count=settings.TRUSTED_PROXY_COUNT,
    )


def client_address(
    remote_addr: str | None, forwarded_for: str | None, trusted_proxy_count: int
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address a request came from, or None if it cannot be established.

    X-Forwarded-For is only consulted when trusted_proxy_count says a proxy in front of this
    server maintains it, and even then only the hop that proxy itself observed is read. Anything
    further to the left of that was written by the client. Taking the leftmost entry (as this
    did) means the value is whatever the caller typed: it was enough to file a submission under
    any country, and, because the string went to GeoIP2 unparsed, a name there was passed to
    socket.gethostbyname() — an arbitrary DNS lookup performed by the server, with no timeout, on
    a name the caller chose.
    """
    address = remote_addr

    if trusted_proxy_count > 0 and forwarded_for:
        hops = [stripped for hop in forwarded_for.split(",") if (stripped := hop.strip())]
        # a chain shorter than the trusted proxies claim to have added is a forged header, not a
        # client address: fall back to remote_addr (the nearest proxy) rather than reading a hop
        # that the client wrote. Only the selected hop is unwrapped — the others are discarded.
        if len(hops) >= trusted_proxy_count:
            address = _unbracket(hops[-trusted_proxy_count])

    try:
        return ipaddress.ip_address(address) if address else None
    except ValueError:
        # not an address at all. Nothing here resolves names: that is what made this a request
        # forgery surface rather than a parsing nuisance
        return None
