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

Services are reachable **on the home LAN only**, under
`*.home.threadsafe.se`, over HTTPS with a Let's Encrypt wildcard
certificate. Remote access is the **router's VPN**, which puts a client on
the LAN like any local machine. There is **no VPN on the server** (no
Tailscale, no WireGuard). Nothing is port-forwarded, so every exposed
service must authenticate on its own.

## Status

2026-10-16:

- **Step 0, host part: done.** `host/00-prep.sh` has run.
  - All four datasets (`zotero-webdav`, `karakeep`, `meilisearch`,
    `certs`) exist, owned as listed under Storage.
  - sanoid snapshots are being taken and its timer is active.
- **Step 0, owner tasks: not done.** These are the Loopia API user (the
  rights are known now, see Build order), the `*.home.threadsafe.se` A
  record (still Loopia's catch-all `194.9.94.86`), the router check and
  `.env`.
- **Step 1, host part: done.** `host/01-microk8s.sh` has run. Traefik 3.6.13
  is live on the server's ports 80/443. Verified from a LAN machine:
  - HTTP → `301` to HTTPS;
  - the dashboard and its API return `404`, even with
    `Host: dashboard.localhost`;
  - the default TLSStore points at `ingress/wildcard-home-tls`. That Secret
    doesn't exist yet, so Traefik serves its self-signed placeholder.
- **Step 1, remaining:** `scripts/secrets.sh`, `k8s/ingress/` (lego),
  the root `k8s/kustomization.yaml`, and the first certificate. These
  wait for the owner tasks.
- **Outside the repo:** Jellyfin also has HEVC and AV1 encoding enabled
  (the backup is `/etc/jellyfin/encoding.xml.bak-pre-hevc-av1`).
- An earlier design used Tailscale. It was dropped on 2026-10-15 because
  the owner already has VPN on the router.

## Hard rules

- **The repo is the rebuild recipe.** Anyone with this repo, the
  prerequisites in README.md and a filled-in `.env` must be able to
  rebuild everything. Every change to the server or cluster lands in a
  file here, in the same commit:
  - host steps as idempotent scripts in `host/`;
  - cluster objects as manifests under `k8s/`, applied with one
    `kubectl apply -k k8s/`;
  - manual and owner steps in README.md's prerequisites or rebuild
    steps.

  No ad-hoc `kubectl edit`/`create`/`patch` that isn't captured. If a fix
  is made by hand while debugging, the file follows in the same change.
  README's "Rebuild" section must always match reality, including its
  *(planned)* markers.
- **README.md is for humans; CLAUDE.md is the working spec.**
  - The README explains purpose, how the pieces fit (including its
    Mermaid diagram), and the *why* behind design choices. It is written
    for a curious stranger, or the owner years later.
  - When a design decision or component changes, update the README's
    explanation and diagram in the same change.
  - Keep exact versions, UIDs and gotchas in CLAUDE.md, not in the README.
- **This repo is public.** Never commit:
  - secrets, API keys, or Loopia API credentials;
  - the ACME account email;
  - kubeconfigs (a MicroK8s kubeconfig is a cluster-admin credential).

  Secrets live in a gitignored `.env` on the operator's machine.
  `scripts/secrets.sh` turns them into Kubernetes Secrets with
  `kubectl create secret … --dry-run=client -o yaml | kubectl apply -f -`.
  `.env.example` lists the names, never values. The domain
  `home.threadsafe.se` and the fact that it points at a private IP are
  public anyway (DNS and Certificate Transparency), so they may be
  committed.
- **LAN only, and everything authenticates.**
  - Services are `ClusterIP`. The only way in is the MicroK8s Traefik
    ingress on ports 80/443 of the server, HTTPS only.
  - No `NodePort`, no `LoadBalancer`, no router port forwards.
  - Internal-only services (translation-server, Meilisearch, Chrome) get
    no ingress at all.
  - Every exposed service requires its own login with a strong password
    or key. Nothing is anonymous "because it's only the LAN".
- **Pinned images, newest releases.** No `:latest`. Bump deliberately and
  note why in the commit.
- **Git:** the owner allows committing and pushing freely. Scan the staged
  diff for credentials, IPs and personal email before every push.

## The server

Facts that matter when working here, verified 2026-10-15.

- Minisforum AI-series: Ryzen AI 9 HX 470, Radeon 890M. 4 TB NVMe (ext4 on
  LVM, `/`) plus 4 × 4 TB HDD in ZFS raidz1 pool `hddpool` (~10 TB usable,
  scrubs clean).
- **The four HDDs are in a USB enclosure** (JMicron JMS567 bridge, `uas`,
  one USB 3 link). Every disk reports the same fake serial
  `0123456789ABCDEF`, which is why the pool uses `sdX` names rather than
  by-id paths. ZFS imports by its own labels, so that's harmless.
  - The enclosure is a single point of failure. If it drops off the bus,
    the pool suspends until it's reconnected.
  - SMART pass-through is unverified.
  - No USB resets were seen as of 2026-10-16. Still, ZFS is a better place
    for data than the NVMe, which has no redundancy and no checksums.
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
  - There **is** a UPS, but it doesn't signal the server (no USB link, no
    NUT or apcupsd, checked 2026-10-16). It bridges short outages, but a
    long one still ends in a hard power-off. Connecting it and running NUT
    would make low-battery shutdowns clean. Not done yet.

