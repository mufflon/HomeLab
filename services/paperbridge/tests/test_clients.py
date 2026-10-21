"""Retries in the HTTP clients."""

import httpx
import pytest

from paperbridge import clients


def answering(*statuses):
    """A transport answering the given statuses in turn, and recording the requests."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(statuses[min(len(seen), len(statuses)) - 1], json={})

    return httpx.MockTransport(handler), seen


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(clients.time, "sleep", lambda s: None)


def karakeep(transport):
    kk = clients.Karakeep("http://karakeep", "key")
    kk.http = httpx.Client(base_url="http://karakeep/api/v1", transport=transport)
    return kk


def test_karakeep_retries_a_locked_database_and_a_spurious_401():
    transport, seen = answering(500, 401, 200)
    karakeep(transport).set_note("bm1", "note")
    assert len(seen) == 3


def test_karakeep_gives_up_after_its_attempts():
    transport, seen = answering(500)
    with pytest.raises(httpx.HTTPStatusError):
        karakeep(transport).set_note("bm1", "note")
    assert len(seen) == clients.Karakeep.attempts


def test_other_services_keep_the_narrow_retry_rule():
    transport, seen = answering(500, 200)
    zotero = clients.Zotero("http://zotero", "1", "key")
    zotero.http = httpx.Client(base_url="http://zotero/users/1", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        zotero.request("GET", "/items")
    assert len(seen) == 1
