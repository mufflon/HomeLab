# CLAUDE.md

## What this is

Configuration for a single home server, `minisforum`, and the services it
runs in Kubernetes. The first stack is a research pipeline:

- **Karakeep** is the register. Everything gets bookmarked there first:
  articles, pages, papers.
- **Zotero** is where papers are read and annotated. Library data
  (metadata, notes, annotations) syncs through zotero.org; PDF files sync
  through our own **WebDAV** server, not Zotero's storage.
- **paperbridge** (working name) is the one glue service between the two.
  It hands papers from Karakeep to Zotero and syncs reading status back,
  so Karakeep stays the register.

Everything is reachable **only over Tailscale**. Nothing is exposed on the
LAN or the internet.

## Status

2026-10-15: build step 0 is half done.

- **Host part: done.** `host/00-prep.sh` has run on the server.
  - `hddpool/k8s` and its children `zotero-webdav`, `karakeep` and
    `meilisearch` exist with `lz4` compression and `atime=off`, owned as
    listed under Storage.
  - `zotero-webdav/zotero/` exists.
  - sanoid has taken the first snapshots of `zotero-webdav` and
    `karakeep`, and `sanoid.timer` is active.
- **Tailscale admin-console part: not done** (owner's task).
- Nothing is deployed: the cluster is a fresh, empty, healthy MicroK8s 1.36.

## Hard rules

- **This repo is public.** Never commit secrets, Tailscale auth keys or
  OAuth credentials, API keys, the tailnet name, or kubeconfigs (a MicroK8s
  kubeconfig is a cluster-admin credential). Secrets live in a gitignored
  `.env` on the operator's machine. `scripts/secrets.sh` turns them into
  Kubernetes Secrets with `kubectl create secret … --dry-run=client -o yaml
  | kubectl apply -f -`. `.env.example` lists the names, never values.
- **No exposure outside the tailnet.** Services are `ClusterIP`. The only
  way in is a Tailscale ingress. No `NodePort`, no `LoadBalancer`, no
  router port forwards. Internal-only services (translation-server,
  Meilisearch, Chrome) get no ingress at all.
- **Pinned images, newest releases.** No `:latest`. Bump deliberately and
  note why in the commit.
- **Propose before git.** Commits and pushes happen when the owner says so.

## The server

Facts that matter when working here, verified 2026-10-15.

- Minisforum AI-series: Ryzen AI 9 HX 470, Radeon 890M. 4 TB NVMe (ext4 on
  LVM, `/`) plus 4 × 4 TB HDD in ZFS raidz1 pool `hddpool` (~10 TB usable,
  scrubs clean).
- Ubuntu 26.04.1 LTS, kernel 7.0 (upgraded from 24.04 on 2026-10-15).
  `sudo` is **sudo-rs**: no `-E`. MicroK8s wrappers print a warning about
  it. Harmless.
- MicroK8s `1.36/stable`, classic snap, single node. Addons: `dns`,
  `ha-cluster` (Calico), `helm`. Reinstalled clean on 2026-10-15. Calico and
  kube-proxy use **iptables-legacy**. That is MicroK8s's own default, not a
  fault. Leave it.
- Outside Kubernetes on the host: Jellyfin (apt, VA-API transcoding on
  `/dev/dri/renderD128`), NFS export of `hddpool/jellyfin` to the LAN,
  Livepatch, ESM. Not managed by this repo.
- **Power-loss history.** The NVMe reports 11 unsafe shutdowns out of 21
  power cycles. Normal reboots are clean (verified). A hard power-off once
  left a containerd image layer truncated: `/pause` was 0 bytes, and every
  pod sandbox died with `exec format error` for 200 days. Lessons:
  - Durable data goes on ZFS (copy-on-write, so no zero-length files after
    power loss), never on the ext4 root.
  - If containers fail with `exec format error`, suspect a truncated image
    layer: remove the image and pull it again.
  - A UPS would remove the root cause.

## Architecture

### Access: Tailscale Kubernetes operator

The Tailscale operator (Helm chart `tailscale/tailscale-operator`, pinned)
runs in namespace `tailscale`. Every service we expose is an `Ingress`
with `ingressClassName: tailscale`. It becomes its own tailnet device
`<name>.<tailnet>.ts.net` with a valid HTTPS certificate. Manifests only
contain the short name (`tls.hosts: [karakeep]`), never the tailnet name.
Tailnet requirements: MagicDNS and HTTPS enabled, an OAuth client for the
operator, tags `tag:k8s-operator` and `tag:k8s`, and ACLs that admit only
the owner's devices.

Remote `kubectl` also goes through the operator's API server proxy (`tailscale
configure kubeconfig …`). Kubeconfigs stay on the operator's machine.

### Storage: ZFS datasets as local PersistentVolumes

All Kubernetes storage lives in ZFS on `hddpool`, never on the NVMe. A
dedicated parent dataset, `hddpool/k8s`, exists only for the cluster:

- Properties are set on the parent and inherited by the children:
  `compression=lz4`, `atime=off`.
- There is one child dataset per app, so each one can be snapshotted,
  rolled back and given a quota on its own.
- These are datasets (filesystems), not zvols. A zvol would mean a second
  filesystem inside ZFS and would lose per-app snapshots.

The child datasets are created once on the host with sudo:

| Dataset | Used by | Snapshots |
|---|---|---|
| `hddpool/k8s/zotero-webdav` | WebDAV server | yes |
| `hddpool/k8s/karakeep` | Karakeep `DATA_DIR` (SQLite + assets) | yes |
| `hddpool/k8s/meilisearch` | search index | no (rebuildable from Karakeep) |

Each one is a `local` PersistentVolume (StorageClass `zfs-local`,
`kubernetes.io/no-provisioner`, `WaitForFirstConsumer`, node affinity to
`minisforum`), bound by name from the app's PVC.

`host/00-prep.sh` owns the datasets, and it sets each dataset's owner to
the UID its app runs as. The manifests must set the same
`runAsUser`/`runAsGroup`:

- **zotero-webdav:** 10001. The image runs as root by default; we run it
  as non-root.
- **meilisearch:** 10002. Same.
- **karakeep:** root. Its s6-overlay `/init` needs root.

Image users were read from the registry configs on 2026-10-15.

Snapshots are taken on the host by sanoid (`sanoid.timer`, every 15
minutes): hourly ×36, daily ×30, monthly ×6. Kubernetes knows nothing
about them. Restore by `zfs rollback`, or by copying out of
`<mountpoint>/.zfs/snapshot/`.

### Components

All in namespace `research`.

| Component | Image | Exposed as | Storage |
|---|---|---|---|
| zotero-webdav | `ghcr.io/hacdias/webdav` | Tailscale ingress `zotero-dav` | `zotero-webdav` |
| translation-server | `zotero/translation-server` (pin by digest) | ClusterIP only | none |
| karakeep | `ghcr.io/karakeep-app/karakeep` | Tailscale ingress `karakeep` | `karakeep` |
| meilisearch | `getmeili/meilisearch:v1.41.0`, the version Karakeep's own manifests pin (newest upstream is v1.55) | ClusterIP only | `meilisearch` |
| chrome | `ghcr.io/karakeep-app/karakeep-chrome` (runs as 65534) | ClusterIP only | none |
| paperbridge | built from `services/paperbridge`, published to ghcr.io | CronJob, no ingress | small state |

**Zotero WebDAV.** One user, basic auth, HTTPS through the Tailscale
ingress. Zotero desktop: Settings → Sync → File Syncing → WebDAV, URL
`https://zotero-dav.<tailnet>.ts.net/`. Zotero appends `zotero/` itself,
and that directory must exist. Zotero on mobile supports WebDAV too, but
the device needs Tailscale.

### paperbridge: Karakeep → Zotero → Karakeep

A Python CronJob, every 15 minutes. It must be idempotent: tags and
Zotero's `Extra` field carry all state, plus the last-seen Zotero library
version for incremental sync.

**Handoff, for each bookmark tagged `paper` but not `in-zotero`:**

1. **Metadata.** Ask translation-server (`/web` for a URL, `/search` for a
   DOI, arXiv ID or ISBN). It returns a complete Zotero item. It is the
   same engine the Zotero browser connector uses.
2. **Dedup.** If a Zotero item with the same DOI exists, link to it instead
   of creating a new one.
3. **Create** the item via the Zotero web API, with `Karakeep: <url>` in
   `Extra`.
4. **PDF.** Create a child attachment item via the API
   (`linkMode: imported_file`), then write `<attachmentKey>.zip` (the PDF,
   zipped) and `<attachmentKey>.prop` into the WebDAV `zotero/` directory.
   The web API cannot upload to WebDAV: Zotero's developer says
   WebDAV-stored files are meant for desktop clients, not the API or web
   library (forums.zotero.org/discussion/comment/393726). The format is
   undocumented but simple. Reference implementation: `zotkit` on PyPI.
5. **Back in Karakeep:** tag `in-zotero`, and add the Zotero item link to
   the bookmark's note.

Papers get the `paper` tag manually, or from a Karakeep rule matching
doi.org / arxiv.org URLs.

**Sync back.**

- **Read status:** when the Zotero Reading List plugin marks a paper Read,
  archive the bookmark in Karakeep and add a read-date tag. This replaces
  the separate read-date CronJob idea.
- **Highlights (later):** copy Zotero annotations and comments into the
  bookmark's note, so a Karakeep search also finds highlighted passages.

**Edge cases and intent.**

- **No PDF on the bookmark** (only the landing page was saved): try an
  open-access PDF. Otherwise leave it to "Find Available PDF" in Zotero
  desktop.
- **Two copies of each PDF is on purpose.** Highlights live in Zotero's
  database. Karakeep's copy lets Claude read the full paper through
  Karakeep's MCP server. The Zotero MCP server in web-API mode cannot reach
  WebDAV files.
- **Later, same service:** EPUB/OPDS.

## Build order

Each step is done when it's deployed **and** verified from a tailnet
device. Record that in "Status".

0. **Host prep.**
   - `host/00-prep.sh` (idempotent): the parent dataset `hddpool/k8s`,
     the per-app child datasets and their ownership, and the sanoid policy
     plus timer.
   - In the Tailscale admin console: MagicDNS, HTTPS, the OAuth client,
     tags and ACLs.
1. **Tailscale operator**, verified with a throwaway ingress.
2. **zotero-webdav**, verified with Zotero desktop's "Verify Server" and a
   real attachment round trip.
3. **translation-server**, smoke-tested with one DOI and one arXiv ID.
4. **Karakeep + Meilisearch + Chrome.** First login, then create the API
   key for paperbridge.
5. **paperbridge v0** (handoff). GitHub Actions builds the image on `v*`
   tags; the version lives only in `pyproject.toml`.
6. **paperbridge v1** (read-status sync back), then v2 (highlights).

## Repo layout (target)

```
CLAUDE.md
.env.example              # secret names only
.gitignore                # .env, kubeconfig*, *.secret.yaml
host/                     # one-off host scripts: zfs datasets, sanoid
k8s/
  tailscale-operator/     # pinned chart version + values
  research/
    kustomization.yaml    # namespace, storage, and the apps below
    storage.yaml          # StorageClass zfs-local + PVs
    zotero-webdav/
    translation-server/
    karakeep/
    paperbridge/
scripts/secrets.sh        # .env → Kubernetes Secrets
services/paperbridge/     # Python package + Dockerfile
.github/workflows/
```

Kustomize, applied by hand: `kubectl apply -k k8s/research`. Helm only
for the Tailscale operator. No Flux until there are enough services to
justify it.

## Working on the server

- SSH as `mufflon`, key auth. Define a local alias `minisforum` in
  `~/.ssh/config`; the address is deliberately not in this repo.
  `mufflon` is in the `microk8s` group, so `microk8s kubectl` needs no
  sudo.
- **Root needs the owner's password. Never ask for it in chat.**
  - Batch root steps into one script and copy it over.
  - Run it in a terminal tab with
    `ssh -t minisforum 'sudo bash ./x.sh 2>&1 | tee x.log'`, so the owner
    types the password there.
  - Read `x.log` afterwards. Tabs get closed, so the log is what counts.
  - Type the password promptly: sudo-rs times out the prompt after a few
    minutes.
- **sudo-rs + `| tee` stops anything that touches the terminal.** The
  command doesn't own the terminal, so apt gets SIGTTOU and hangs in state
  `T` after it has finished installing. In scripts, give apt (and anything
  else interactive) `</dev/null`.
- Package upgrades restart networking: a short SSH drop is not a reboot.
  Detect reboots by `/proc/sys/kernel/random/boot_id`, not by
  reachability.

## Verify before relying on

- Karakeep rule engine: can a rule match on URL host (doi.org, arxiv.org)?
- Zotero's `.prop` format, and whether desktop picks up a file paperbridge
  wrote directly. Test with a real library before automating.
- Zotero web API dedup by DOI: does quick search with `qmode=everything`
  match the DOI field, or do we need a local DOI index?
- Where the Reading List plugin stores status (`Extra`, a tag, or
  something else).
- `hacdias/webdav` against Zotero (PROPFIND, missing-file 404s, `.prop`
  handling).
- Meilisearch: stay on Karakeep's pinned v1.41.0, or verify a newer server
  with Karakeep's client first.
