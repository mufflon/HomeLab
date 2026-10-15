"""Settings, read from the environment: a ConfigMap (URLs, tags) and Secrets (keys) in the cluster."""

import os
from dataclasses import dataclass

REQUIRED = (
    "KARAKEEP_URL",
    "KARAKEEP_PUBLIC_URL",
    "KARAKEEP_API_KEY",
    "TRANSLATION_SERVER_URL",
    "ZOTERO_API_URL",
    "ZOTERO_USER_ID",
    "ZOTERO_API_KEY",
    "WEBDAV_URL",
    "WEBDAV_USERNAME",
    "WEBDAV_PASSWORD",
)


@dataclass(frozen=True)
class Config:
    karakeep_url: str  # API base, in-cluster
    karakeep_public_url: str  # what links in Zotero point at
    karakeep_api_key: str
    translation_server_url: str
    zotero_api_url: str
    zotero_user_id: str
    zotero_api_key: str
    webdav_url: str  # Zotero's folder on the WebDAV server, ending in zotero/
    webdav_username: str
    webdav_password: str
    unpaywall_email: str | None = None  # optional: open-access PDF lookup by DOI
    paper_tag: str = "paper"
    done_tag: str = "in-zotero"
    # How long to keep retrying a bookmark whose metadata can't be found yet (the page may still be
    # crawling, or a site may be down) before creating a minimal item tagged needs-metadata.
    metadata_grace_hours: float = 24.0

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise SystemExit("missing settings: " + ", ".join(missing))
        return cls(
            karakeep_url=env["KARAKEEP_URL"].rstrip("/"),
            karakeep_public_url=env["KARAKEEP_PUBLIC_URL"].rstrip("/"),
            karakeep_api_key=env["KARAKEEP_API_KEY"],
            translation_server_url=env["TRANSLATION_SERVER_URL"].rstrip("/"),
            zotero_api_url=env["ZOTERO_API_URL"].rstrip("/"),
            zotero_user_id=env["ZOTERO_USER_ID"],
            zotero_api_key=env["ZOTERO_API_KEY"],
            webdav_url=env["WEBDAV_URL"].rstrip("/") + "/",
            webdav_username=env["WEBDAV_USERNAME"],
            webdav_password=env["WEBDAV_PASSWORD"],
            unpaywall_email=env.get("UNPAYWALL_EMAIL") or None,
            paper_tag=env.get("PAPER_TAG") or cls.paper_tag,
            done_tag=env.get("DONE_TAG") or cls.done_tag,
            metadata_grace_hours=float(env.get("METADATA_GRACE_HOURS") or cls.metadata_grace_hours),
        )