## Architecture

### Access: LAN ingress with a wildcard certificate

- **Ingress.** The MicroK8s `ingress` addon is **Traefik** (chart 39.0.8,
  Traefik v3.6), not ingress-nginx, which upstream retired in 2026. It runs
  as a DaemonSet on host ports 80/443. `host/01-microk8s.sh` enables it
  with `--default-ssl-certificate ingress/wildcard-home-tls`, which creates
  Traefik's default `TLSStore`. The script then hardens it through a Helm
  upgrade:
  - The addon's values create an **unauthenticated dashboard** route on
    `Host(dashboard.localhost)`. The script disables it.
  - The script adds a **web → websecure** redirect:
    `ports.web.http.redirections.entryPoint.*`. In chart 39 the redirect
    sits under `http`. The older `ports.web.redirections` fails schema
    validation.
  - The script switches off `api.dashboard`.
  - The script makes rollouts update in place (`maxSurge 0`,
    `maxUnavailable 1`). The chart's default surge rollout deadlocks on a
    single node, because the new pod needs host ports 80/443 that the old
    pod still holds. Ingress therefore blips for a few seconds per update.
- **Writing Ingresses.** Use `ingressClassName: public` (the addon's
  default class). Put a `tls: [{hosts: [<name>.home.threadsafe.se]}]`
  entry **without** `secretName`, so Traefik serves the default wildcard
  certificate.
- **Names.** A public wildcard A record, `*.home.threadsafe.se`, points at
  the server's LAN IP. It is set at Loopia, so it resolves the same at
  home and over the router VPN, and it is useless from outside.
  - Routers with "DNS rebind protection" block public names that point to
    private IPs. If names don't resolve, allow `home.threadsafe.se` there.
  - The wildcard keeps individual service names out of the public
    Certificate Transparency logs.
- **Certificates.** A CronJob in namespace `ingress` runs daily:
  1. An init container `goacme/lego` (pinned) gets the certificate from
     Let's Encrypt through a **DNS-01** challenge via LoopiaAPI. lego has
     a built-in `loopia` provider. Let's Encrypt only checks a temporary
     TXT record in public DNS and never connects to the server. The first
     run uses `run`; later runs use `renew --days 30`.
  2. The main container, `alpine/kubectl` pinned to the cluster's minor
     version, writes the certificate into the Secret
     `ingress/wildcard-home-tls`. Its ServiceAccount may only update that
     one Secret.

  lego's account key and certificate live on the `certs` dataset, so runs
  don't re-issue and hit Let's Encrypt rate limits. Use the Let's Encrypt
  **staging** server until the flow works.

  cert-manager was rejected: its only Loopia webhook was last released in
  2021, on cert-manager v1.2.
- **Changing domain.** To move to `wallindevelopment.se`, also at Loopia,
  replace `home.threadsafe.se` in the manifests and create a Loopia API
  user for that domain.
- **Remote kubectl.** Use a kubeconfig from `microk8s config`, kept on the
  operator's machine. The API server listens on the LAN at `:16443`.

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
| `hddpool/k8s/certs` | lego account + certificates (private keys, mode 0700) | yes |

Each one is a `local` PersistentVolume (StorageClass `zfs-local`,
`kubernetes.io/no-provisioner`, `WaitForFirstConsumer`, node affinity to
`minisforum`), bound by name from the app's PVC.

`host/00-prep.sh` owns the datasets, and it sets each dataset's owner to
the UID its app runs as. The manifests must set the same
`runAsUser`/`runAsGroup`:

- **zotero-webdav:** 10001. The image runs as root by default; we run it
  as non-root.
- **meilisearch:** 10002. Same.
- **certs:** 10003, for lego. Same.
- **karakeep:** root. Its s6-overlay `/init` needs root.

Image users were read from the registry configs on 2026-10-15.

