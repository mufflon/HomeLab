"""The whole handoff against in-memory fakes of Karakeep, Zotero, WebDAV and translation-server."""

import io
import zipfile
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from paperbridge.clients import Rejected
from paperbridge.config import Config
from paperbridge.handoff import Handoff, tag_names

NOW = datetime.fromisoformat("2026-10-16T12:00:00Z").timestamp()
PDF = b"%PDF-1.7 the paper"
ARXIV_ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<published>2017-06-12T17:57:34Z</published></entry></feed>"""
PREPRINT = [
    {
        "key": "TS000001",
        "version": 0,
        "itemType": "preprint",
        "title": "Attention Is All You Need",
        "creators": [{"firstName": "Ashish", "lastName": "Vaswani", "creatorType": "author"}],
        "date": "2023-08-02",
        "url": "http://arxiv.org/abs/1706.03762",
        "extra": "arXiv:1706.03762 [cs.CL]",
        "archiveID": "arXiv:1706.03762",
        "DOI": "10.48550/arXiv.1706.03762",
    },
    {"itemType": "note", "parentItem": "TS000001", "note": "Comment: 15 pages, 5 figures"},
]


class FakeKarakeep:
    def __init__(self, bookmarks):
        self.bookmarks = {b["id"]: b for b in bookmarks}
        self.writes = []

    def tag_id(self, name):
        return "tag-" + name

    def bookmarks_tagged(self, tag_id):
        return [b for b in self.bookmarks.values() if any(x["id"] == tag_id for x in b["tags"])]

    def bookmark(self, bid, include_content=False):
        return self.bookmarks[bid]

    def asset(self, asset_id):
        return PDF

    def set_note(self, bid, note):
        self.writes.append(("note", bid))
        self.bookmarks[bid]["note"] = note

    def add_tag(self, bid, name):
        self.writes.append(("tag", bid, name))
        self.bookmarks[bid]["tags"].append({"id": "tag-" + name, "name": name})

    def lists(self):
        return [{"id": "L1", "name": "Papers", "parentId": None, "type": "manual"}]

    def list_bookmark_ids(self, lid):
        return ["bm1"]


# Zotero's schema, as much of it as the tests use (GET /itemTypeCreatorTypes).
CREATOR_TYPES = {"thesis": {"author", "contributor"}}
ALL_CREATOR_TYPES = {"author", "contributor", "editor", "reviewedAuthor", "translator"}


class FakeZotero:
    def __init__(self):
        self.items = {}
        self.writes = []
        self.scans = 0
        self.collections_ = {}
        self.refuse = set()  # item types to refuse, as Zotero refuses an invalid item

    def creator_types(self, item_type):
        return CREATOR_TYPES.get(item_type, ALL_CREATOR_TYPES)

    def key_info(self):
        return {"userID": 1, "username": "owner", "access": {"user": {"library": True, "write": True}}}

    def top_items(self):
        self.scans += 1
        return [dict(i) for i in self.items.values() if "parentItem" not in i]

    def children(self, key):
        return [dict(i) for i in self.items.values() if i.get("parentItem") == key]

    def collections(self):
        return [dict(c) for c in self.collections_.values()]

    def create(self, objects, kind="items"):
        if kind == "collections":
            self.writes.append(("create", ["collection"] * len(objects)))
            self.collections_.update({o["key"]: {**o, "version": 1} for o in objects})
            return
        self.writes.append(("create", [o["itemType"] for o in objects]))
        # Like Zotero: invalid objects are refused, and so are children of a refused parent.
        failed, refused = {}, set()
        for i, o in enumerate(objects):
            bad = [c["creatorType"] for c in o.get("creators", [])
                   if c["creatorType"] not in self.creator_types(o["itemType"])]
            if bad or o["itemType"] in self.refuse:
                failed[str(i)] = {"code": 400, "message": f"invalid: {bad or o['itemType']}"}
                refused.add(o.get("key"))
            elif o.get("parentItem") in refused:
                failed[str(i)] = {"code": 409, "message": f"Parent item {o['parentItem']} not found"}
        objects = [o for i, o in enumerate(objects) if str(i) not in failed]
        stored = []
        for o in objects:
            # Like Zotero: a client-chosen key needs version 0 (or If-Unmodified-Since-Version).
            assert "key" not in o or o.get("version") == 0, f"key without version: {o}"
            key = o.get("key") or f"SRV{len(self.items):05d}"  # the server assigns missing keys
            self.items[key] = {**o, "key": key, "version": 1}
            stored.append(dict(self.items[key]))
        if failed:
            raise Rejected("items", failed)
        return stored

    def update(self, path, version, fields):
        key = path.removeprefix("/items/")
        assert version == self.items[key]["version"], "stale version"
        self.writes.append(("update", key))
        self.items[key].update(fields, version=version + 1)
        return version + 1


class FakeWebDAV:
    def __init__(self):
        self.files = {}

    def put(self, name, data, content_type):
        self.files[name] = data

    def delete(self, name):
        self.files.pop(name, None)


class FakeTS:
    def __init__(self, answers):
        self.answers = answers

    def web(self, url):
        return self.answers.get(url)

    def search(self, q):
        return self.answers.get(q)


def web():
    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=ARXIV_ATOM)
        if request.url.host == "arxiv.org" and request.url.path.startswith("/pdf/"):
            return httpx.Response(200, content=PDF)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def bookmark(bid, url, created="2026-10-16T11:00:00.000Z", **content):
    return {
        "id": bid,
        "createdAt": created,
        "title": None,
        "note": None,
        "tags": [{"id": "tag-paper", "name": "paper"}],
        "content": {"type": "link", "url": url, "crawlStatus": "success", **content},
    }


CFG = Config(
    karakeep_url="http://karakeep",
    karakeep_public_url="https://karakeep.example",
    karakeep_api_key="k",
    translation_server_url="http://ts",
    zotero_api_url="https://api.zotero.example",
    zotero_user_id="1",
    zotero_api_key="z",
    webdav_url="http://dav/zotero/",
    webdav_username="u",
    webdav_password="p",
)


@pytest.fixture
def world():
    karakeep = FakeKarakeep([bookmark("bm1", "https://arxiv.org/abs/1706.03762")])
    zotero, dav = FakeZotero(), FakeWebDAV()
    ts = FakeTS({"arXiv:1706.03762": PREPRINT})
    handoff = Handoff(CFG, karakeep, zotero, dav, ts, web(), now=lambda: NOW)
    return handoff, karakeep, zotero, dav


def test_hands_off_once(world):
    handoff, karakeep, zotero, dav = world
    assert handoff.run() == 0

    [parent] = [i for i in zotero.items.values() if i["itemType"] == "preprint"]
    assert parent["date"] == "2017-06-12"  # arXiv v1, not the latest version
    assert parent["extra"] == (
        "arXiv:1706.03762 [cs.CL]\nKarakeep: https://karakeep.example/dashboard/preview/bm1"
    )
    [note] = [i for i in zotero.items.values() if i["itemType"] == "note"]
    assert note["parentItem"] == parent["key"]
    [att] = [i for i in zotero.items.values() if i["itemType"] == "attachment"]
    assert att["parentItem"] == parent["key"] and att["linkMode"] == "imported_file"
    assert att["filename"] == "Vaswani - 2017 - Attention Is All You Need.pdf"

    zipped = zipfile.ZipFile(io.BytesIO(dav.files[f"{att['key']}.zip"]))
    assert zipped.read(att["filename"]) == PDF
    assert f"<hash>{att['md5']}</hash>".encode() in dav.files[f"{att['key']}.prop"]

    bm = karakeep.bookmarks["bm1"]
    assert "in-zotero" in {x["name"] for x in bm["tags"]}
    assert f"https://www.zotero.org/owner/items/{parent['key']}" in bm["note"]

    # A second run changes nothing.
    zotero.writes.clear(), karakeep.writes.clear()
    assert handoff.run() == 0
    assert zotero.writes == [] and karakeep.writes == []


def test_finds_its_own_item_after_a_crash(world):
    handoff, karakeep, zotero, dav = world
    handoff.run()
    bm = karakeep.bookmarks["bm1"]
    bm["tags"] = [x for x in bm["tags"] if x["name"] != "in-zotero"]  # as if tagging never happened
    zotero.writes.clear()
    handoff.run()
    assert zotero.writes == []
    assert "in-zotero" in {x["name"] for x in bm["tags"]}


def test_links_a_second_bookmark_with_the_same_doi(world):
    handoff, karakeep, zotero, dav = world
    handoff.run()
    karakeep.bookmarks["bm2"] = bookmark("bm2", "https://arxiv.org/pdf/1706.03762v7")
    zotero.writes.clear()
    handoff.run()
    [parent] = [i for i in zotero.items.values() if i["itemType"] == "preprint"]
    assert zotero.writes == [("update", parent["key"])]  # Extra gains a line, no new item or PDF
    assert parent["extra"].endswith("/dashboard/preview/bm1\nKarakeep: https://karakeep.example/dashboard/preview/bm2")


def test_two_bookmarks_of_one_paper_in_one_run(world):
    handoff, karakeep, zotero, dav = world
    karakeep.bookmarks["bm2"] = bookmark("bm2", "https://arxiv.org/pdf/1706.03762v7")
    karakeep.bookmarks["bm3"] = bookmark("bm3", "https://arxiv.org/abs/1706.03762v2")
    assert handoff.run() == 0
    assert [i["itemType"] for i in zotero.items.values()].count("preprint") == 1
    [parent] = [i for i in zotero.items.values() if i["itemType"] == "preprint"]
    assert parent["extra"].count("Karakeep: ") == 3
    assert zotero.scans == 1


def test_karakeeps_pdf_copy_wins(world):
    handoff, karakeep, zotero, dav = world
    karakeep.bookmarks["bm1"]["content"] = {
        "type": "asset", "assetType": "pdf", "assetId": "asset1",
        "sourceUrl": "https://arxiv.org/pdf/1706.03762", "content": "Attention Is All You Need ...",
    }
    handoff.run()
    [att] = [i for i in zotero.items.values() if i["itemType"] == "attachment"]
    assert len(dav.files) == 2 and att["md5"]


def test_page_print_is_not_the_paper(world):
    handoff, karakeep, zotero, dav = world
    karakeep.bookmarks["bm1"] = bookmark("bm1", "https://example.org/paper", pdfAssetId="print1")
    handoff.ts = FakeTS({"https://example.org/paper": [{"itemType": "journalArticle", "title": "X"}]})
    handoff.run()
    assert [i["itemType"] for i in zotero.items.values()] == ["journalArticle"]
    assert dav.files == {}


def test_waits_for_metadata_then_falls_back(world):
    handoff, karakeep, zotero, dav = world
    handoff.ts = FakeTS({})
    handoff.run()
    assert zotero.items == {} and karakeep.writes == []  # young bookmark: try again later

    karakeep.bookmarks["bm1"]["createdAt"] = "2026-10-14T00:00:00.000Z"
    karakeep.bookmarks["bm1"]["content"]["url"] = "https://example.org/x"
    handoff.run()
    [parent] = zotero.items.values()
    assert parent["itemType"] == "webpage" and {"tag": "needs-metadata"} in parent["tags"]


def test_still_crawling_waits(world):
    handoff, karakeep, zotero, dav = world
    karakeep.bookmarks["bm1"]["content"]["crawlStatus"] = "pending"
    handoff.run()
    assert zotero.items == {}


def test_a_new_paper_is_filed_in_the_same_run(world):
    handoff, karakeep, zotero, dav = world
    handoff.cfg = replace(CFG, mirror_lists=("Papers",))
    assert handoff.run() == 0
    [parent] = [i for i in zotero.items.values() if i["itemType"] == "preprint"]
    [papers] = zotero.collections_
    assert parent["collections"] == [papers]
    zotero.writes.clear()
    handoff.library = None  # a new run reads the library again
    assert handoff.run() == 0 and zotero.writes == []


def test_dry_run_writes_nothing(world):
    handoff, karakeep, zotero, dav = world
    handoff.dry_run = True
    assert handoff.run() == 0
    assert zotero.items == {} and dav.files == {} and karakeep.writes == []


def zotero_only(zotero, title="Attention Is All You Need"):
    """A paper added in Zotero directly: no Karakeep line, no DOI, its own PDF."""
    zotero.items["OWN00001"] = {"key": "OWN00001", "version": 5, "itemType": "journalArticle", "title": title, "extra": ""}
    zotero.items["OWNPDF01"] = {
        "key": "OWNPDF01", "version": 5, "itemType": "attachment", "parentItem": "OWN00001",
        "linkMode": "imported_file", "contentType": "application/pdf",
    }


def uploaded(file_name, text):
    b = bookmark("up1", None)
    b["content"] = {"type": "asset", "assetType": "pdf", "assetId": "a1", "fileName": file_name, "content": text}
    return b


@pytest.mark.parametrize(
    "file_name, text",
    [
        ("Vaswani et al. - 2017 - Attention Is All You Need.pdf", "no identifiers here"),  # Zotero's file name
        ("paper.pdf", "Provided proper attribution...\nAttention Is All You\nNeed\nAshish Vaswani"),  # page 1
    ],
)
def test_a_pdf_taken_out_of_zotero_links_to_its_item(world, file_name, text):
    handoff, karakeep, zotero, dav = world
    zotero_only(zotero)
    karakeep.bookmarks = {"up1": uploaded(file_name, text)}
    handoff.ts = FakeTS({})  # nothing to translate
    assert handoff.run() == 0
    assert sorted(zotero.items) == ["OWN00001", "OWNPDF01"] and dav.files == {}  # no duplicate, no second PDF
    assert zotero.items["OWN00001"]["extra"] == "Karakeep: https://karakeep.example/dashboard/preview/up1"
    assert "in-zotero" in tag_names(karakeep.bookmarks["up1"])


def test_a_title_cited_on_page_one_does_not_win(world):
    handoff, karakeep, zotero, dav = world
    zotero_only(zotero)
    text = "Some Newer Paper About Transformers\nWe build on Attention Is All You Need. doi:10.1000/newer"
    karakeep.bookmarks = {"up1": uploaded("newer.pdf", text)}
    handoff.ts = FakeTS({"10.1000/newer": [{"itemType": "journalArticle", "title": "Some Newer Paper About Transformers"}]})
    handoff.run()
    titles = sorted(i.get("title") for i in zotero.items.values() if "parentItem" not in i)
    assert titles == ["Attention Is All You Need", "Some Newer Paper About Transformers"]


def test_short_titles_are_not_matched(world):
    handoff, karakeep, zotero, dav = world
    zotero_only(zotero, title="Deep learning")
    karakeep.bookmarks = {"up1": uploaded("A - 2015 - Deep learning.pdf", "Deep learning\nY. LeCun")}
    karakeep.bookmarks["up1"]["createdAt"] = "2026-10-01T00:00:00.000Z"  # past the grace period
    handoff.ts = FakeTS({})
    handoff.run()
    assert len([i for i in zotero.items.values() if "parentItem" not in i]) == 2  # a new, minimal item


def test_a_creator_type_the_item_type_does_not_allow_becomes_contributor(world):
    handoff, karakeep, zotero, dav = world
    thesis = {"itemType": "thesis", "title": "Demand modeling for water networks", "date": "2016",
              "creators": [{"firstName": "G.", "lastName": "Student", "creatorType": "author"},
                           {"firstName": "P.", "lastName": "Supervisor", "creatorType": "editor"}]}
    handoff.ts.answers = {"arXiv:1706.03762": [thesis]}
    assert handoff.run() == 0
    [parent] = [i for i in zotero.items.values() if i["itemType"] == "thesis"]
    assert [c["creatorType"] for c in parent["creators"]] == ["author", "contributor"]


def test_a_refused_item_leaves_no_files_on_webdav(world):
    handoff, karakeep, zotero, dav = world
    zotero.refuse = {"preprint"}
    assert handoff.run() == 1  # failed: the next run retries it
    assert zotero.items == {} and dav.files == {}
    assert "in-zotero" not in {x["name"] for x in karakeep.bookmarks["bm1"]["tags"]}

    zotero.refuse = set()
    assert handoff.run() == 0
    [att] = [i for i in zotero.items.values() if i["itemType"] == "attachment"]
    assert set(dav.files) == {f"{att['key']}.zip", f"{att['key']}.prop"}
