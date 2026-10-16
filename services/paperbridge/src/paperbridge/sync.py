"""Sync between each handed-off Zotero item and its Karakeep bookmark(s), every run.

It follows the one-entry-point rule (CLAUDE.md, paperbridge):
- metadata (title, authors, date, abstract) and comments sync both ways;
- read status goes Zotero -> Karakeep (the Zotero Reading List plugin's Extra lines);
- files and structure are not touched here; they only go Karakeep -> Zotero (handoff, mirror).

Syncing both ways needs to know which side changed, so the last-synced values of every
(item, bookmark) pair are kept in a JSON file on paperbridge's own ZFS dataset. Per field, a
three-way merge against that snapshot:
- changed on one side: copied to the other;
- changed on both: Zotero wins for metadata, and comments keep both texts with a conflict marker;
- no snapshot yet (a pair's first sync): Zotero's value wins if it has one; comments that
  differ are both kept.

Comments are the bookmark's note (minus paperbridge's Zotero links) and one Zotero child note
per bookmark, headed "From Karakeep", that links back to the bookmark. Karakeep keeps author,
date and description only for link bookmarks, so PDF and text bookmarks sync title and comments.
"""

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser

from .clients import Karakeep, Zotero

log = logging.getLogger(__name__)

NOTE_HEADING = "From Karakeep"
ZOTERO_LINK_LINE = re.compile(
    r"^(?:Zotero: zotero://select/library/items/[A-Z0-9]{8}|https://www\.zotero\.org/\S+/items/[A-Z0-9]{8})$"
)


# --- text helpers ------------------------------------------------------------------------------