Snapshots are taken on the host by sanoid (`sanoid.timer`, every 15
minutes): hourly ×36, daily ×30, monthly ×6. Kubernetes knows nothing
about them. Restore by `zfs rollback`, or by copying out of
`<mountpoint>/.zfs/snapshot/`.

### Components

The research apps are in namespace `research`; ingress and certificates
are in `ingress`.

| Component | Image | Exposed as | Storage |
|---|---|---|---|
| lego (CronJob) | `goacme/lego` + `alpine/kubectl` | none | `certs` |
| zotero-webdav | `ghcr.io/hacdias/webdav` | `zotero-dav.home.threadsafe.se` | `zotero-webdav` |
| translation-server | `zotero/translation-server` (pin by digest) | ClusterIP only | none |
| karakeep | `ghcr.io/karakeep-app/karakeep` | `karakeep.home.threadsafe.se` | `karakeep` |
| meilisearch | `getmeili/meilisearch:v1.41.0`, the version Karakeep's own manifests pin (newest upstream is v1.55) | ClusterIP only | `meilisearch` |
| chrome | `ghcr.io/karakeep-app/karakeep-chrome` (runs as 65534) | ClusterIP only | none |
| paperbridge | built from `services/paperbridge`, published to ghcr.io | CronJob, no ingress | small state |

**Zotero WebDAV.** One user, basic auth with a bcrypt-hashed password,
HTTPS only. Zotero desktop: Settings → Sync → File Syncing → WebDAV, URL
`https://zotero-dav.home.threadsafe.se/`. Zotero appends `zotero/` itself,
and that directory already exists. Zotero on mobile works at home, or over
the router VPN when away.

**Karakeep.** Its own login. Set `DISABLE_SIGNUPS=true` once the owner's
account exists. `NEXTAUTH_SECRET` and `MEILI_MASTER_KEY` come from `.env`.

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

Each step is done when it's deployed **and** verified from a LAN device
(and once over the router VPN). Record that in "Status".

0. **Host prep.**
   - `host/00-prep.sh` (idempotent): the parent dataset `hddpool/k8s`,
     the per-app child datasets and their ownership, and the sanoid policy
     plus timer.
   - Owner tasks:
     - In Loopia Customer Zone → LoopiaAPI, create an API user with only
       `getZoneRecords`, `addZoneRecord`, `removeZoneRecord` and
       `removeSubdomain`. These are exactly the methods lego's Loopia
       client calls (checked in its source on 2026-10-16). Its
       credentials go in `.env`.
     - At Loopia, add an A record `*.home.threadsafe.se` → the server's
       LAN IP.
     - Check that the router's DNS rebind protection doesn't block it.
1. **Ingress and certificate.**
   - Run `host/01-microk8s.sh` (Traefik addon plus hardening), and write
     `scripts/secrets.sh`.
   - Add `k8s/ingress/` (lego CronJob, RBAC, certs PV/PVC) and the root
     `k8s/kustomization.yaml`.
   - Run lego against Let's Encrypt staging, then production.
   - Check that a throwaway service shows a valid certificate on a LAN
     device, that the dashboard is gone and that HTTP redirects.
2. **zotero-webdav**, verified with Zotero desktop's "Verify Server" and a
   real attachment round trip.
3. **translation-server**, smoke-tested with one DOI and one arXiv ID.
4. **Karakeep + Meilisearch + Chrome.** First login, disable sign-ups,
   then create the API key for paperbridge.
5. **paperbridge v0** (handoff). GitHub Actions builds the image on `v*`
   tags; the version lives only in `pyproject.toml`.
6. **paperbridge v1** (read-status sync back), then v2 (highlights).

## Repo layout (target)

```
CLAUDE.md
README.md                 # prerequisites + rebuild procedure
.env.example              # secret names only
.gitignore                # .env, kubeconfig*, *.secret.yaml
host/
  00-prep.sh              # ZFS datasets + sanoid
  01-microk8s.sh          # MicroK8s + hardened Traefik ingress addon
k8s/
  kustomization.yaml      # root: kubectl apply -k k8s/ rebuilds everything
  ingress/                # lego CronJob, RBAC, certs PV/PVC
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

Kustomize, applied by hand: `kubectl apply -k k8s/` applies everything,
and `kubectl apply -k k8s/<dir>` applies a single part. We use no Helm
charts or CRDs of our own; Traefik comes in through the MicroK8s addon in
`host/01-microk8s.sh`. No Flux until there are enough services to justify
it.

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

- An Ingress with `tls.hosts` and no `secretName` gets the default
  TLSStore certificate.
- lego's `loopia` provider: its DNS propagation timeout against Loopia's
  nameservers.
- The router (and VPN clients) resolve `*.home.threadsafe.se` to the LAN
  IP. Watch for DNS rebind protection.
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
