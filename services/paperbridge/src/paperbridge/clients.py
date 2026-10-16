"""Thin clients for the services paperbridge talks to. Each method is one HTTP call."""

import logging
import secrets
import time
from collections.abc import Iterator

import httpx

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"paperbridge/{__version__} (+https://github.com/mufflon/HomeLab)"
RETRY_STATUS = {429, 502, 503, 504}
ATTEMPTS = 3


def web_client(timeout: float = 60.0) -> httpx.Client:
    """For plain downloads from the internet (arXiv, Unpaywall, publishers)."""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)


class API:
    def __init__(self, base_url: str, *, headers=None, auth=None, timeout: float = 30.0):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            auth=auth,
            timeout=timeout,
        )

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send, retrying connection errors, rate limits and gateway errors a few times."""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError as e:
                if attempt == ATTEMPTS:
                    raise
                log.warning("%s %s: %s; retrying", method, url, e)
                time.sleep(2**attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < ATTEMPTS:
                wait = _retry_after(response) or 2**attempt
                log.warning("%s %s: HTTP %d; retrying in %ds", method, url, response.status_code, wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable")


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return min(int(response.headers["Retry-After"]), 120)
    except (KeyError, ValueError):
        return None


class Karakeep(API):
    """Karakeep's REST API (v1), authenticated with an API key."""

    def __init__(self, url: str, api_key: str):
        super().__init__(f"{url}/api/v1", headers={"Authorization": f"Bearer {api_key}"})

    def tag_id(self, name: str) -> str | None:
        for tag in self._pages("/tags", "tags", nameContains=name):
            if tag["name"] == name:
                return tag["id"]
        return None

    def bookmarks_tagged(self, tag_id: str) -> Iterator[dict]:
        return self._pages(f"/tags/{tag_id}/bookmarks", "bookmarks")

    def bookmark(self, bookmark_id: str, include_content: bool = False) -> dict:
        params = {"includeContent": "true"} if include_content else {}
        return self.request("GET", f"/bookmarks/{bookmark_id}", params=params).json()

    def asset(self, asset_id: str) -> bytes:
        return self.request("GET", f"/assets/{asset_id}", timeout=120).content

    def set_note(self, bookmark_id: str, note: str) -> None:
        self.request("PATCH", f"/bookmarks/{bookmark_id}", json={"note": note})

    def add_tag(self, bookmark_id: str, name: str) -> None:
        self.request("POST", f"/bookmarks/{bookmark_id}/tags", json={"tags": [{"tagName": name}]})

    def lists(self) -> list[dict]:
        """All lists, manual and smart, owned by or shared with the user (not paginated)."""
        return self.request("GET", "/lists").json()["lists"]

    def list_bookmark_ids(self, list_id: str) -> list[str]:
        return [b["id"] for b in self._pages(f"/lists/{list_id}/bookmarks", "bookmarks")]

    def _pages(self, path: str, key: str, **params) -> Iterator[dict]:
        cursor = None
        while True:
            query = {"limit": 100, **params} | ({"cursor": cursor} if cursor else {})
            page = self.request("GET", path, params=query).json()
            yield from page[key]
            cursor = page.get("nextCursor")
            if not cursor:
                return


class Zotero(API):
    """Zotero's web API (v3), for one user library."""

    def __init__(self, api_url: str, user_id: str, api_key: str):
        super().__init__(
            f"{api_url}/users/{user_id}",
            headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"},
        )
        self.api_url = api_url
        self.user_id = user_id

    def key_info(self) -> dict:
        """The API key's owner and rights (GET /keys/current)."""
        return self.request("GET", f"{self.api_url}/keys/current").json()

    def top_items(self) -> list[dict]:
        """Every top-level item outside the trash.

        Quick search (q=) can't stand in: even qmode=everything matches only title, creators,
        year, notes and full text, not the DOI or Extra fields.
        """
        return self._all("/items/top")

    def collections(self) -> list[dict]:
        """Every collection, including those in the trash (flagged `deleted`)."""
        return self._all("/collections")

    def children(self, key: str) -> list[dict]:
        return [i["data"] for i in self.request("GET", f"/items/{key}/children").json()]

    def collection_is_empty(self, key: str) -> bool:
        """No items in it, not even trashed ones, and no subcollections."""
        items = self.request(
            "GET", f"/collections/{key}/items", params={"limit": 1, "includeTrashed": 1}
        ).json()
        subcollections = self.request("GET", f"/collections/{key}/collections", params={"limit": 1}).json()
        return not items and not subcollections

    def create(self, objects: list[dict], kind: str = "items") -> list[dict]:
        """POST new items (or collections) in one request; every one must succeed. Returns them
        as stored."""
        result = self.request(
            "POST",
            f"/{kind}",
            json=objects,
            headers={"Zotero-Write-Token": secrets.token_hex(16)},
        ).json()
        if result.get("failed"):
            raise RuntimeError(f"Zotero rejected {kind}: {result['failed']}")
        return [result["successful"][str(i)]["data"] for i in range(len(objects))]

    def update(self, path: str, version: int, fields: dict) -> int:
        """PATCH some fields of an item or collection (path: /items/KEY, /collections/KEY), only
        if nobody changed it since `version`. Returns its new version."""
        response = self.request(
            "PATCH",
            path,
            json=fields,
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        return int(response.headers["Last-Modified-Version"])

    def _all(self, path: str) -> list[dict]:
        objects: list[dict] = []
        while True:
            response = self.request("GET", path, params={"limit": 100, "start": len(objects)})
            page = [o["data"] for o in response.json()]
            objects += page
            if not page or len(objects) >= int(response.headers.get("Total-Results", 0)):
                return objects


class WebDAV(API):
    """The folder Zotero desktop syncs attachment files through (…/zotero/)."""

    def __init__(self, url: str, username: str, password: str):
        super().__init__(url, auth=(username, password), timeout=300)

    def put(self, name: str, data: bytes, content_type: str) -> None:
        self.request("PUT", name, content=data, headers={"Content-Type": content_type})


class TranslationServer(API):
    """Zotero's translation-server: URL or identifier -> Zotero items."""

    def __init__(self, url: str):
        super().__init__(url, timeout=120)

    def web(self, url: str) -> list[dict] | None:
        return self._translate("/web", url)

    def search(self, identifier: str) -> list[dict] | None:
        return self._translate("/search", identifier)

    def _translate(self, path: str, body: str) -> list[dict] | None:
        """Items, or None if nothing was found. Connection errors are raised: retry next run."""
        try:
            response = self.request(
                "POST", path, content=body.encode(), headers={"Content-Type": "text/plain"}
            )
        except httpx.HTTPStatusError as e:
            # 300 a page listing several items, 400 unsupported (e.g. a PDF), 501 no translator or
            # not found, 500 the site couldn't be fetched. All mean "no metadata from here".
            log.info("translation-server %s %s: HTTP %d", path, body, e.response.status_code)
            return None
        return response.json()
