"""Targeted tests for the client.py review comments (not spec-numbered tests).

1. (covered in test_client.py) 429 exhaustion raises rate_limited, never
   upstream_unavailable.
2. Backoff grows with attempt number on the network-error branch.
3. httpx.TransportError broadly caught; non-JSON 200 -> upstream_unavailable;
   unlisted 4xx -> non-retryable bad_request; TLS verification failure ->
   non-retryable config_error.
5. Connection errors/timeouts are capped at 2 retries (NETWORK_AND_5XX_MAX_RETRIES),
   independent of a larger WC_MAX_RETRIES.
   Semaphore: backoff sleeps release the concurrency slot (see client.py's
   _send_once docstring for the reasoning) -- verified here by proving a
   second call isn't blocked while the first is sleeping.
"""

import asyncio
import ssl
import time

import httpx
import pytest
import respx

from wc_connector import errors
from wc_connector.client import NETWORK_AND_5XX_MAX_RETRIES, WooClient

from conftest import make_config


@pytest.fixture
async def client_factory():
    created = []

    def _make(**config_overrides):
        config = make_config(**config_overrides)
        client = WooClient(config, jitter_fn=lambda a, b: 0)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


async def test_backoff_grows_with_attempt_on_network_errors(client_factory, monkeypatch):
    client = client_factory(max_retries=5, max_total_wait_seconds=5)
    recorded_attempts = []

    def fake_backoff(attempt):
        recorded_attempts.append(attempt)
        return 0.0  # keep the test fast; we only care about the attempt argument

    monkeypatch.setattr(client, "_backoff_delay", fake_backoff)

    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/1").mock(
            side_effect=[
                httpx.ConnectError("boom"),
                httpx.ConnectError("boom"),
                httpx.Response(200, json={"id": 1}),
            ]
        )
        result = await client.get("/wp-json/wc/v3/orders/1")
        assert result.data == {"id": 1}

    # Each retry must pass a strictly increasing attempt number, not a
    # constant 1, or backoff never actually grows.
    assert recorded_attempts == [1, 2]


async def test_transport_error_broadly_caught_not_just_three_subclasses(client_factory):
    """httpx.ProtocolError is a TransportError but not a Timeout/Connect/NetworkError;
    it must still be caught and mapped, never leak out as a raw httpx exception."""
    client = client_factory(max_retries=5, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/1").mock(side_effect=httpx.ProtocolError("bad framing"))
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "UPSTREAM_UNAVAILABLE"
        assert exc_info.value.retryable is True


async def test_non_json_200_body_is_upstream_unavailable(client_factory):
    client = client_factory(max_retries=1, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/1").mock(
            return_value=httpx.Response(200, content=b"<html>not json</html>")
        )
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "UPSTREAM_UNAVAILABLE"


async def test_unlisted_4xx_is_non_retryable_bad_request(client_factory):
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders/1").mock(return_value=httpx.Response(409))
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "BAD_REQUEST"
        assert exc_info.value.retryable is False
        assert route.call_count == 1


async def test_tls_verification_failure_is_non_retryable_config_error(client_factory):
    """respx's side_effect machinery re-raises exceptions via `raise error.origin
    from error` (respx/router.py), which clobbers whatever __cause__ we set on
    our crafted exception before it reaches client.py. So this bypasses respx
    and patches the underlying httpx.AsyncClient.get directly, which is the
    only way to deliver a TransportError with __cause__ intact."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    tls_error = httpx.ConnectError("certificate verify failed")
    tls_error.__cause__ = ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")

    call_count = 0

    async def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise tls_error

    client._client.get = fake_get

    with pytest.raises(errors.ConnectorError) as exc_info:
        await client.get("/wp-json/wc/v3/orders/1")
    assert exc_info.value.code == "CONFIG_ERROR"
    assert exc_info.value.retryable is False
    assert call_count == 1  # no point retrying a cert problem


async def test_tls_verification_failure_detected_through_double_wrapped_cause(client_factory):
    """Confirmed against the real store: httpx/httpcore can wrap the SSL
    error in two layers of ConnectError (exc.__cause__ is itself a
    ConnectError, and exc.__cause__.__cause__ is the real
    SSLCertVerificationError), not always just one. A single-level
    exc.__cause__ check misses this shape."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    ssl_error = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
    )
    inner = httpx.ConnectError("boom")
    inner.__cause__ = ssl_error
    outer = httpx.ConnectError("boom")
    outer.__cause__ = inner

    async def fake_get(*args, **kwargs):
        raise outer

    client._client.get = fake_get

    with pytest.raises(errors.ConnectorError) as exc_info:
        await client.get("/wp-json/wc/v3/orders/1")
    assert exc_info.value.code == "CONFIG_ERROR"
    assert exc_info.value.retryable is False


async def test_tls_verification_failure_detected_when_wrapped_as_constructor_arg(client_factory):
    """Also confirmed against the real store: httpcore.ConnectError wraps the
    ssl error as a plain constructor argument (`ConnectError(ssl_error)`),
    not via __cause__ at all. Checking only __cause__ (even recursively)
    misses this shape -- .args must be inspected too."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    ssl_error = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
    )
    httpcore_style_error = Exception(ssl_error)  # mirrors httpcore.ConnectError(ssl_error)
    outer = httpx.ConnectError("boom")
    outer.__cause__ = httpcore_style_error

    async def fake_get(*args, **kwargs):
        raise outer

    client._client.get = fake_get

    with pytest.raises(errors.ConnectorError) as exc_info:
        await client.get("/wp-json/wc/v3/orders/1")
    assert exc_info.value.code == "CONFIG_ERROR"
    assert exc_info.value.retryable is False


async def test_connection_errors_capped_at_two_retries_even_with_higher_max_retries(client_factory):
    """WC_MAX_RETRIES=10 must not apply here; the spec fixes this category at 2."""
    client = client_factory(max_retries=10, max_total_wait_seconds=10)
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders/1").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "UPSTREAM_UNAVAILABLE"
        # initial attempt + NETWORK_AND_5XX_MAX_RETRIES retries, no more
        assert route.call_count == 1 + NETWORK_AND_5XX_MAX_RETRIES


async def test_backoff_sleep_releases_the_semaphore_slot(client_factory, monkeypatch):
    """With max_concurrent_requests=1, a second independent call must be able
    to run while the first is sleeping between retries -- proving the slot
    isn't held during backoff (see _send_once's docstring for why)."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5, max_concurrent_requests=1)
    monkeypatch.setattr(client, "_backoff_delay", lambda attempt: 0.3)

    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/1").mock(
            side_effect=[httpx.Response(502), httpx.Response(200, json={"id": 1})]
        )
        router.get("/wp-json/wc/v3/orders/2").mock(return_value=httpx.Response(200, json={"id": 2}))

        first_call = asyncio.create_task(client.get("/wp-json/wc/v3/orders/1"))
        await asyncio.sleep(0.05)  # let the first call hit its 502 and enter the 0.3s backoff sleep

        # If the semaphore slot were held during the sleep, this would hang
        # past the backoff window; wait_for with a tight timeout proves it
        # doesn't.
        second_result = await asyncio.wait_for(client.get("/wp-json/wc/v3/orders/2"), timeout=0.2)
        assert second_result.data == {"id": 2}

        first_result = await first_call
        assert first_result.data == {"id": 1}
