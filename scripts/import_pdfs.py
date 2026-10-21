#!/usr/bin/env python3
"""Import local PDFs into Karakeep, the entry point; paperbridge carries them on to Zotero.

One-way and additive: nothing in Karakeep is ever deleted or moved by this script, and rerunning
it only adds what isn't there yet. The input is a manifest (JSON list), one entry per PDF:

  {"path": "/abs/file.pdf", "doi": "...", "arxiv": "...", "title": "...",
   "source_url": "https://doi.org/..." | null, "list": ["Papers", "Topic"],
   "tags": ["paper", ...], "note": "..."}

A PDF counts as already in Karakeep when any bookmark has

  1. its import ID in the note (`import-id: doi:…`, `arxiv:…` or `sha256:…`),
  2. its DOI or arXiv ID in its URL or source URL,
  3. an asset with the same file name, or
  4. the same title (20+ characters, normalized).

A bookmark this script created but didn't finish (no `paper` tag yet) is completed instead. More
than one bookmark with the same import ID is reported, never touched.

Dry run by default. Usage:
  scripts/import_pdfs.py MANIFEST.json                 # report only
  scripts/import_pdfs.py MANIFEST.json --apply --limit 50

Karakeep: KARAKEEP_PUBLIC_URL-style base URL in --url (default the homelab's), API key from .env
(KARAKEEP_IMPORT_API_KEY, else KARAKEEP_API_KEY).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "https://karakeep.home.threadsafe.se/api/v1"
PAPER_TAG = "paper"  # paperbridge's trigger; added last so a paper is complete when picked up
LIST_ICON = "📚"


def env_value(key: str) -> str:
    try:
        with open(os.path.join(REPO, ".env")) as f:
            for line in f:
                if line.startswith(key + "="):
                    return re.sub(r"\s+#.*$", "", line.split("=", 1)[1]).strip()
    except FileNotFoundError:
        pass
    return ""


def norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def arxiv_id(value: str) -> str:
    """The bare arXiv ID from an ID, `arXiv:…` or an arxiv.org link (manifests have all three)."""
    v = re.sub(r"^\s*(https?://(www\.)?arxiv\.org/(abs|pdf)/|arxiv:)", "", value, flags=re.I).strip()
    return re.sub(r"\.pdf$", "", v)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Unreachable(Exception):
    """Karakeep didn't answer (restarting, overloaded): the request may or may not have taken effect."""


class Karakeep:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, *, data: bytes | None = None,
             content_type: str = "application/json", retry: bool | None = None):
        """Retry transient failures only where repeating is harmless: GET and PUT by default.

        A POST that fails this way raises Unreachable, because it may have taken effect anyway:
        blindly repeating POST /bookmarks while Karakeep restarted created a duplicate (2026-10-21).
        401 and 429 are retried for any method: the request was turned away before it ran. Karakeep
        answers 401 whenever its API-key lookup throws, a passing database error included (a bare
        `catch` in createContextFromRequest, v0.33.2); that stopped an import once (2026-10-21).
        """
        if retry is None:
            retry = method in ("GET", "PUT")
        if body is not None:
            data = json.dumps(body).encode()
        for attempt in range(6):
            if attempt:
                time.sleep(min(4 * 2 ** (attempt - 1), 60))  # 4 s ... 60 s: about 2 min, a restart
            req = urllib.request.Request(self.base + path, method=method, data=data,
                                         headers={"Authorization": f"Bearer {self.key}",
                                                  "Content-Type": content_type})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code in (401, 429):
                    err, safe = f"HTTP {e.code}", True
                elif e.code in (502, 503, 504):
                    err, safe = f"HTTP {e.code}", retry
                else:
                    raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                err, safe = str(getattr(e, "reason", e)), retry
            more = safe and attempt < 5
            print(f"  {method} {path}: {err}{'; retrying' if more else ''}", file=sys.stderr)
            if not more:
                break
        raise Unreachable(f"{method} {path}: {err}")

    def bookmarks(self) -> list[dict]:
        out = []
        for archived in ("false", "true"):
            cursor = None
            while True:
                q = {"limit": "100", "archived": archived, "includeContent": "false"}
                if cursor:
                    q["cursor"] = cursor
                page = self.call("GET", "/bookmarks?" + urllib.parse.urlencode(q))
                out += page.get("bookmarks", [])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        return out

    def lists(self) -> list[dict]:
        return self.call("GET", "/lists")["lists"]

    def upload(self, path: str) -> dict:
        boundary = uuid.uuid4().hex
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/pdf"
        with open(path, "rb") as f:
            payload = f.read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        # Repeating an upload at worst leaves an extra asset that no bookmark uses.
        return self.call("POST", "/assets", data=body, retry=True,
                         content_type=f"multipart/form-data; boundary={boundary}")