def norm_text(text: str | None) -> str:
    """Lines stripped, runs of blank lines made one, ends trimmed: how comments are compared."""
    lines = [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


class _HTMLText(HTMLParser):
    BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.parts.append("\n" if tag == "br" else "\n\n" if tag in self.BLOCKS else "")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        self.parts.append(re.sub(r"\s+", " ", data))


def html_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return norm_text("".join(parser.parts))


def note_html(text: str, link: str) -> str:
    """A "From Karakeep" note: the comments as paragraphs, then a link back to the bookmark."""
    paragraphs = [p for p in norm_text(text).split("\n\n") if p]
    body = "".join("<p>" + "<br>".join(escape(line) for line in p.split("\n")) + "</p>" for p in paragraphs)
    return f'<h1>{NOTE_HEADING}</h1>{body}<p><a href="{escape(link)}">Karakeep bookmark</a></p>'


def note_comments(html: str, link: str) -> str:
    """The comments in a "From Karakeep" note: without its heading and its link back."""
    footer = rf'<p>(?:(?!</p>).)*href="{re.escape(escape(link))}"(?:(?!</p>).)*</p>'
    text = html_text(re.sub(footer, "", html, flags=re.S))
    first, _, rest = text.partition("\n")
    return norm_text(rest) if first.strip() == NOTE_HEADING else text


def bookmark_comments(note: str | None) -> str:
    """The comments in a bookmark's note: without the Zotero links paperbridge put there."""
    return norm_text("\n".join(l for l in (note or "").splitlines() if not ZOTERO_LINK_LINE.match(l.strip())))


def zotero_links(key: str, web_library: str) -> str:
    return f"Zotero: zotero://select/library/items/{key}\n{web_library}/{key}"


def bookmark_note(comments: str, key: str, web_library: str) -> str:
    comments = norm_text(comments)
    links = zotero_links(key, web_library)
    return f"{comments}\n\n{links}" if comments else links


def extra_value(item: dict, name: str) -> str:
    """The last "Name: value" line in Extra (the Reading List plugin's format), or ""."""
    pattern = re.compile(rf"^{re.escape(name)}:(.+)$", re.I)
    values = [m.group(1).strip() for line in (item.get("extra") or "").split("\n") if (m := pattern.match(line))]
    return values[-1] if values else ""


# --- metadata fields ---------------------------------------------------------------------------

def _authors(item: dict) -> str:
    names = []
    for c in item.get("creators") or []:
        if c.get("creatorType", "author") == "author":
            names.append(c.get("name") or " ".join(p for p in (c.get("firstName"), c.get("lastName")) if p))
    return "; ".join(n for n in names if n)


def _creators(authors: str, item: dict) -> list[dict]:
    """Zotero creators from "First Last; First Last", keeping editors and other non-authors."""
    parts = authors.split(";") if ";" in authors else [authors]
    creators = []
    for part in (p.strip() for p in parts):
        if not part:
            continue
        first, _, last = part.rpartition(" ")
        creators.append(
            {"creatorType": "author", "firstName": first, "lastName": last}
            if first
            else {"creatorType": "author", "name": part}
        )
    others = [c for c in item.get("creators") or [] if c.get("creatorType", "author") != "author"]
    return creators + others


def _iso_date(date: str) -> str | None:
    """Zotero's date ("2017", "2017-06", "2017-06-12", "2017-06-12 10:00") as Karakeep's ISO time."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", date.strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2) or '01'}-{m.group(3) or '01'}T00:00:00.000Z"


def _same_date(z: str, k: str) -> bool:
    iso = _iso_date(z) if z else None
    return (iso[:10] if iso else z) == k


def _link_content(b: dict) -> dict:
    content = b.get("content") or {}
    return content if content.get("type") == "link" else {}


@dataclass(frozen=True)
class Field:
    name: str
    zotero: Callable[[dict], str]  # the item's value
    karakeep: Callable[[dict], str]  # the bookmark's value
    to_karakeep: Callable[[str], dict | None]  # bookmark PATCH fields; None: can't be expressed
    to_zotero: Callable[[str, dict], dict]  # item PATCH fields
    same: Callable[[str, str], bool] = same_text
    links_only: bool = False  # Karakeep keeps it for link bookmarks only


FIELDS = (
    Field(
        "title",
        lambda i: i.get("title") or "",
        lambda b: b.get("title") or _link_content(b).get("title") or "",
        lambda v: {"title": v or None},
        lambda v, i: {"title": v},
    ),
    Field(
        "authors",
        _authors,
        lambda b: _link_content(b).get("author") or "",
        lambda v: {"author": v or None},
        lambda v, i: {"creators": _creators(v, i)},
        links_only=True,
    ),
    Field(
        "date",
        lambda i: i.get("date") or "",
        lambda b: (_link_content(b).get("datePublished") or "")[:10],
        lambda v: {"datePublished": _iso_date(v)} if not v or _iso_date(v) else None,
        lambda v, i: {"date": v},
        same=_same_date,
        links_only=True,
    ),
    Field(
        "abstract",
        lambda i: i.get("abstractNote") or "",
        lambda b: _link_content(b).get("description") or "",
        lambda v: {"description": v or None},
        lambda v, i: {"abstractNote": v},
        links_only=True,
    ),
)


def decide(z: str, k: str, snap: dict | None, same: Callable[[str, str], bool]) -> str | None:
    """Which side's value wins: "z", "k", or None for nothing to do. See the module docstring."""
    if snap is None:
        return None if same(z, k) else "z" if z else "k" if k else None
    z_changed, k_changed = z != snap["z"], k != snap["k"]
    if z_changed and k_changed:
        return None if same(z, k) else "z"
    return "z" if z_changed else "k" if k_changed else None


# --- state ---------------------------------------------------------------------------------------

class State:
    """The last-synced values, one JSON file, replaced atomically (and only when changed)."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {"version": 1, "pairs": {}}
        self.pairs: dict[str, dict] = self.data["pairs"]
        self.dirty = False

    def put(self, key: str, value: dict) -> None:
        if self.pairs.get(key) != value:
            self.pairs[key] = value
            self.dirty = True

    def drop_except(self, keep: set[str]) -> None:
        for key in set(self.pairs) - keep:
            del self.pairs[key]
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self.dirty = False


# --- the sync ------------------------------------------------------------------------------------

class Sync:
    def __init__(
        self,
        karakeep: Karakeep,
        zotero: Zotero,
        state: State,
        *,
        bookmark_prefix: str,
        web_library: str,
        read_status: str,
        read_tag_format: str,
        write: Callable,
        dry_run: bool,
        now: Callable[[], float],
    ):
        self.karakeep = karakeep
        self.zotero = zotero
        self.state = state
        self.prefix = bookmark_prefix
        self.web_library = web_library
        self.read_status = read_status
        self.read_tag_format = read_tag_format
        self.write = write  # Handoff._write: does it, or logs it in a dry run
        self.dry_run = dry_run  # then the snapshot isn't updated either
        self.now = now

    def run(self, library: list[dict], bookmarks: dict[str, dict], skip: set[str]) -> int:
        """Sync every (item, bookmark) pair. `bookmarks`: the bookmarks tagged `paper`, by id;
        `skip`: those handed off in this run (they sync from the next one). Returns failures."""
        from .mirror import bookmark_ids

        pairs = [(item, bid) for item in library for bid in bookmark_ids(item, self.prefix)]
        self.state.drop_except({f"{item['key']} {bid}" for item, bid in pairs})
        notes: dict[str, list[dict]] = {}
        if pairs:
            for note in self.zotero.notes():
                notes.setdefault(note.get("parentItem") or "", []).append(note)
        failed = changed = 0
        for item, bid in pairs:
            bookmark = bookmarks.get(bid)
            if bookmark is None or bid in skip:
                continue  # no longer tagged `paper`, or handed off just now
            try:
                changed += self._pair(item, bookmark, notes.setdefault(item["key"], []))
            except Exception:
                failed += 1
                log.exception("syncing %s with bookmark %s failed; the next run retries it", item["key"], bid)
        log.info("sync: %d pair(s), %d changed", len(pairs), changed)
        return failed

    def _pair(self, item: dict, b: dict, notes: list[dict]) -> bool:
        key, bid = item["key"], b["id"]
        pair = f"{key} {bid}"
        snap = self.state.pairs.get(pair, {})
        link = f"{self.prefix}{bid}"
        is_link = (b.get("content") or {}).get("type") == "link"
        fields = [f for f in FIELDS if is_link or not f.links_only]
        z_patch: dict = {}
        k_patch: dict = {}

        for f in fields:
            z, k = f.zotero(item), f.karakeep(b)
            winner = decide(z, k, snap.get("fields", {}).get(f.name), f.same)
            if winner == "z" and (fields_k := f.to_karakeep(z)) is not None:
                log.info("%s -> bookmark %s: %s %r", key, bid, f.name, z[:80])
                k_patch.update(fields_k)
            elif winner == "k":
                log.info("bookmark %s -> %s: %s %r", bid, key, f.name, k[:80])
                z_patch.update(f.to_zotero(k, item))

        note = next((n for n in notes if f'href="{escape(link)}"' in (n.get("note") or "")), None)
        note = note or next((n for n in notes if n["key"] == snap.get("note")), None)
        z_text = note_comments(note["note"], link) if note else ""
        k_text = bookmark_comments(b.get("note"))
        text = self._merge_comments(z_text, k_text, snap.get("comments"))
        to_zotero_note = text is not None and text != z_text
        if text is not None and text != k_text:
            log.info("%s -> bookmark %s: comments", key, bid)
            k_patch["note"] = bookmark_note(text, key, self.web_library)

        read_tag = self._read(item, b, snap.get("read"), k_patch)

        if z_patch:
            version = self.write(f"update {key}", self.zotero.update, f"/items/{key}", item["version"], z_patch)
            item.update(z_patch, version=version or item["version"])
        if to_zotero_note:
            log.info("bookmark %s -> %s: comments", bid, key)
            note = self._write_note(item, note, text, link, notes)
        if k_patch:
            b = self.write(f"update bookmark {bid}", self.karakeep.update, bid, k_patch) or {**b, **k_patch}
        if read_tag != snap.get("read"):
            if read_tag:
                self.write(f"tag bookmark {bid} {read_tag!r}", self.karakeep.add_tag, bid, read_tag)
            else:
                self.write(f"untag bookmark {bid}", self.karakeep.remove_tag, bid, snap["read"])

        new_snap = {
            "fields": {f.name: {"z": f.zotero(item), "k": f.karakeep(b)} for f in fields},
            "comments": {
                "z": note_comments(note["note"], link) if note else "",
                "k": bookmark_comments(b.get("note")),
            },
            "note": note["key"] if note else None,
            "read": read_tag,
        }
        if not self.dry_run:
            self.state.put(pair, new_snap)
        return bool(z_patch or k_patch or to_zotero_note or read_tag != snap.get("read"))

    def _merge_comments(self, z: str, k: str, snap: dict | None) -> str | None:
        """The comments both sides should have, or None if nothing needs doing."""
        if snap is None:
            if z == k or not z or not k:
                return None if z == k else (z or k)
            return self._both(z, k)
        z_changed, k_changed = z != snap["z"], k != snap["k"]
        if z_changed and k_changed and z != k:
            return self._both(z, k)
        if z_changed:
            return z
        if k_changed:
            return k
        return None

    def _both(self, z: str, k: str) -> str:
        day = datetime.fromtimestamp(self.now(), UTC).strftime("%Y-%m-%d")
        log.warning("comments were edited in both Karakeep and Zotero; keeping both")
        return f"{k}\n\n[paperbridge {day}: edited in both Karakeep and Zotero; Zotero's text follows]\n\n{z}"

    def _write_note(self, item: dict, note: dict | None, text: str, link: str, notes: list[dict]) -> dict | None:
        html = note_html(text, link)
        if note:
            version = self.write(f"update note {note['key']}", self.zotero.update, f"/items/{note['key']}", note["version"], {"note": html})
            stored = self.write(f"read note {note['key']}", self.zotero.item, note["key"]) if version else None
        else:
            created = self.write(
                f"create a note on {item['key']}",
                self.zotero.create,
                [{"itemType": "note", "parentItem": item["key"], "note": html}],
            )
            stored = self.write("read the new note", self.zotero.item, created[0]["key"]) if created else None
        stored = stored or {**(note or {"key": None, "version": 0}), "note": html}  # dry run
        if note:
            notes[notes.index(note)] = stored
        else:
            notes.append(stored)
        return stored

    def _read(self, item: dict, b: dict, applied: str | None, k_patch: dict) -> str | None:
        """Read status, Zotero -> Karakeep: archive and tag when the item is marked read, undo
        when it no longer is. Returns the read tag now applied (or None)."""
        is_read = extra_value(item, "Read_Status").lower() == self.read_status.lower()
        if is_read and not applied:
            try:
                when = datetime.fromisoformat(extra_value(item, "Read_Status_Date"))
            except ValueError:
                when = datetime.fromtimestamp(self.now(), UTC)
            tag = when.strftime(self.read_tag_format)
            log.info("%s is marked %s: archiving bookmark %s, tag %r", item["key"], self.read_status, b["id"], tag)
            k_patch["archived"] = True
            return tag
        if applied and not is_read:
            log.info("%s is no longer marked %s: unarchiving bookmark %s", item["key"], self.read_status, b["id"])
            k_patch["archived"] = False
            return None
        return applied
