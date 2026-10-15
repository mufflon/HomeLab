"""PDFs: where to get one, and the files Zotero's WebDAV sync expects (<KEY>.zip + <KEY>.prop)."""

import hashlib
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass

import httpx

from .clients import Karakeep
from .metadata import ARXIV_DOI_PREFIX, Identifiers

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class PDF:
    data: bytes
    source: str  # for the log


def looks_like_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def find_pdf(
    bookmark: dict,
    ids: Identifiers,
    karakeep: Karakeep,
    web: httpx.Client,
    unpaywall_email: str | None,
) -> PDF | None:
    """The paper's PDF: Karakeep's own copy first, then open-access copies."""
    content = bookmark.get("content") or {}
    # Only a bookmark *of* a PDF counts. A link bookmark's pdfAssetId is Karakeep's print of the
    # web page (CRAWLER_STORE_PDF), not the paper.
    if content.get("type") == "asset" and content.get("assetType") == "pdf":
        data = karakeep.asset(content["assetId"])
        if looks_like_pdf(data):
            return PDF(data, f"Karakeep asset {content['assetId']}")
        log.warning("Karakeep asset %s is not a PDF", content["assetId"])

    urls = []
    if ids.arxiv:
        urls.append(f"https://arxiv.org/pdf/{ids.arxiv}")
    if ids.doi and unpaywall_email and not ids.doi.startswith(ARXIV_DOI_PREFIX):
        urls += _unpaywall(web, ids.doi, unpaywall_email)
    for url in urls:
        data = _download(web, url)
        if data:
            return PDF(data, url)
    return None


def _unpaywall(web: httpx.Client, doi: str, email: str) -> list[str]:
    """Open-access PDF URLs for a DOI, best first."""
    try:
        response = web.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email})
        if response.status_code == 404:
            return []
        response.raise_for_status()
        record = response.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Unpaywall %s: %s", doi, e)
        return []
    locations = [record.get("best_oa_location")] + (record.get("oa_locations") or [])
    urls = [loc["url_for_pdf"] for loc in locations if loc and loc.get("url_for_pdf")]
    return list(dict.fromkeys(urls))


def _download(web: httpx.Client, url: str) -> bytes | None:
    """The file at url if it is a PDF (publishers often answer with an HTML login page)."""
    try:
        with web.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_PDF_BYTES:
                    log.warning("%s: larger than %d MB; skipped", url, MAX_PDF_BYTES >> 20)
                    return None
    except httpx.HTTPError as e:
        log.info("PDF %s: %s", url, e)
        return None
    if not looks_like_pdf(data):
        log.info("PDF %s: not a PDF", url)
        return None
    return bytes(data)


def attachment_filename(item: dict) -> str:
    """Zotero's default file name: "{firstCreator} - {year} - {title}.pdf"."""
    authors = [c for c in item.get("creators", []) if c.get("creatorType", "author") == "author"]
    names = [c.get("lastName") or c.get("name") or "" for c in authors]
    names = [n for n in names if n]
    if len(names) == 1:
        first = names[0]
    elif len(names) == 2:
        first = f"{names[0]} and {names[1]}"
    elif names:
        first = f"{names[0]} et al."
    else:
        first = ""
    year = m.group(0) if (m := re.search(r"\d{4}", item.get("date") or "")) else ""
    title = (item.get("title") or "")[:100].strip()
    stem = " - ".join(p for p in (first, year, title) if p) or "paper"
    stem = unicodedata.normalize("NFC", stem)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", stem)  # not allowed in file names somewhere
    stem = " ".join(stem.split()).rstrip(". ")
    return f"{stem}.pdf"


def zip_for_webdav(filename: str, data: bytes) -> bytes:
    """<KEY>.zip: a plain zip holding the file under its own name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(filename, data)
    return buffer.getvalue()


def prop_for_webdav(mtime_ms: int, md5: str) -> bytes:
    """<KEY>.prop: the unzipped file's mtime (ms since the epoch) and MD5."""
    return (
        f'<properties version="1"><mtime>{mtime_ms}</mtime><hash>{md5}</hash></properties>'
    ).encode()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
