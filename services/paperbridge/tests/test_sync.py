"""Two-way sync of metadata and comments, and read status, against in-memory fakes."""

import copy
import json
from datetime import datetime

import pytest

from paperbridge.sync import (
    State,
    Sync,
    highlights_section,
    split_note,
    _creators,
    bookmark_comments,
    extra_value,
    html_text,
    note_comments,
    note_html,
)

PREFIX = "https://karakeep.example/dashboard/preview/"
WEB = "https://www.zotero.org/owner/items"
NOW = datetime.fromisoformat("2026-10-16T12:00:00Z").timestamp()
LINKS = "Zotero: zotero://select/library/items/ITEM0001\nhttps://www.zotero.org/owner/items/ITEM0001"


class FakeKarakeep:
    def __init__(self, bookmarks):
        self.bookmarks = {b["id"]: b for b in bookmarks}
        self.writes = []

    def update(self, bid, fields):
        self.writes.append(("update", bid, sorted(fields)))
        b = self.bookmarks[bid]
        for name, value in fields.items():
            if name in ("author", "description", "datePublished"):
                b["content"][name] = value
            else:
                b[name] = value
        return copy.deepcopy(b)

    def add_tag(self, bid, name):
        self.writes.append(("tag", bid, name))
        self.bookmarks[bid]["tags"].append({"name": name})

    def remove_tag(self, bid, name):
        self.writes.append(("untag", bid, name))
        self.bookmarks[bid]["tags"] = [t for t in self.bookmarks[bid]["tags"] if t["name"] != name]


class FakeZotero:
    """Rewrites note HTML on save, as Zotero desktop and the server's sanitizer do."""

    def __init__(self, items):
        self.items = {i["key"]: i for i in items}
        self.writes = []
        self.n = 0
        self.annotation_reads = 0

    def library_version(self):
        return hash(json.dumps(self.items, sort_keys=True))  # any change moves it

    def attachments(self):
        return [copy.deepcopy(i) for i in self.items.values() if i["itemType"] == "attachment"]

    def annotations(self):
        self.annotation_reads += 1
        return [copy.deepcopy(i) for i in self.items.values() if i["itemType"] == "annotation"]

    def notes(self):
        return [copy.deepcopy(i) for i in self.items.values() if i["itemType"] == "note"]

    def item(self, key):
        return copy.deepcopy(self.items[key])

    def update(self, path, version, fields):
        key = path.removeprefix("/items/")
        assert version == self.items[key]["version"], "stale version"
        self.writes.append(("update", key, sorted(fields)))
        if "note" in fields:
            fields = {**fields, "note": f'<div data-schema-version="9">{fields["note"]}</div>'}
        self.items[key].update(fields, version=version + 1)
        return version + 1

    def create(self, objects):
        self.writes.append(("create", [o["itemType"] for o in objects]))
        created = []
        for o in objects:
            self.n += 1
            key = f"NOTE{self.n:04d}"
            self.items[key] = {**o, "key": key, "version": 1, "note": f'<div data-schema-version="9">{o["note"]}</div>'}
            created.append(copy.deepcopy(self.items[key]))
        return created


def item(**fields):
    return {
        "key": "ITEM0001",
        "version": 1,
        "itemType": "preprint",
        "title": "Attention Is All You Need",
        "creators": [
            {"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"},
            {"creatorType": "author", "firstName": "Noam", "lastName": "Shazeer"},
        ],
        "date": "2017-06-12",
        "abstractNote": "The dominant sequence transduction models ...",
        "extra": f"arXiv:1706.03762 [cs.CL]\nKarakeep: {PREFIX}bm1",
        **fields,
    }


def link_bookmark(**fields):
    return {
        "id": "bm1",
        "title": None,
        "note": LINKS,
        "archived": False,
        "tags": [{"name": "paper"}, {"name": "in-zotero"}],
        "content": {
            "type": "link",
            "url": "https://arxiv.org/abs/1706.03762",
            "title": "[1706.03762] Attention Is All You Need",
            "author": None,
            "description": "Abstract page for arXiv paper 1706.03762",
            "datePublished": None,
        },
        **fields,
    }


