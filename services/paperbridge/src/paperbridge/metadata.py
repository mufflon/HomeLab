"""Finding a paper's Zotero metadata: identifiers, translation-server, and fixes on top."""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

from .clients import TranslationServer

log = logging.getLogger(__name__)

_ARXIV_ID = r"\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7}"  # 1706.03762, hep-th/9901001
ARXIV_IN_URL = re.compile(rf"arxiv\.org/(?:abs|pdf|html|format)/({_ARXIV_ID})(?:v\d+)?", re.I)
ARXIV_IN_TEXT = re.compile(rf"\barXiv:\s?({_ARXIV_ID})(?:v\d+)?", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
DOI_RESOLVERS = {"doi.org", "dx.doi.org", "www.doi.org"}
DOI_URL_SUFFIXES = re.compile(r"(?:[./](?:full|pdf|abstract|epdf))+$", re.I)
ARXIV_DOI_PREFIX = "10.48550/"  # arXiv's own DOIs; Unpaywall has nothing beyond arXiv for them
TEXT_SCAN = 20_000  # how far into a PDF's text to look for its identifiers and title


@dataclass(frozen=True)
class Identifiers:
    arxiv: str | None = None
    doi: str | None = None


def identifiers_in_url(url: str) -> Identifiers:
    url = unquote(url)
    arxiv = ARXIV_IN_URL.search(url)
    doi = DOI.search(url)
    return Identifiers(
        arxiv=arxiv.group(1) if arxiv else None,
        doi=_clean_doi(DOI_URL_SUFFIXES.sub("", doi.group(1).split("?")[0])) if doi else None,
    )


def identifiers_in_text(text: str) -> Identifiers:
    """The first arXiv ID and DOI in a paper's text (the paper's own usually come first)."""
    head = text[:TEXT_SCAN]
    arxiv = ARXIV_IN_TEXT.search(head)
    doi = DOI.search(head)
    return Identifiers(
        arxiv=arxiv.group(1) if arxiv else None,
        doi=_clean_doi(doi.group(1)) if doi else None,
    )


def identifiers_of_item(item: dict) -> Identifiers:
    """arXiv ID and DOI of a Zotero item (translated or already in the library)."""
    archive_id = item.get("archiveID") or ""
    arxiv = archive_id.removeprefix("arXiv:") if archive_id.startswith("arXiv:") else None
    if not arxiv and item.get("url"):
        arxiv = identifiers_in_url(item["url"]).arxiv
    return Identifiers(arxiv=arxiv, doi=item_doi(item))


def item_doi(item: dict) -> str | None:
    """The DOI field, or a "DOI: …" line in Extra (for item types without a DOI field)."""
    if item.get("DOI"):
        return _clean_doi(item["DOI"])
    for line in (item.get("extra") or "").splitlines():
        if line.lower().startswith("doi:"):
            return _clean_doi(line[4:].strip())
    return None


def _clean_doi(doi: str) -> str:
    doi = doi.strip().rstrip(".,;:)]}")
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    return doi.lower()


def lookup(ts: TranslationServer, url: str | None, text: str | None) -> tuple[dict, list[dict]] | None:
    """The bookmark's Zotero item and its child notes, from translation-server, or None.

    Identifiers beat scraping: an arXiv ID or a doi.org link is looked up directly, other
    pages through the site's translator. Identifiers found only in a PDF's text are accepted
    only if the paper's title appears in that text too, so a cited DOI can't win.
    """
    attempts: list[tuple[str, str, bool]] = []  # (endpoint, query, check title against text)
    if url:
        ids = identifiers_in_url(url)
        if ids.arxiv:
            attempts.append(("search", f"arXiv:{ids.arxiv}", False))
        if ids.doi and urlsplit(url).hostname in DOI_RESOLVERS:
            attempts.append(("search", ids.doi, False))
        attempts.append(("web", url, False))
        if ids.doi:
            attempts.append(("search", ids.doi, False))
    if text:
        ids = identifiers_in_text(text)
        if ids.arxiv:
            attempts.append(("search", f"arXiv:{ids.arxiv}", True))
        if ids.doi:
            attempts.append(("search", ids.doi, True))

    seen = set()
    for endpoint, query, check_title in attempts:
        if (endpoint, query) in seen:
            continue
        seen.add((endpoint, query))
        items = ts.web(query) if endpoint == "web" else ts.search(query)
        found = _split(items or [])
        if not found:
            continue
        if check_title and not _title_in(found[0].get("title", ""), text or ""):
            log.info("%s %s: title %r not in the PDF's text; ignoring", endpoint, query, found[0].get("title"))
            continue
        log.info("metadata from translation-server %s %s", endpoint, query)
        return found
    return None


def _split(items: list[dict]) -> tuple[dict, list[dict]] | None:
    """The first regular item, cleaned for the Zotero API, and its child notes."""
    drop = {"key", "version", "parentItem", "attachments", "notes", "collections"}
    main = next((i for i in items if i.get("itemType") not in ("note", "attachment")), None)
    if main is None:
        return None
    notes = [
        {k: v for k, v in i.items() if k not in drop}
        for i in items
        if i.get("itemType") == "note" and i.get("parentItem") in (None, main.get("key"))
    ]
    return {k: v for k, v in main.items() if k not in drop}, notes


def _normalize(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", s.lower()).split())


def _title_in(title: str, text: str) -> bool:
    title = _normalize(title)
    return bool(title) and title in _normalize(text[:TEXT_SCAN])


def arxiv_submission_date(web: httpx.Client, arxiv_id: str) -> str | None:
    """The date of an arXiv paper's first version (YYYY-MM-DD), from arXiv's API.

    translation-server dates arXiv items by their latest version (v7 of 1706.03762 is from 2023);
    papers are cited by when they first appeared.
    """
    try:
        response = web.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id})
        response.raise_for_status()
        published = ET.fromstring(response.content).find(
            "{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}published"
        )
    except (httpx.HTTPError, ET.ParseError) as e:
        log.warning("arXiv API for %s: %s; keeping the translator's date", arxiv_id, e)
        return None
    return published.text[:10] if published is not None and published.text else None


def fallback_item(bookmark: dict, url: str | None) -> dict:
    """A minimal item from what Karakeep knows, for papers no translator recognises."""
    content = bookmark.get("content") or {}
    is_pdf = content.get("type") == "asset"
    title = (
        bookmark.get("title")
        or content.get("title")
        or content.get("fileName")
        or url
        or "Untitled"
    )
    item = {
        "itemType": "document" if is_pdf or not url else "webpage",
        "title": title,
        "url": url or "",
        "abstractNote": content.get("description") or "",
        "date": (content.get("datePublished") or "")[:10],
        "tags": [{"tag": "needs-metadata"}],
    }
    if content.get("author"):
        item["creators"] = [{"name": content["author"], "creatorType": "author"}]
    return item
