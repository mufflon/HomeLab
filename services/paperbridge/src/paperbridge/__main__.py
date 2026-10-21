"""Command line: one pass over Karakeep, then exit (the CronJob runs it every 15 minutes)."""

import argparse
import contextlib
import fcntl
import logging
import os
import sys

from . import __version__
from .clients import Karakeep, TranslationServer, WebDAV, Zotero, web_client
from .config import Config
from .handoff import Handoff

log = logging.getLogger("paperbridge")


def run_lock(state_path: str | None):
    """An exclusive lock beside the sync's state, held for the run; None if another run holds it.

    Two runs at once would hand the same bookmarks off twice and race on the snapshot. The
    CronJob's concurrencyPolicy: Forbid doesn't stop a manual `kubectl create job --from=cronjob/…`
    from overlapping a scheduled run; this does. The kernel drops the lock when the process ends,
    however it ends (activeDeadlineSeconds included). Without STATE_PATH there's nowhere shared to
    lock, so no lock.
    """
    if not state_path:
        return contextlib.nullcontext()
    f = open(os.path.join(os.path.dirname(state_path) or ".", "paperbridge.lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


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
    lock = run_lock(cfg.state_path)
    if lock is None:
        log.info("another paperbridge run is active; this one does nothing")
        return 0
    with lock:
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
