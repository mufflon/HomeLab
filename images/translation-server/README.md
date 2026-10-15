# translation-server image

We build [Zotero's translation-server](https://github.com/zotero/translation-server) ourselves:
the newest image on Docker Hub (2.0.6, January 2025) was published for **arm64 only**, and the
server is x86_64. The last amd64 image there is from 2021.

- `UPSTREAM_REF` pins the upstream commit to build. Changing it (and pushing to `main`)
  triggers `.github/workflows/translation-server.yml`, which publishes
  `ghcr.io/mufflon/translation-server:<first 12 chars of the commit>` for linux/amd64.
- Upstream's Dockerfile clones the `translators` repo itself, so the workflow initializes
  only the other submodules (`utilities`, `translate`, `zotero-schema`).
- At runtime the container `git pull`s the translators on every start, so site support stays
  current between rebuilds; rebuild for server-code changes.
- After bumping, update the image digest in `k8s/research/translation-server/deployment.yaml`.