def tag_names(b: dict) -> set[str]:
    return {t["name"] for t in b.get("tags", [])}


def bookmark_refs(b: dict) -> str:
    """Everything identifying a bookmark, lowercased, for substring matches."""
    c = b.get("content") or {}
    return " ".join(str(x) for x in (c.get("url"), c.get("sourceUrl"), c.get("fileName"), b.get("note")) if x).lower()


class Index:
    """What Karakeep already holds, for the duplicate checks."""

    def __init__(self, bookmarks: list[dict]):
        self.by_import_id: dict[str, list[dict]] = {}
        self.refs: list[tuple[str, dict]] = []
        self.by_file: dict[str, dict] = {}
        self.by_title: dict[str, dict] = {}
        for b in bookmarks:
            for m in re.finditer(r"import-id:\s*(\S+)", b.get("note") or ""):
                self.by_import_id.setdefault(m.group(1).lower(), []).append(b)
            self.refs.append((bookmark_refs(b), b))
            name = ((b.get("content") or {}).get("fileName") or "").lower()
            if name:
                self.by_file[name] = b
            t = norm_title(b.get("title") or "")
            if len(t) >= 20:
                self.by_title[t] = b

    def match(self, import_id: str, doi: str, arxiv: str, filename: str, title: str) -> tuple[str, list[dict]]:
        """Why the PDF is already in Karakeep, and the bookmarks holding it (all of them for an
        import ID, which can be more than one after a failure); ("", []) when it isn't."""
        if import_id.lower() in self.by_import_id:
            return "import-id", self.by_import_id[import_id.lower()]
        # arXiv's own DOIs (10.48550/arXiv.<id>) name the same paper as its arxiv.org links.
        m = re.match(r"10\.48550/arxiv\.(\S+)", doi, re.I)
        arxiv = arxiv or (m.group(1) if m else "")
        for needle, why in ((doi.lower(), "DOI"), (arxiv.lower(), "arXiv ID")):
            if needle:
                for refs, b in self.refs:
                    if needle in refs or urllib.parse.quote(needle, safe="/") in refs:
                        return why, [b]
        if filename.lower() in self.by_file:
            return "file name", [self.by_file[filename.lower()]]
        t = norm_title(title)
        if len(t) >= 20 and t in self.by_title:
            return "title", [self.by_title[t]]
        return "", []

    def add(self, import_id: str, b: dict):
        self.by_import_id.setdefault(import_id.lower(), []).append(b)
        self.refs.append((bookmark_refs(b), b))


def create_bookmark(kk: Karakeep, body: dict, import_id: str) -> dict:
    """POST the bookmark once; if Karakeep went away meanwhile, look for it before posting again."""
    try:
        return kk.call("POST", "/bookmarks", body)
    except Unreachable:
        found = Index(kk.bookmarks()).by_import_id.get(import_id.lower())  # GETs wait for Karakeep
        if found:
            return found[0]
        return kk.call("POST", "/bookmarks", body)


def ensure_list(kk: Karakeep, lists: list[dict], path: list[str], apply: bool) -> str | None:
    parent = None
    for name in path:
        found = next((l for l in lists if l["name"] == name and l.get("parentId") == parent
                      and l.get("type", "manual") == "manual"), None)
        if not found:
            if not apply:
                return None
            found = kk.call("POST", "/lists", {"name": name, "icon": LIST_ICON, "type": "manual",
                                               **({"parentId": parent} if parent else {})})
            lists.append(found)
            print(f"  created list {'/'.join(path[:path.index(name) + 1])}")
        parent = found["id"]
    return parent


