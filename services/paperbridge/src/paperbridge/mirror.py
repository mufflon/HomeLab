"""Chosen Karakeep lists -> Zotero collections, one way, every run.

The structure for papers is kept in Karakeep (see CLAUDE.md, paperbridge "Structure"):
- MIRROR_LISTS names top-level Karakeep lists. Each becomes a top-level collection of the
  same name, with its manual sublists as subcollections. Nothing else in Karakeep is mirrored.
- An item paperbridge handed off sits, within those trees, in exactly the collections of its
  bookmarks' lists: moving a bookmark moves the item. Its other collections are left alone.
- Inside a mirrored tree, a collection whose list is gone goes to Zotero's trash once empty.

Never touched: items without a Karakeep line (papers added in Zotero directly), collections
outside the mirrored trees, and collections already in the trash. Collections are matched by
their names from the top, as Zotero has no field for a list ID. A top-level list that's no
longer mirrored (or no longer exists) leaves its collection as it is.
"""

import logging
from collections.abc import Callable

from .clients import Karakeep, Zotero

log = logging.getLogger(__name__)

Path = tuple[str, ...]  # a list's or collection's names from the top: ("ML", "Transformers")


def bookmark_ids(item: dict, prefix: str) -> list[str]:
    """The Karakeep bookmarks an item came from, by its "Karakeep: <prefix><id>" lines."""
    marker = f"Karakeep: {prefix}"
    return [
        line.removeprefix(marker).strip()
        for line in (item.get("extra") or "").splitlines()
        if line.startswith(marker)
    ]


def paths(nodes: dict[str, dict], parent_key: str) -> dict[str, Path]:
    """Every node's path of names, following `parent_key` up (cycles and dangling parents end it)."""
    result = {}
    for key, node in nodes.items():
        names, seen = [], set()
        while node is not None and node["key"] not in seen:
            seen.add(node["key"])
            names.append(node["name"])
            node = nodes.get(node.get(parent_key) or "")
        result[key] = tuple(reversed(names))
    return result


class Mirror:
    def __init__(
        self,
        karakeep: Karakeep,
        zotero: Zotero,
        *,
        roots: tuple[str, ...],
        bookmark_prefix: str,
        write: Callable,
        new_key: Callable[[], str],
    ):
        self.karakeep = karakeep
        self.zotero = zotero
        self.roots = roots  # MIRROR_LISTS
        self.prefix = bookmark_prefix  # https://karakeep…/dashboard/preview/
        self.write = write  # Handoff._write: does it, or logs it in a dry run
        self.new_key = new_key

    def run(self, library: list[dict]) -> int:
        """Bring the mirrored collections, and the handed-off items' places in them, in line with
        Karakeep. `library` is the top-level items, updated in place. Returns how many changes
        failed (the next run retries them)."""
        if not self.roots:
            return 0
        lists = {l["id"]: {**l, "key": l["id"]} for l in self.karakeep.lists()}
        list_paths = paths(lists, "parentId")
        top = {path[0] for path in list_paths.values() if len(path) == 1}
        for name in self.roots:
            if name not in top:
                log.warning("MIRROR_LISTS: no top-level Karakeep list %r; its collection is left as it is", name)
        scope = set(self.roots) & top
        mirrored = {
            lid: path
            for lid, path in list_paths.items()
            if path[0] in scope and lists[lid].get("type", "manual") == "manual"
        }  # smart lists are saved searches: their contents shift on their own
        needed = {path[:n] for path in mirrored.values() for n in range(1, len(path) + 1)}

        papers = {bid: item for item in library for bid in bookmark_ids(item, self.prefix)}
        wanted: dict[str, set[Path]] = {item["key"]: set() for item in papers.values()}
        for lid, path in mirrored.items():
            for bid in self.karakeep.list_bookmark_ids(lid):
                if bid in papers:
                    wanted[papers[bid]["key"]].add(path)
        log.info(
            "mirroring %d list(s) under %s; %d of %d handed-off item(s) are in one",
            len(mirrored), sorted(scope), sum(1 for p in wanted.values() if p), len(wanted),
        )

        collections = {c["key"]: c for c in self.zotero.collections() if not c.get("deleted")}
        collection_paths = paths(collections, "parentCollection")
        by_path: dict[Path, str] = {}
        for key, path in sorted(collection_paths.items()):
            by_path.setdefault(path, key)  # duplicate names: the first one counts
        self._create_missing(needed, by_path)
        managed = {key for path, key in by_path.items() if path[0] in scope}

        failed = 0
        for item in library:
            if item["key"] not in wanted:
                continue  # not from Karakeep: never touched
            current = set(item.get("collections") or [])
            target = (current - managed) | {by_path[p] for p in wanted[item["key"]]}
            if target == current:
                continue
            log.info(
                "%s %r: %s -> %s",
                item["key"],
                (item.get("title") or "")[:60],
                self._names(current & managed, by_path) or "unfiled",
                sorted("/".join(p) for p in wanted[item["key"]]) or "unfiled",
            )
            try:
                version = self.write(
                    f"move {item['key']}",
                    self.zotero.update,
                    f"/items/{item['key']}",
                    item["version"],
                    {"collections": sorted(target)},
                )
            except Exception:
                failed += 1
                log.exception("moving %s failed; the next run retries it", item["key"])
                continue
            item.update(collections=sorted(target), version=version or item["version"])

        stale = {k for k in managed & collections.keys() if collection_paths[k] not in needed}
        self._trash_empty(stale, collections, collection_paths, library)
        return failed

    def _create_missing(self, needed: set[Path], by_path: dict[Path, str]) -> None:
        new = []
        for path in sorted(needed - by_path.keys(), key=len):  # parents first
            key = self.new_key()
            new.append(
                {
                    "key": key,
                    "version": 0,  # new, with a client-chosen key
                    "name": path[-1],
                    "parentCollection": by_path[path[:-1]] if len(path) > 1 else False,
                }
            )
            by_path[path] = key
            log.info("new collection %s as %s", "/".join(path), key)
        for i in range(0, len(new), 50):  # Zotero takes up to 50 objects per request
            batch = new[i : i + 50]
            self.write(f"create {len(batch)} collection(s)", self.zotero.create, batch, "collections")

    def _trash_empty(self, stale: set[str], collections: dict, collection_paths: dict, library: list[dict]) -> None:
        """Stale collections that hold nothing (not even items added in Zotero directly, or in
        the trash) and have no subcollections go to Zotero's trash, deepest first."""
        used = {key for item in library for key in item.get("collections") or []}
        trashed: set[str] = set()
        for key in sorted(stale, key=lambda k: -len(collection_paths[k])):
            children = {k for k, c in collections.items() if c.get("parentCollection") == key}
            if key in used or children - trashed or not self.zotero.collection_is_empty(key):
                continue
            log.info("collection %s: its list is gone and it's empty; to the trash", "/".join(collection_paths[key]))
            self.write(
                f"trash collection {key}",
                self.zotero.update,
                f"/collections/{key}",
                collections[key]["version"],
                {"deleted": True},
            )
            trashed.add(key)

    @staticmethod
    def _names(keys: set[str], by_path: dict[Path, str]) -> list[str]:
        known = {key: "/".join(path) for path, key in by_path.items()}
        return sorted(known.get(k, k) for k in keys)
