"""Mirroring chosen Karakeep lists into Zotero collections, against in-memory fakes."""

import itertools

import pytest

from paperbridge.mirror import Mirror, bookmark_ids, paths

PREFIX = "https://karakeep.example/dashboard/preview/"


class FakeKarakeep:
    def __init__(self):
        self.lists_ = {}  # id -> list
        self.members = {}  # list id -> bookmark ids

    def add_list(self, lid, name, parent=None, type="manual"):
        self.lists_[lid] = {"id": lid, "name": name, "parentId": parent, "type": type}
        self.members.setdefault(lid, [])

    def lists(self):
        return list(self.lists_.values())

    def list_bookmark_ids(self, lid):
        return list(self.members[lid])


class FakeZotero:
    def __init__(self):
        self.collections_ = {}
        self.items = {}
        self.writes = []
        self.version = itertools.count(100)

    def collections(self):
        return [dict(c) for c in self.collections_.values()]

    def create(self, objects, kind):
        assert kind == "collections"
        for o in objects:
            assert o["version"] == 0
            assert o["parentCollection"] is False or o["parentCollection"] in self.collections_
            self.collections_[o["key"]] = {**o, "version": next(self.version)}
        self.writes.append(("create", [o["name"] for o in objects]))

    def update(self, path, version, fields):
        kind, key = path.strip("/").split("/")
        target = self.items[key] if kind == "items" else self.collections_[key]
        assert version == target["version"], "stale version"
        target.update(fields, version=next(self.version))
        self.writes.append((kind, key, fields))
        return target["version"]

    def collection_is_empty(self, key):
        in_use = any(key in i.get("collections", []) for i in self.items.values())
        children = any(c.get("parentCollection") == key and not c.get("deleted") for c in self.collections_.values())
        return not in_use and not children

    def library(self):
        return [dict(i) for i in self.items.values()]

    def path_of(self, key):
        return paths(self.collections_, "parentCollection")[key]

    def collections_of(self, item_key):
        return sorted("/".join(self.path_of(k)) for k in self.items[item_key].get("collections", []))

    def live(self):
        return sorted("/".join(self.path_of(k)) for k, c in self.collections_.items() if not c.get("deleted"))


def paper(key, *bookmarks, collections=()):
    extra = "\n".join(f"Karakeep: {PREFIX}{b}" for b in bookmarks)
    return {"key": key, "version": 1, "title": key, "extra": extra, "collections": list(collections)}


@pytest.fixture
def world():
    kk, z = FakeKarakeep(), FakeZotero()
    kk.add_list("L1", "Papers")
    kk.add_list("L2", "Digital twin", parent="L1")
    kk.add_list("L3", "Forecasting", parent="L1")
    kk.add_list("L4", "Groceries")  # not mirrored
    z.items["P1"] = paper("P1", "bm1")
    keys = (f"C{n:07d}" for n in itertools.count())
    mirror = Mirror(kk, z, roots=("Papers",), bookmark_prefix=PREFIX, write=lambda what, fn, *a: fn(*a), new_key=lambda: next(keys))

    def run():
        library = z.library()
        assert mirror.run(library) == 0
        return library

    return kk, z, mirror, run


def test_bookmark_ids():
    item = {"extra": f"arXiv:1 [cs]\nKarakeep: {PREFIX}a\nKarakeep: {PREFIX}b\nKarakeep: https://other/x"}
    assert bookmark_ids(item, PREFIX) == ["a", "b"]


def test_files_and_mirrors_only_the_chosen_tree(world):
    kk, z, mirror, run = world
    kk.members["L2"].append("bm1")
    kk.members["L4"].append("bm1")
    run()
    assert z.live() == ["Papers", "Papers/Digital twin", "Papers/Forecasting"]  # no Groceries
    assert z.collections_of("P1") == ["Papers/Digital twin"]

    z.writes.clear()
    run()
    assert z.writes == []  # a second run changes nothing


def test_moving_a_bookmark_moves_the_item(world):
    kk, z, mirror, run = world
    kk.members["L2"].append("bm1")
    run()
    kk.members["L2"].remove("bm1")
    kk.members["L3"].append("bm1")
    run()
    assert z.collections_of("P1") == ["Papers/Forecasting"]


def test_removed_list_goes_to_the_trash_once_empty(world):
    kk, z, mirror, run = world
    kk.members["L2"].append("bm1")
    run()
    del kk.lists_["L2"], kk.members["L2"]
    run()
    assert z.collections_of("P1") == []  # in no mirrored list now: unfiled
    assert z.live() == ["Papers", "Papers/Forecasting"]
    [trashed] = [c for c in z.collections_.values() if c.get("deleted")]
    assert trashed["name"] == "Digital twin"


def test_papers_added_in_zotero_are_never_touched(world):
    kk, z, mirror, run = world
    run()
    ml = next(k for k, c in z.collections_.items() if c["name"] == "Digital twin")
    z.items["OWN"] = {"key": "OWN", "version": 1, "title": "mine", "collections": [ml]}
    del kk.lists_["L2"], kk.members["L2"]
    run()
    assert z.items["OWN"]["collections"] == [ml] and z.items["OWN"]["version"] == 1
    assert "Papers/Digital twin" in z.live()  # still holds a paper of the owner's: kept


def test_collections_outside_the_mirror_are_kept(world):
    kk, z, mirror, run = world
    z.collections_["MINE"] = {"key": "MINE", "version": 1, "name": "Reading group", "parentCollection": False}
    z.items["P1"]["collections"] = ["MINE"]
    kk.members["L3"].append("bm1")
    run()
    assert z.collections_of("P1") == ["Papers/Forecasting", "Reading group"]


def test_a_root_no_longer_mirrored_is_left_alone(world):
    kk, z, mirror, run = world
    kk.members["L2"].append("bm1")
    run()
    mirror.roots = ()
    assert mirror.run(z.library()) == 0
    mirror.roots = ("Gone",)  # named, but no such list in Karakeep
    z.writes.clear()
    run()
    assert z.writes == [] and z.collections_of("P1") == ["Papers/Digital twin"]


def test_trashed_collections_and_smart_lists_are_ignored(world):
    kk, z, mirror, run = world
    z.collections_["OLD"] = {"key": "OLD", "version": 1, "name": "Papers", "parentCollection": False, "deleted": True}
    kk.add_list("L5", "Recent", parent="L1", type="smart")
    kk.members["L5"].append("bm1")
    run()
    assert z.collections_["OLD"] == {"key": "OLD", "version": 1, "name": "Papers", "parentCollection": False, "deleted": True}
    assert "Papers/Recent" not in z.live() and z.collections_of("P1") == []