def finish(kk: Karakeep, lists: list[dict], b: dict, e: dict) -> None:
    """List membership first, tags last (paper last of all), so paperbridge sees it complete."""
    list_id = ensure_list(kk, lists, e["list"], True)
    kk.call("PUT", f"/lists/{list_id}/bookmarks/{b['id']}")
    others = [t for t in e["tags"] if t != PAPER_TAG]
    # Attaching a tag twice is harmless, so these POSTs may be retried.
    if others:
        kk.call("POST", f"/bookmarks/{b['id']}/tags", {"tags": [{"tagName": t} for t in others]}, retry=True)
    kk.call("POST", f"/bookmarks/{b['id']}/tags", {"tags": [{"tagName": PAPER_TAG}]}, retry=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("--apply", action="store_true", help="upload; without it, only report")
    ap.add_argument("--limit", type=int, default=0, help="import at most N new PDFs this run")
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()

    key = env_value("KARAKEEP_IMPORT_API_KEY") or env_value("KARAKEEP_API_KEY")
    if not key:
        raise SystemExit("no Karakeep API key in .env (KARAKEEP_IMPORT_API_KEY or KARAKEEP_API_KEY)")
    kk = Karakeep(args.url, key)
    with open(args.manifest) as f:
        manifest = json.load(f)

    print("reading Karakeep ...")
    index, lists = Index(kk.bookmarks()), kk.lists()
    seen_hashes: dict[str, str] = {}
    counts = {"new": 0, "present": 0, "completed": 0, "same file twice": 0, "extra bookmarks": 0}
    imported = 0
    stopped = ""

    try:
        for e in manifest:
            path, name = e["path"], os.path.basename(e["path"])
            digest = sha256(path)
            if digest in seen_hashes:
                counts["same file twice"] += 1
                print(f"  skip  {name}: identical to {seen_hashes[digest]}")
                continue
            seen_hashes[digest] = name
            arxiv = arxiv_id(e.get("arxiv") or "")
            import_id = (f"doi:{e['doi']}" if e.get("doi") else f"arxiv:{arxiv}" if arxiv
                         else f"sha256:{digest}").lower()
            why, found = index.match(import_id, e.get("doi", ""), arxiv, name, e.get("title", ""))
            if found:
                finished = [b for b in found if PAPER_TAG in tag_names(b)]
                keep = finished[0] if finished else found[-1]  # newest first: [-1] is the oldest
                if why == "import-id" and not finished:
                    counts["completed"] += 1
                    print(f"  {'finish' if args.apply else 'would finish'}  {name} (created earlier, not finished)")
                    if args.apply:
                        finish(kk, lists, keep, e)
                else:
                    counts["present"] += 1
                extra = [b["id"] for b in found if b is not keep]
                if extra:  # never touched here: delete them in Karakeep by hand
                    counts["extra bookmarks"] += len(extra)
                    print(f"  NOTE  {name}: extra bookmark(s) {', '.join(extra)} besides {keep['id']}")
                continue

            counts["new"] += 1
            if not args.apply:
                continue
            if args.limit and imported >= args.limit:
                continue
            asset = kk.upload(path)
            body = {"type": "asset", "assetType": "pdf", "assetId": asset["assetId"], "fileName": name,
                    "note": (e.get("note", "").rstrip() + f"\nimport-id: {import_id}").strip()}
            if e.get("source_url"):
                body["sourceUrl"] = e["source_url"]
            if e.get("title"):
                body["title"] = e["title"][:1000]
            b = create_bookmark(kk, body, import_id)
            index.add(import_id, b)
            finish(kk, lists, b, e)
            imported += 1
            print(f"  added {name} -> {'/'.join(e['list'])}")
    except Unreachable as err:
        stopped = str(err)

    mode = "imported" if args.apply else "would import"
    print(f"\n{len(manifest)} PDFs: {counts['new']} new ({mode} {imported if args.apply else counts['new']}), "
          f"{counts['present']} already in Karakeep, {counts['completed']} to finish, "
          f"{counts['same file twice']} identical duplicates on disk, "
          f"{counts['extra bookmarks']} extra bookmarks in Karakeep")
    if stopped:
        hint = " (401 on every try: check the API key)" if "HTTP 401" in stopped else ""
        raise SystemExit(f"stopped: {stopped}{hint}\nRerun to continue: a half-done PDF is finished, not added again.")
    if not args.apply:
        missing = sorted({"/".join(e["list"][:i + 1]) for e in manifest for i in range(len(e["list"]))
                          if ensure_list(kk, lists, e["list"][:i + 1], False) is None})
        if missing:
            print("lists to create:", ", ".join(missing))


if __name__ == "__main__":
    main()
