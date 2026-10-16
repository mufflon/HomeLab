"""The handoff: Karakeep bookmarks tagged `paper` become Zotero items with their PDF.

Idempotent, with no state of its own:
- the `in-zotero` tag on a bookmark means done;
- a "Karakeep: <bookmark link>" line in the Zotero item's Extra finds the item again if a run
  stopped between creating it and tagging the bookmark;
- a DOI already in the library is linked instead of duplicated.
Both lookups read the library's top-level items, once per run. mirror.py then files the
items into collections that mirror the chosen Karakeep lists.
"""

import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime

import httpx

from .clients import Karakeep, TranslationServer, WebDAV, Zotero
from .config import Config
from .files import PDF, attachment_filename, find_pdf, md5, prop_for_webdav, zip_for_webdav
from .mirror import Mirror
from .metadata import (
    Identifiers,
    arxiv_submission_date,
    fallback_item,
    identifiers_in_url,
    identifiers_of_item,
    item_doi,
    lookup,
)

log = logging.getLogger(__name__)

KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"  # Zotero object keys: 8 of these


def new_key() -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))


def tag_names(bookmark: dict) -> set[str]:
    return {t["name"] for t in bookmark.get("tags") or []}


def source_url(bookmark: dict) -> str | None:
    content = bookmark.get("content") or {}
    return content.get("url") if content.get("type") == "link" else content.get("sourceUrl")


def source_text(bookmark: dict) -> str | None:
    """A PDF bookmark's extracted text, or a text bookmark's text."""
    content = bookmark.get("content") or {}
    return content.get("content") if content.get("type") == "asset" else content.get("text")


def append_line(text: str | None, line: str) -> str:
    return f"{text.rstrip()}\n{line}" if text and text.strip() else line


