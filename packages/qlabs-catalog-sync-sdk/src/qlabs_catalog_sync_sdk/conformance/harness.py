"""Reusable respx/vcrpy harness for the HTTP-behavior half of the conformance kit.

WP1 / T1.8. Retry/backoff and ``Retry-After`` handling already live entirely in
:class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint` (T1.4) and are exhaustively covered by
that module's own tests — a connector built on :class:`HttpEndpoint` gets that behavior
for free and does not need to re-prove it per connector. What *is* connector-specific,
and therefore what this module exists to help certify, is whether the connector's own
write path actually sends the concurrency header the manifest promises
(``concurrency=etag``/``revision`` implying ``If-Match``) and whether a capability
refusal happens *before* any request is attempted.

**Scope, stated honestly.** Every helper here works by activating :mod:`respx`, which
patches httpx's transport. It sees every request any httpx-based client sends —
including a connector's own use of :class:`HttpEndpoint`, and even a third-party client
that itself happens to be built on httpx. It sees **nothing** from a connector whose
vendor SDK talks over a different transport (``requests``, ``urllib3``, raw sockets, a
gRPC/native client). For such a connector, :func:`assert_no_http_calls` and
:func:`capture_requests` cannot verify anything — a "0 calls" result there is not
evidence of honesty, only of respx's blindness to that transport. A connector author in
that position must certify capability honesty and no-op idempotency some other way (a
vendor-SDK-level call counter, a local mock server, ...); this module cannot do it for
them.

These are plain helpers — not fixtures — precisely because *what* a connector's write
path is expected to call (which method, which path, which body) is unavoidably
connector-specific; only the certifying author knows their own wire shape. Point one of
these at your connector's real call inside your own test, per the module examples below.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import respx

if TYPE_CHECKING:
    import vcr

__all__ = [
    "assert_if_match_sent",
    "assert_no_http_calls",
    "capture_requests",
    "retry_then_succeed",
    "vcr_config",
]


@contextlib.contextmanager
def capture_requests(*, response: httpx.Response | None = None) -> Iterator[respx.Route]:
    """Intercept every outbound httpx request for the duration of the block.

    Registers one catch-all route (any method, any host) so nothing escapes to a real
    network, and returns the :class:`respx.Route` (before the block body runs) so the
    caller can inspect ``route.call_count`` and ``route.calls`` afterwards — each
    ``call.request`` is a real :class:`httpx.Request` with its method, URL and headers as
    actually sent.

    ``response`` is what every intercepted request receives; defaults to an otherwise
    unremarkable ``204 No Content`` so a connector whose write path checks the status
    code does not blow up on the mock. Pass an explicit ``response`` (or override
    ``route.side_effect``/``route.return_value`` on the yielded route) when the code
    under test needs a particular body.

    Example::

        with capture_requests() as route:
            await connector.update(ref, diff)
        assert route.call_count == 1
        assert_if_match_sent(route.calls, required=True)
    """
    reply = response if response is not None else httpx.Response(204)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.route(url__regex=r".*").mock(return_value=reply)
        yield route


@contextlib.contextmanager
def assert_no_http_calls() -> Iterator[None]:
    """Assert that no httpx request is sent while the block executes.

    This is the "second half" of a capability-honesty check: catching the
    :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` proves the connector
    *said* no, this proves it never dialed out to say it. See the module docstring for
    what this cannot see (any non-httpx transport).

    Example::

        with pytest.raises(CapabilityError), assert_no_http_calls():
            await connector.update(ref, diff_touching_a_ro_field)
    """
    with capture_requests() as route:
        yield
    assert route.call_count == 0, (
        f"expected zero HTTP requests, but {route.call_count} were sent: "
        f"{[f'{call.request.method} {call.request.url}' for call in route.calls]}"
    )


def assert_if_match_sent(
    requests: Iterable[httpx.Request] | Iterable[respx.models.Call], *, required: bool
) -> None:
    """Assert every captured request does (``required=True``) or does not
    (``required=False``) carry an ``If-Match`` header.

    Accepts either the raw :class:`httpx.Request` objects or the
    :class:`respx.models.Call` records :attr:`respx.Route.calls` yields (each of which
    wraps ``.request``) — pass whichever :func:`capture_requests` handed back.

    This is what certifies decision-honesty for ``concurrency=etag``/``revision``: a
    connector whose manifest declares either but whose write path never actually
    forwards the revision it read as ``If-Match`` fails this, exactly as it should — the
    manifest and the wire behavior have to agree, or the engine's optimistic-concurrency
    story is fiction.
    """
    seen: list[httpx.Request] = []
    for item in requests:
        request = item.request if isinstance(item, respx.models.Call) else item
        seen.append(request)
        has_if_match = bool(request.headers.get("if-match"))
        if required and not has_if_match:
            raise AssertionError(
                f"expected If-Match on {request.method} {request.url}, but it was absent "
                f"(manifest declares a concurrency mode that requires it)"
            )
        if not required and has_if_match:
            raise AssertionError(
                f"unexpected If-Match on {request.method} {request.url} "
                f"(manifest declares concurrency=none)"
            )
    if not seen:
        raise AssertionError("no requests were captured to check for If-Match")


def retry_then_succeed(
    *,
    first_status: int,
    retry_after: str | None = None,
    success_json: object = None,
) -> list[httpx.Response]:
    """A ``[failure, success]`` response sequence for a respx ``side_effect``.

    Matches the shape ``HttpEndpoint``'s own retry tests use
    (``tests/http/test_retry.py``): the first response is a retryable failure (429 or
    5xx, optionally with ``Retry-After``), the second a plain 200. Reused here so a
    connector author certifying that *their* write call goes through ``HttpEndpoint``'s
    retry (rather than, say, a raw ``httpx.AsyncClient`` that bypasses it) does not have
    to hand-build the sequence.

    Example::

        respx_mock.patch(f"{base_url}/items/1").mock(
            side_effect=retry_then_succeed(first_status=429, retry_after="0")
        )
        result = await connector.update(ref, diff)
        assert result.outcome is WriteOutcome.UPDATED
    """
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return [
        httpx.Response(first_status, headers=headers),
        httpx.Response(200, json=success_json if success_json is not None else {"ok": True}),
    ]


def vcr_config(
    cassette_library_dir: str | Path,
    *,
    record_mode: str = "once",
    match_on: Sequence[str] = ("method", "scheme", "host", "port", "path", "query"),
) -> vcr.VCR:
    """A pre-configured :class:`vcr.VCR` instance for recording a connector's real API
    traffic into replayable cassettes.

    Filters the ``Authorization`` and ``X-Api-Key`` headers from anything written to
    disk — a recorded cassette must never carry a live credential — and defaults to
    ``record_mode="once"`` (record if the cassette does not exist yet, otherwise replay
    only, never silently re-hit the network). ``match_on`` deliberately excludes request
    *body*: two functionally-identical requests can differ in incidental body ordering
    (JSON key order, whitespace) without meaning a different interaction.

    Complements, rather than replaces, :func:`capture_requests`: respx is for unit tests
    against synthetic responses (fast, no credentials, exercises retry/If-Match logic
    precisely); vcrpy cassettes are for a recorded fixture of what a real endpoint
    actually returned, replayed without live credentials in CI. A connector author picks
    whichever a given conformance test needs — often both, in different tests.
    """
    import vcr as vcrpy

    return vcrpy.VCR(
        cassette_library_dir=str(cassette_library_dir),
        record_mode=record_mode,
        match_on=list(match_on),
        filter_headers=["authorization", "x-api-key"],
        decode_compressed_response=True,
    )
