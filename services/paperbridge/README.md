# paperbridge

Hands papers from Karakeep to Zotero. Every 15 minutes (a Kubernetes CronJob), for each
Karakeep bookmark tagged `paper` but not `in-zotero`, it:

1. looks up the paper's metadata with Zotero's translation-server: an arXiv ID or doi.org link
   directly, other pages through the site's translator, a bookmarked PDF through the arXiv ID
   or DOI in its text;
2. links to an existing Zotero item with the same DOI, or creates one, with
   `Karakeep: <bookmark link>` in its Extra field;
3. finds the PDF (Karakeep's own copy when the bookmark *is* the PDF, else arXiv, else
   Unpaywall) and writes it to Zotero's WebDAV folder as `<KEY>.zip` + `<KEY>.prop`, then
   creates the attachment item, so Zotero desktop downloads it like any synced file;
4. tags the bookmark `in-zotero` and adds the Zotero links to its note.

The tags and the Extra line are all its state, so a run can stop anywhere and the next one
picks up where it left off. The full design and its reasons are in the repo's
[CLAUDE.md](../../CLAUDE.md) and [README.md](../../README.md).

```bash
pip install -e '.[test]' && pytest           # tests use in-memory fakes, no network
paperbridge --dry-run                        # look everything up, change nothing
paperbridge --bookmark <id>                  # only one bookmark
```

Settings are environment variables, listed in `src/paperbridge/config.py`. In the cluster
they come from `k8s/research/paperbridge/paperbridge.env` and the `paperbridge` and
`zotero-webdav` Secrets.

Releasing: bump `version` in `pyproject.toml`, commit, tag `v<version>` and push the tag.
GitHub Actions publishes `ghcr.io/mufflon/paperbridge:<version>`; then pin its digest in
`k8s/research/paperbridge/cronjob.yaml`.