@pytest.fixture
def world(tmp_path):
    kk = FakeKarakeep([link_bookmark()])
    z = FakeZotero([item()])
    path = str(tmp_path / "state.json")

    def run(dry_run=False):
        state = State(path)
        sync = Sync(
            kk, z, state, bookmark_prefix=PREFIX, web_library=WEB, read_status="Read",
            read_tag_format="read-%Y-%m", write=lambda what, fn, *a: None if dry_run else fn(*a),
            dry_run=dry_run, now=lambda: NOW,
        )
        library = [copy.deepcopy(i) for i in z.items.values() if "parentItem" not in i]
        assert sync.run(library, copy.deepcopy(kk.bookmarks), set()) == 0
        if not dry_run:
            state.save()
        # the library copy was updated in place; write versions back like a later read would
        for i in library:
            z.items[i["key"]]["version"] = i["version"]

    def quiet():
        """A further run changes nothing."""
        z.writes.clear(), kk.writes.clear()
        run()
        assert z.writes == [] and kk.writes == []

    return kk, z, run, quiet


def bm(kk):
    return kk.bookmarks["bm1"]


def test_first_sync_zotero_wins_where_it_has_a_value(world):
    kk, z, run, quiet = world
    run()
    assert bm(kk)["title"] == "Attention Is All You Need"
    assert bm(kk)["content"]["author"] == "Ashish Vaswani; Noam Shazeer"
    assert bm(kk)["content"]["datePublished"] == "2017-06-12T00:00:00.000Z"
    assert bm(kk)["content"]["description"].startswith("The dominant")
    assert z.writes == []  # no comments anywhere: no note
    quiet()


def test_a_pdf_bookmark_syncs_title_and_comments_only(world):
    kk, z, run, quiet = world
    kk.bookmarks["bm1"] = {"id": "bm1", "title": None, "note": f"Read §3 first\n\n{LINKS}", "archived": False,
                           "tags": [], "content": {"type": "asset", "assetType": "pdf"}}
    run()
    assert bm(kk)["title"] == "Attention Is All You Need" and "author" not in bm(kk)["content"]
    [note] = [i for i in z.items.values() if i["itemType"] == "note"]
    assert note_comments(note["note"], PREFIX + "bm1") == "Read §3 first"
    assert note["parentItem"] == "ITEM0001"
    quiet()


def test_an_edit_on_either_side_crosses_over(world):
    kk, z, run, quiet = world
    run()
    bm(kk)["title"] = "Attention is all you need (Transformer)"
    run()
    assert z.items["ITEM0001"]["title"] == "Attention is all you need (Transformer)"
    quiet()
    z.items["ITEM0001"]["abstractNote"] = "Shorter abstract."
    run()
    assert bm(kk)["content"]["description"] == "Shorter abstract."
    quiet()
    bm(kk)["content"]["author"] = "Ashish Vaswani; Noam Shazeer; Niki Parmar"
    z.items["ITEM0001"]["creators"].append({"creatorType": "editor", "lastName": "Editor"})
    run()
    creators = z.items["ITEM0001"]["creators"]
    assert [c.get("lastName") for c in creators] == ["Vaswani", "Shazeer", "Parmar", "Editor"]
    quiet()


def test_both_changed_zotero_wins_for_metadata(world):
    kk, z, run, quiet = world
    run()
    bm(kk)["title"] = "Karakeep's title"
    z.items["ITEM0001"]["title"] = "Zotero's title"
    run()
    assert bm(kk)["title"] == "Zotero's title" and z.items["ITEM0001"]["title"] == "Zotero's title"
    quiet()


def test_comments_go_both_ways_and_keep_both_on_conflict(world):
    kk, z, run, quiet = world
    bm(kk)["note"] = f"Key idea: attention only.\n\n{LINKS}"
    run()
    [key] = [k for k, i in z.items.items() if i["itemType"] == "note"]
    quiet()

    # Edited in Zotero desktop, which keeps its own HTML around our paragraphs.
    z.items[key]["note"] = z.items[key]["note"].replace("attention only.", "attention only.</p><p>See fig. 2.")
    run()
    assert bm(kk)["note"] == f"Key idea: attention only.\n\nSee fig. 2.\n\n{LINKS}"  # links kept last
    quiet()

    bm(kk)["note"] = f"From Karakeep's side.\n\n{LINKS}"
    z.items[key]["note"] = note_html("From Zotero's side.", PREFIX + "bm1")
    run()
    merged = bookmark_comments(bm(kk)["note"])
    assert merged.startswith("From Karakeep's side.") and merged.endswith("From Zotero's side.")
    assert "edited in both Karakeep and Zotero" in merged
    assert note_comments(z.items[key]["note"], PREFIX + "bm1") == merged
    quiet()


