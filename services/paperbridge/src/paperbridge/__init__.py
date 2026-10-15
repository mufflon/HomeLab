"""paperbridge: Karakeep -> Zotero handoff (see the HomeLab repo's CLAUDE.md)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("paperbridge")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"