class Handoff:
    def __init__(
        self,
        cfg: Config,
        karakeep: Karakeep,
        zotero: Zotero,
        webdav: WebDAV,
        ts: TranslationServer,
        web: httpx.Client,
        *,
        dry_run: bool = False,
        now: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.karakeep = karakeep
        self.zotero = zotero
        self.webdav = webdav
        self.ts = ts
        self.web = web
        self.dry_run = dry_run
        self.now = now
        self.web_library = ""  # https://www.zotero.org/<user>/items, once the API key is checked
        self.library: list[dict] | None = None  # Zotero's top-level items, read once per run
        self.bookmark_prefix = f"{cfg.karakeep_public_url}/dashboard/preview/"

    def run(self, only: str | None = None) -> int:
        """Hand off every pending bookmark, then mirror the chosen Karakeep lists into Zotero's
        collections (not with `only`). Returns how many things failed; the next run retries them."""
        tag_id = self.karakeep.tag_id(self.cfg.paper_tag)
        tagged = list(self.karakeep.bookmarks_tagged(tag_id)) if tag_id else []
        pending = [
            b
            for b in tagged
            if self.cfg.done_tag not in tag_names(b) and only in (None, b["id"])
        ]
        log.info(
            "%d bookmark(s) tagged %r, %d without %r",
            len(tagged), self.cfg.paper_tag, len(pending), self.cfg.done_tag,
        )
        failed = 0
        if pending:
            self._check_zotero_key()
        for bookmark in pending:
            try:
                self.hand_off(bookmark)
            except Exception:
                failed += 1
                log.exception("bookmark %s failed; the next run retries it", bookmark["id"])
        if only is None and self.cfg.mirror_lists:
            mirror = Mirror(
                self.karakeep,
                self.zotero,
                roots=self.cfg.mirror_lists,
                bookmark_prefix=self.bookmark_prefix,
                write=self._write,
                new_key=new_key,
            )
            try:
                failed += mirror.run(self._library())
            except Exception:
                failed += 1
                log.exception("mirroring Karakeep's lists failed; the next run retries it")
        return failed

    def _check_zotero_key(self) -> None:
        info = self.zotero.key_info()
        rights = info.get("access", {}).get("user", {})
        if str(info.get("userID")) != str(self.cfg.zotero_user_id) or not rights.get("write"):
            raise SystemExit("ZOTERO_API_KEY must belong to ZOTERO_USER_ID and allow library writes")
        self.web_library = f"https://www.zotero.org/{info['username']}/items"

    def hand_off(self, bookmark: dict) -> None:
        bid = bookmark["id"]
        content = bookmark.get("content") or {}
        if content.get("type") == "link" and content.get("crawlStatus") == "pending":
            # It may turn out to be a PDF (Karakeep then makes it an asset bookmark): wait for that.
            log.info("bookmark %s: Karakeep is still crawling it; next run", bid)
            return
        if content.get("type") in ("asset", "text"):
            bookmark = self.karakeep.bookmark(bid, include_content=True)
        url, text = source_url(bookmark), source_text(bookmark)
        marker = f"Karakeep: {self.bookmark_prefix}{bid}"
        log.info("bookmark %s: %s", bid, url or bookmark.get("title") or content.get("fileName"))

        new: list[dict] = []  # Zotero objects to create, in one request
        parent = self._find(lambda item: marker in (item.get("extra") or "").splitlines())
        if parent:
            log.info("already in Zotero as %s (from an earlier run)", parent["key"])
        else:
            found = self._metadata(bookmark, url, text)
            if found is None:
                return
            item, notes = found
            doi = item_doi(item)
            parent = self._find(lambda i: item_doi(i) == doi) if doi else None
            if parent:
                log.info("DOI %s is already in Zotero as %s; linking to it", doi, parent["key"])
                extra = append_line(parent.get("extra"), marker)
                version = self._write(
                    f"add {marker!r} to the Extra of {parent['key']}",
                    self.zotero.update,
                    f"/items/{parent['key']}",
                    parent["version"],
                    {"extra": extra},
                )
                parent.update(extra=extra, version=version or parent["version"])
            else:
                # version 0: "new" to Zotero's API, which requires it with a client-chosen key.
                parent = {
                    **item,
                    "key": new_key(),
                    "version": 0,
                    "extra": append_line(item.get("extra"), marker),
                }
                new = [parent] + [{**note, "parentItem": parent["key"]} for note in notes]
                log.info("new %s %r as %s", parent["itemType"], parent.get("title"), parent["key"])

        if new or not self._has_pdf(parent["key"]):
            ids = identifiers_of_item(parent)
            if url and not ids.arxiv:
                ids = Identifiers(arxiv=identifiers_in_url(url).arxiv, doi=ids.doi)
            pdf = find_pdf(bookmark, ids, self.karakeep, self.web, self.cfg.unpaywall_email)
            if pdf:
                new.append(self._store(pdf, parent))
            else:
                log.info("no PDF found; Zotero desktop's Find Available PDF can try")
        if new:
            try:
                created = self._write(f"create {len(new)} Zotero object(s)", self.zotero.create, new)
            except Exception:
                self.library = None  # some objects may exist now: read the library again
                raise
            if new[0] is parent:  # a new parent: later bookmarks in this run must see it
                self.library.append(created[0] if created else parent)
        self._mark_done(bookmark, parent["key"])

    def _library(self) -> list[dict]:
        if self.library is None:
            self.library = self.zotero.top_items()
            log.info("Zotero library: %d top-level items", len(self.library))
        return self.library

    def _find(self, match: Callable[[dict], bool]) -> dict | None:
        return next((i for i in self._library() if match(i)), None)

    def _metadata(self, bookmark: dict, url: str | None, text: str | None):
        found = lookup(self.ts, url, text)
        if found:
            item, notes = found
            arxiv = identifiers_of_item(item).arxiv
            if item.get("itemType") == "preprint" and arxiv:
                first = arxiv_submission_date(self.web, arxiv)
                if first and first != item.get("date"):
                    log.info("date %s -> %s (arXiv v1)", item.get("date"), first)
                    item["date"] = first
            return item, notes
        age_hours = (self.now() - datetime.fromisoformat(bookmark["createdAt"]).timestamp()) / 3600
        if age_hours < self.cfg.metadata_grace_hours:
            log.warning(
                "no metadata found; retrying each run until the bookmark is %g h old",
                self.cfg.metadata_grace_hours,
            )
            return None
        log.warning("no metadata found; creating a minimal item tagged needs-metadata")
        return fallback_item(bookmark, url), []

    def _has_pdf(self, key: str) -> bool:
        return any(
            child.get("itemType") == "attachment"
            and child.get("contentType") == "application/pdf"
            and child.get("linkMode") in ("imported_file", "imported_url", "linked_file")
            for child in self.zotero.children(key)
        )

    def _store(self, pdf: PDF, parent: dict) -> dict:
        """Put the PDF where Zotero desktop's WebDAV sync looks; return its attachment item."""
        key = new_key()
        filename = attachment_filename(parent)
        checksum = md5(pdf.data)
        mtime = int(self.now() * 1000)
        log.info("PDF from %s (%d kB) as %s %r", pdf.source, len(pdf.data) >> 10, key, filename)
        # Files before the item: once the item syncs, Zotero desktop goes looking for them.
        zipped = zip_for_webdav(filename, pdf.data)
        self._write(f"PUT {key}.zip", self.webdav.put, f"{key}.zip", zipped, "application/zip")
        prop = prop_for_webdav(mtime, checksum)
        self._write(f"PUT {key}.prop", self.webdav.put, f"{key}.prop", prop, "text/xml")
        return {
            "key": key,
            "version": 0,
            "itemType": "attachment",
            "parentItem": parent["key"],
            "linkMode": "imported_file",
            "title": "Preprint PDF" if parent.get("itemType") == "preprint" else "Full Text PDF",
            "contentType": "application/pdf",
            "filename": filename,
            # Zotero's API lets clients set these for WebDAV-synced files; desktop compares them
            # with the .prop to decide whether to download.
            "md5": checksum,
            "mtime": mtime,
        }

    def _mark_done(self, bookmark: dict, key: str) -> None:
        bid = bookmark["id"]
        note = bookmark.get("note") or ""
        if key not in note:
            links = f"Zotero: zotero://select/library/items/{key}\n{self.web_library}/{key}"
            new_note = f"{note.rstrip()}\n\n{links}" if note.strip() else links
            self._write("add the Zotero links to the bookmark's note", self.karakeep.set_note, bid, new_note)
        self._write(f"tag the bookmark {self.cfg.done_tag!r}", self.karakeep.add_tag, bid, self.cfg.done_tag)

    def _write(self, what: str, fn: Callable, *args):
        if self.dry_run:
            log.info("dry run: would %s", what)
            return None
        return fn(*args)