def test_read_status_archives_and_tags_then_undoes(world):
    kk, z, run, quiet = world
    run()
    z.items["ITEM0001"]["extra"] += "\nRead_Status: Read\nRead_Status_Date: 2026-10-14T09:30:00.000Z"
    run()
    assert bm(kk)["archived"] is True and {"name": "read-2026-10"} in bm(kk)["tags"]
    quiet()
    z.items["ITEM0001"]["extra"] = z.items["ITEM0001"]["extra"].replace("Read_Status: Read", "Read_Status: In Progress")
    run()
    assert bm(kk)["archived"] is False and {"name": "read-2026-10"} not in bm(kk)["tags"]
    quiet()


def test_dry_run_changes_and_saves_nothing(world, tmp_path):
    kk, z, run, quiet = world
    run(dry_run=True)
    assert kk.writes == [] and z.writes == [] and not (tmp_path / "state.json").exists()


def test_unlinked_pairs_are_forgotten(world, tmp_path):
    kk, z, run, quiet = world
    run()
    assert State(str(tmp_path / "state.json")).pairs
    z.items["ITEM0001"]["extra"] = "arXiv:1706.03762 [cs.CL]"
    run()
    assert State(str(tmp_path / "state.json")).pairs == {}


def test_helpers():
    assert extra_value({"extra": "Read_Status: To Read\nread_status: Read"}, "Read_Status") == "Read"
    assert html_text("<p>a<br>b</p>\n<p>c &amp; d</p>") == "a\nb\n\nc & d"
    assert bookmark_comments(f"x\n\n{LINKS}\n") == "x"
    assert note_comments(note_html("one\n\ntwo", PREFIX + "b"), PREFIX + "b") == "one\n\ntwo"
    assert [c.get("name") or c["lastName"] for c in _creators("Plato; Aidan N. Gomez", {"creators": []})] == ["Plato", "Gomez"]


def annotation(key, sort, text="", comment="", kind="highlight", page="3"):
    return {"key": key, "version": 1, "itemType": "annotation", "parentItem": "PDF00001", "annotationType": kind,
            "annotationText": text, "annotationComment": comment, "annotationPageLabel": page,
            "annotationSortIndex": sort}


def test_highlights_go_to_the_bookmark_note_and_follow_zotero(world):
    kk, z, run, quiet = world
    z.items["PDF00001"] = {"key": "PDF00001", "version": 1, "itemType": "attachment", "parentItem": "ITEM0001"}
    z.items["ANN00002"] = annotation("ANN00002", "00004|000100|00200", "multi-head\nattention", "Key idea", page="5")
    z.items["ANN00001"] = annotation("ANN00001", "00002|000010|00100", "The dominant sequence transduction models")
    bm(kk)["note"] = f"My own comment.\n\n{LINKS}"
    run()
    note = bm(kk)["note"]
    assert note.startswith("My own comment.\n\nHighlights from Zotero:\n- p. 3: \"The dominant")
    assert '- p. 5: "multi-head attention"\n  Key idea\n\nZotero: zotero://' in note  # reading order, links last
    assert split_note(note)[0] == "My own comment."
    [own_note] = [i for i in z.items.values() if i["itemType"] == "note"]  # comments only, no highlights
    assert "Highlights" not in own_note["note"]
    quiet()  # this run reads annotations again once: the last run's own writes moved the version
    reads = z.annotation_reads
    quiet()
    assert z.annotation_reads == reads  # library unchanged since: annotations not read again

    z.items["ANN00001"].update(annotationComment="Now with a comment", version=2)  # edited in Zotero
    del z.items["ANN00002"]
    run()
    assert '- p. 3: "The dominant sequence transduction models"\n  Now with a comment\n\nZotero' in bm(kk)["note"]
    assert "multi-head" not in bm(kk)["note"]
    quiet()

    # Typing into the highlights section in Karakeep is undone; a comment edit above it syncs.
    comments, section = split_note(bm(kk)["note"])
    bm(kk)["note"] = f"My own comment, edited.\n\n{section}\n- my addition\n\n{LINKS}"
    run()
    assert "my addition" not in bm(kk)["note"] and bm(kk)["note"].startswith("My own comment, edited.\n\nHighlights")
    assert "My own comment, edited." in [i for i in z.items.values() if i["itemType"] == "note"][0]["note"]
    quiet()


def test_highlights_section_formats():
    notes = [
        annotation("A", "1", comment="Check this proof\nagain", kind="note", page="7"),
        annotation("B", "0", text="x", page=""),
        annotation("C", "2", kind="image"),  # no text, no comment: left out
    ]
    assert highlights_section(notes) == 'Highlights from Zotero:\n- "x"\n- p. 7, note: Check this proof\n  again'
    assert highlights_section([]) == ""
