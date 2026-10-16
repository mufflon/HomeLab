# HomeLab

This is the infrastructure-as-code for one person's home server: a small
machine in a closet called `minisforum`. It describes what runs on it, why
it is set up the way it is, and how to rebuild all of it from scratch.

It's public because there's no reason for it not to be. You're welcome to
read it or borrow from it. Nothing here is a product, though: it's tuned to
one home network, one server and one person's reading habits.

> **Status:** the research pipeline runs: Karakeep, Zotero's file
> storage, and paperbridge's handoff of papers from Karakeep to Zotero.
> Syncing reading status back is next. Things marked *(planned)* below
> aren't running yet. The detailed, current state is in the "Status"
> section of [CLAUDE.md](CLAUDE.md).

## What it's for

The first stack is a **research and reading pipeline**. It solves three
annoyances:

1. **Saving and reading happen in different places.** Articles, blog posts
   and papers get saved from everywhere. [Karakeep](https://karakeep.app),
   a self-hosted bookmark manager, is the single inbox for all of it. It
   keeps an archived copy of each page, makes everything searchable, and is
   the *register*: the one place to ask "what have I saved, and have I
   read it?".
2. **Papers deserve a proper reference manager.**
   [Zotero](https://www.zotero.org) is where papers get read, highlighted
   and cited. Zotero's own file storage is small on the free plan, so PDFs
   are stored at home on a WebDAV server. Only the library metadata (titles,
   authors, notes, highlights) syncs through zotero.org.
3. **Moving papers between the two by hand is tedious.** *paperbridge* (a
   working name) is a small service written here:
   - A bookmark tagged `paper` in Karakeep becomes a complete Zotero item,
     with metadata looked up automatically and the PDF placed in Zotero's
     storage.
   - When a paper is marked read in Zotero, that flows back to Karakeep
     *(planned)*.
   - Karakeep's lists decide how papers are filed in Zotero. The lists
     chosen for it, with their sublists, appear in Zotero as collections,
     and moving a bookmark in Karakeep moves the paper in Zotero. There's
     one structure to maintain, and it lives in Karakeep. Papers added in
     Zotero directly are left alone.

A side benefit: everything lives at home, as plain files on our own disks.
And because Karakeep keeps the full text, an AI assistant can read saved
papers through Karakeep's MCP server.

## How it fits together

```mermaid
flowchart LR
  subgraph clients["Your devices"]
    browser["Browser / Karakeep app"]
    zotero["Zotero desktop / mobile"]
  end

  subgraph server["minisforum: single-node MicroK8s"]
    direction TB
    traefik["Traefik ingress<br/>HTTPS *.home.threadsafe.se"]
    karakeep["Karakeep"]
    dav["WebDAV<br/>(Zotero's PDFs)"]
    meili["Meilisearch"]
    chrome["Headless Chrome"]
    bridge["paperbridge<br/>(CronJob)"]
    ts["Zotero translation-server"]
    lego["lego<br/>(CronJob)"]
    zfs[("ZFS datasets<br/>+ snapshots")]
  end

  subgraph internet["Outbound only"]
    zoteroorg["zotero.org<br/>(library metadata)"]
    papers["arXiv, Unpaywall,<br/>publishers' sites"]
    le["Let's Encrypt"]
    loopia["Loopia DNS API"]
  end

  browser --> traefik
  zotero --> traefik
  traefik --> karakeep
  traefik --> dav
  karakeep --> meili
  karakeep --> chrome
  bridge --> karakeep
  bridge --> dav
  bridge --> ts
  bridge --> zoteroorg
  bridge --> papers
  ts --> papers
  zotero --> zoteroorg
  lego --> le
  lego --> loopia
  lego -. "wildcard certificate" .-> traefik
  karakeep -.- zfs
  dav -.- zfs
```

Only Traefik is reachable from the LAN. Everything else is cluster-internal.
The only things that leave the house are outbound calls: zotero.org for
library sync, Let's Encrypt plus Loopia's API for certificates, and the
web itself, for the pages being saved and for papers' metadata and
open-access PDFs.

### Day to day

1. Bookmark a paper in Karakeep: its arXiv or doi.org link, the
   publisher's page, or the PDF itself. Tag it `paper`.
2. Within 15 minutes paperbridge has made it a Zotero item, with full
   metadata and, when one can be found, the PDF. The bookmark gets the tag
   `in-zotero`, and its note links to the Zotero item.
3. Zotero desktop or mobile shows the paper after its next sync. If the
   bookmark is in a list under one of the mirrored lists, the paper is in
   the matching collection; move the bookmark, and the paper follows.

A paper that's already in Zotero (same DOI) is linked, not duplicated. If
no translator recognises a page, paperbridge keeps trying for a day, then
creates a minimal item tagged `needs-metadata` to fix by hand.

## Design choices, and why

- **LAN-only, with the router's VPN for remote access.**
  - The router already runs a VPN, and a VPN client lands on the LAN like
    any other machine. So the server runs no VPN of its own, and nothing is
    port-forwarded.
  - The flip side is that "on the LAN" is not a security boundary. Every
    exposed service has its own login, and internal services are not
    exposed at all.
- **Real HTTPS without exposing anything.**
  - Services get names under `*.home.threadsafe.se`, a public DNS record
    that points at the server's private LAN address. The names work
    everywhere, but they lead nowhere unless you're on the LAN.
  - [lego](https://go-acme.github.io/lego/) gets a Let's Encrypt wildcard
    certificate through a **DNS-01** challenge. It proves ownership by
    creating a temporary TXT record through Loopia's API, so Let's Encrypt
    never has to connect to the server.
  - The wildcard keeps individual service names out of the public
    certificate logs.
  - The usual tool, cert-manager, was skipped: its only Loopia integration
    is unmaintained since 2021.
- **Single-node [MicroK8s](https://microk8s.io).** It's one machine, so
  there's no high availability to pretend at. Kubernetes still earns its
  place: every service is declared in files, isolated, restartable, and
  rebuilt with one command. MicroK8s is a single snap with the cluster DNS,
  networking and ingress (Traefik) as addons.
- **Data on ZFS, not on the system disk.**
  - Each service's data is its own dataset on a raidz1 pool of four disks
    (in a USB enclosure; a mini PC has no room inside), with automatic
    snapshots (hourly, daily, monthly) by
    [sanoid](https://github.com/jimsalterjrs/sanoid).
  - This machine has a history of hard power-offs. Once, an ext4 file was
    left empty by one of them, which quietly broke the original cluster
    for months. ZFS's copy-on-write design doesn't produce half-written
    files like that.
- **paperbridge keeps no state of its own.**
  - The `in-zotero` tag on a bookmark means "done", and a line in the
    Zotero item's Extra field points back at the bookmark. If a run stops
    halfway, the next one finds what was already done and finishes it.
  - Zotero's web API can't upload files to WebDAV storage. So paperbridge
    writes the PDF into the WebDAV folder itself, in the same zip-plus-
    checksum format Zotero desktop uses there, and only then creates the
    attachment in the library. Zotero desktop downloads it like any synced
    file.
- **One structure, kept in Karakeep.**
  - Two services with their own folders would drift apart. So Zotero's
    collections follow Karakeep's lists one-way, and are never organized
    by hand.
  - Karakeep holds more than papers. Only the lists named in
    `MIRROR_LISTS` (in `k8s/research/paperbridge/paperbridge.env`) are
    mirrored, with everything under them.
  - Papers added to Zotero directly, and collections outside those trees,
    are left alone.
  - An emptied collection goes to Zotero's trash rather than being
    deleted, so nothing is lost by accident.
- **The repo is the rebuild recipe.** Host setup is idempotent scripts.
  Everything in the cluster is plain Kubernetes manifests, applied with a
  single `kubectl apply -k k8s/`. We add no Helm charts, operators or CRDs
  of our own. Secrets are the one thing outside git: their names are in
  `.env.example`, and their values in a local `.env`.

## What's in the repo

| Path | What it is |
|---|---|
| `README.md` | This file: purpose, design, how to rebuild |
| `CLAUDE.md` | The detailed spec and working notes. [Claude Code](https://claude.com/claude-code), the AI coding assistant this was built with, reads it at the start of every session. It has the exact versions, UIDs, gotchas and open questions. Worth reading for the "how exactly" |
| `host/` | Idempotent scripts run once on the server with sudo (storage, MicroK8s) |
| `k8s/` | Kubernetes manifests, one directory per area: `storage/` (ZFS-backed volumes), `ingress/` (certificates), `research/` (the apps) |
| `scripts/` | Helpers run from your machine: `secrets.sh` turns `.env` into Kubernetes Secrets |
| `images/` | Third-party images we build ourselves, e.g. Zotero's translation-server, because the published one is ARM-only |
| `services/paperbridge/` | The glue service: its Python source, tests and container build |
| `.github/workflows/` | GitHub Actions that build those images and publish them to `ghcr.io/mufflon/*` |
| `.env.example` | Every secret the setup needs, by name only |

## Prerequisites (outside this repo)

1. **Server**
   - Ubuntu 26.04 LTS, your user with SSH key access and sudo.
   - A **fixed LAN address**: a DHCP reservation on the router. The DNS
     record below points at it.
2. **ZFS pool `hddpool`**
   - It also holds the media library, so this repo never creates or
     destroys it.
   - If it already exists, the cluster's data in `hddpool/k8s/*` is reused
     on rebuild: the PersistentVolumes are `Retain` and point at the same
     paths.
3. **DNS at Loopia, for `threadsafe.se`**
   - An A record `*.home.threadsafe.se` → the server's LAN address. It is
     public, but it points at a private IP.
   - A LoopiaAPI user for certificate renewal, with exactly these rights:
     `getZoneRecords`, `addZoneRecord`, `removeZoneRecord`,
     `removeSubdomain`. Nothing else: no domain, billing or DNS-server
     rights.
4. **Router**
   - The VPN server for remote access.
   - **No** port forwards to this server.
   - DNS rebind protection, if any, must allow `home.threadsafe.se`.
5. **Accounts**
   - A Zotero account, and an API key with library read/write.
   - Optionally, an email address for Unpaywall's open-access lookup. It
     needs no account; it just asks callers to identify themselves.
   - The Karakeep API key is created during the build (step 4).
6. **Your machine** (the workstation you manage the cluster from, not the
   server)
   - A clone of this repo, with `.env` filled in from `.env.example`.
     `.env` stays on this machine: it is gitignored, and the server never
     stores it. `scripts/secrets.sh` sends the values into the cluster as
     Secrets.
   - `kubectl` within one minor version of the cluster.

## Rebuild

Run the host scripts on the server. Each one is idempotent, so a rerun
only fixes what is missing. Typing the sudo password is the only
interactive part.

```bash
scp host/*.sh minisforum:
ssh -t minisforum 'sudo bash ./00-prep.sh 2>&1 | tee 00-prep.log'
ssh -t minisforum 'sudo bash ./01-microk8s.sh 2>&1 | tee 01-microk8s.log'
```

1. **`host/00-prep.sh`** sets up storage:
   - the ZFS datasets under `hddpool/k8s`, each owned by the UID its app
     runs as;
   - sanoid snapshots for `zotero-webdav`, `karakeep` and `certs`.
2. **`host/01-microk8s.sh`** sets up the cluster:
   - MicroK8s `1.36/stable`;
   - the Traefik ingress addon, using the wildcard certificate as the
     default;
   - the Traefik dashboard route switched off, and HTTP redirected to
     HTTPS.
3. **Kubeconfig:** `ssh minisforum microk8s config > ~/.kube/homelab`.
   Point `KUBECONFIG` at it. Never commit it.
4. **Secrets:** `scripts/secrets.sh` creates every Kubernetes Secret from
   `.env`. Secrets whose values aren't filled in yet are skipped.
5. **Everything else:** `kubectl apply -k k8s/`.
   - Storage (StorageClass + PVs) and the lego certificate CronJob.
   - The research apps: WebDAV, translation-server, Karakeep with
     Meilisearch and headless Chrome, and the paperbridge CronJob.
   - No Helm charts and no CRDs of our own, so a single apply is enough.
6. **First certificate:** run the CronJob once instead of waiting for its
   schedule:

   ```bash
   kubectl -n ingress create job lego-now --from=cronjob/lego
   ```

   It usually takes 1–5 minutes, mostly waiting for Loopia's DNS. After
   that the CronJob renews by itself.
7. **First-run steps per app:**
   - **Karakeep.** If its data on `hddpool/k8s/karakeep` survived, your
     account is still there and there's nothing to do. On a **completely
     empty** rebuild:
     1. Set `DISABLE_SIGNUPS=false` in `k8s/research/karakeep/karakeep.env`
        and apply.
     2. Create your account. The first one becomes admin.
     3. Set it back to `true` and apply again.

     Then, in Karakeep → Settings → API Keys, create the key for
     paperbridge, put it in `.env` and rerun `scripts/secrets.sh`.
   - **Zotero desktop:** Settings → Account → File Syncing → WebDAV, URL
     `https://zotero-dav.home.threadsafe.se/`, user and password from
     `.env`, then "Verify Server".
   - **paperbridge** needs the Karakeep key from above plus the Zotero
     user ID and API key in `.env`, then `scripts/secrets.sh`. Until its
     Secret exists, each run of its CronJob fails at start, which is
     harmless.
   - **Optional, auto-tagging papers:** in Karakeep → Settings → Rules,
     add a rule "when a bookmark is added, if the URL contains `arxiv.org`
     or `doi.org`, add the tag `paper`". Rules live in Karakeep's database,
     so they survive a rebuild with the data and aren't in this repo.

## What this does not cover

- **Clean shutdown on power loss.** There is a UPS, but it isn't connected
  to the server, so a long outage still ends in a hard power-off. A USB
  link plus NUT would fix that.
- **Off-site backup.** sanoid snapshots live on the same pool, so they
  protect against mistakes, not against losing the pool. Off-site copies
  (e.g. `syncoid` to another machine) are not set up.
- **The media library, Jellyfin and NFS.** They run on the host, outside
  Kubernetes, and are not managed here.
