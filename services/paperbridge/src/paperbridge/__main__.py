"""Command line: one pass over Karakeep, then exit (the CronJob runs it every 15 minutes)."""

import argparse
import logging
import sys

from . import __version__
from .clients import Karakeep, TranslationServer, WebDAV, Zotero, web_client
from .config import Config
from .handoff import Handoff

log = logging.getLogger("paperbridge")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paperbridge",
        description="Hand Karakeep bookmarks tagged `paper` over to Zotero. Settings come from "
        "environment variables (see config.py).",
    )
    parser.add_argument("--dry-run", action="store_true", help="look everything up, change nothing")
    parser.add_argument("--bookmark", metavar="ID", help="only this Karakeep bookmark")
    parser.add_argument("-v", "--verbose", action="store_true", help="also log every HTTP request")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)

    cfg = Config.from_env()
    log.info("paperbridge %s%s", __version__, " (dry run)" if args.dry_run else "")
    handoff = Handoff(
        cfg,
        Karakeep(cfg.karakeep_url, cfg.karakeep_api_key),
        Zotero(cfg.zotero_api_url, cfg.zotero_user_id, cfg.zotero_api_key),
        WebDAV(cfg.webdav_url, cfg.webdav_username, cfg.webdav_password),
        TranslationServer(cfg.translation_server_url),
        web_client(),
        dry_run=args.dry_run,
    )
    failed = handoff.run(only=args.bookmark)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
