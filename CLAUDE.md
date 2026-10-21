# CLAUDE.md

## What this is

Configuration for a single home server, `minisforum`, and the services it
runs in Kubernetes. The first stack is a research pipeline:

- **Karakeep** is the register. Everything gets bookmarked there first:
  articles, pages, papers.
- **Zotero** is where papers are read and annotated. Library data
  (metadata, notes, annotations) syncs through zotero.org; PDF files sync
  through our own **WebDAV** server, not Zotero's storage. The owner does
  **not** add PDFs to Zotero by hand. Papers and their PDFs arrive through
  paperbridge, and Zotero presents what Karakeep gathered.
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
- **Step 0, owner tasks: done.** The LoopiaAPI user exists, the A record
  `*.home.threadsafe.se` → LAN IP resolves (and the router passes it, so
  there's no DNS-rebind block), and `.env` has the step 1 values.
- **Step 1: done.** Verified from a LAN machine on 2026-10-16, and over
  the router VPN on 2026-10-20: the owner's phone, on the VPN, synced
  Zotero files through `https://zotero-dav.home.threadsafe.se`. So VPN
  clients resolve `*.home.threadsafe.se`, reach the server and trust the
  certificate.
  - Traefik 3.6.13 is live on the server's ports 80/443.
  - lego got the production wildcard certificate (issuer `Let's Encrypt
    YE1`). Staging took about 1 minute and production about 5, mostly
    waiting for Loopia's DNS. The CronJob renews daily at 03:23 UTC
    when due.
  - A throwaway Ingress (`tls.hosts`, no `secretName`) answered `200` on
    `https://test.home.threadsafe.se/` with a trusted certificate.
  - HTTP gets `301` to HTTPS. Unknown names get `404`. The dashboard and
    its API return `404`, even with `Host: dashboard.localhost`.
  - The kubeconfig is `~/.kube/homelab` on the workstation.
  - Leftover: lego's staging account and certificate under
    `/data/letsencrypt-staging` on the certs dataset. Harmless.
- **Step 2, zotero-webdav: deployed and tested with curl from the LAN**
  (2026-10-16).
  - `401` without a login or with a wrong one.
  - `PROPFIND` returns `207`, and `zotero/` exists.
  - A missing `.prop` returns `404`.
  - PUT, GET and DELETE work; a 20 MB round trip took about 1 s with
    matching checksums.
  - `DAV: 1, 2`.
  - Zotero desktop: "Verify Server" succeeded. A real attachment synced up
    as a `.zip` plus a `.prop`; it was a test, deleted afterwards. Its
    format is recorded under paperbridge, step 4.
- **Step 3, translation-server: done** (2026-10-16).
  - Docker Hub's 2.0.6 is arm64-only and fails on this amd64 server with
    `exec format error`. We build our own image instead (see Components).
  - Smoke-tested via port-forward:
    - `/search 10.1038/nature14539` → *Deep learning*, Nature, journal
      article;
    - `/search arXiv:1706.03762` and `/web https://arxiv.org/abs/1706.03762`
      → *Attention Is All You Need*, preprint.
  - Gotcha for paperbridge: arXiv items carry the **latest version's**
    date (2023-08-02 for v7), not the original submission date.
- **Step 4, Karakeep + Meilisearch + Chrome: done** (2026-10-16).
  - Migrations ran, `db.db` is on ZFS, and the crawler is connected to
    Chrome.
  - `https://karakeep.home.threadsafe.se` answers over trusted HTTPS, and
    `/api/health` reports ok.
  - The owner's account exists, and then `DISABLE_SIGNUPS=true` was
    applied. Verified: the sign-in page serves `"disableSignups":true`.
- **Step 5, paperbridge v0: done**, verified end to end on 2026-10-16.
  - The owner checked Zotero desktop: the item synced, and its
    "Preprint PDF" downloaded from WebDAV and opens. So desktop picks up
    files paperbridge writes directly.
  - The CronJob runs `ghcr.io/mufflon/paperbridge` every 15 minutes:
    0.1.2 at the end of step 5; 0.2.1 now. Its Secret comes from `.env`. In `scripts/secrets.sh`, a
    source key ending in `?` is optional (`UNPAYWALL_EMAIL`). The WebDAV
    login comes from the existing `zotero-webdav` Secret.
  - Live test, run in-cluster as Jobs made from the CronJob:
    - Bookmark `https://arxiv.org/abs/1706.03762`, tagged `paper`.
      - It became a `preprint`, *Attention Is All You Need*, with all 8
        authors, DOI `10.48550/arXiv.1706.03762`, and the date fixed to
        the v1 date 2017-06-12.
      - It got the arXiv comment as a child note, and a "Preprint PDF"
        attachment (2.2 MB): `Vaswani et al. - 2017 - Attention Is All
        You Need.pdf`.
      - On WebDAV: `<KEY>.zip` holds that one file, and `.prop` has the
        MD5 of the unzipped PDF, which matches the item's `md5`.
      - The bookmark got `in-zotero`, and its note got the
        `zotero://select/…` and web-library links.
    - Same paper, bookmarked again as `https://arxiv.org/pdf/1706.03762`.
      Karakeep made it a PDF bookmark. paperbridge linked it by DOI: a
      second `Karakeep:` line in Extra, no new item, no second PDF.
    - Interrupted-run test: removing `in-zotero` from the first bookmark
      made the next run find the item by its Extra line. It only re-tagged
      the bookmark.
    - After that, a run finds nothing to do. The library holds exactly
      three objects.
  - 0.1.0 and 0.1.1 were broken; both bugs were found live:
    - 0.1.0: Zotero rejects a create with a client-chosen `key` unless
      it also has `"version": 0` (`428 Either If-Unmodified-Since-Version
      or 'version' property must be provided for 'key'-based writes`). That
      run had already uploaded the PDF's two WebDAV files; those orphans were
      deleted.
    - 0.1.1: quick search can't find DOIs or Extra lines (see "v0 as
      built"). It would have made duplicates.
    - The CronJob was suspended by hand while the fixes went out, then
      unsuspended. The manifests match what runs.
  - Found in Karakeep v0.33.2:
    - Bookmarking a PDF URL turns the bookmark into an `asset` bookmark
      (`assetType: pdf`, `sourceUrl`, the extracted text in `content`).
      Verified live.
    - A link bookmark's `pdfAssetId` is Karakeep's print of the web page,
      not the paper. paperbridge ignores it.
  - Found in translation-server: it returns no attachments (no PDF URLs).
    PDFs therefore come from Karakeep's asset, then `arxiv.org/pdf/<id>`,
    then Unpaywall.
  - On WebDAV, `77YMI7CV.zip` / `.prop` remain from step 2's deleted test
    item. Zotero desktop purges orphaned WebDAV files by itself.
- **Lists → collections: done in paperbridge 0.2.0** (2026-10-16), ahead
  of step 6 at the owner's request. It is deployed and mirrors `Papers`
  (see paperbridge, "Structure").
- **paperbridge 0.2.1** (2026-10-16): a PDF taken out of Zotero and
  uploaded to Karakeep links to its existing item instead of duplicating
  it.
  - That covers papers added in Zotero directly. It works through an
    identifier in the PDF's text, or by title (see "v0 as built").
  - Tested live: the test paper's PDF, downloaded from our WebDAV under
    Zotero's file name, was uploaded as a Karakeep file bookmark with no
    URL. It linked through its arXiv ID. With translation-server pointed
    at a dead URL, a dry run linked it by the file name's title. The
    throwaway bookmark and its Extra line were then removed.
  - Every run now reads the Zotero library, even with nothing to hand
    off. That is about one request per 100 items, plus the collections.
- **Step 6, paperbridge v1 (0.3.0): done**, verified live on 2026-10-16.
  It syncs read status Zotero → Karakeep, and metadata and comments both
  ways (paperbridge, "Sync").
  - Root step: the owner ran `host/00-prep.sh` once. It created
    `hddpool/k8s/paperbridge`, owned 10004, with lz4, atime off and
    sanoid snapshots. The CronJob mounts it at `/state`; the snapshot of
    last-synced values is `/state/state.json`.
  - The first sync, on the test paper's two bookmarks, gave the link
    bookmark Zotero's authors and date, and the PDF bookmark the paper's
    title. The run after that changed nothing.
  - Live tests on the test paper, each reverted afterwards:
    - a title edited in Karakeep reached Zotero and the other bookmark in
      the same run;
    - an abstract edited in Zotero reached the link bookmark (the PDF
      bookmark has no description);
    - a title edited on both sides: Zotero's won;
    - a comment written in Karakeep created the "From Karakeep" note;
    - editing that note in Zotero updated the bookmark's note, keeping
      the Zotero links last;
    - comments edited on both sides kept both texts with the dated
      marker, on both sides;
    - `Read_Status: Read` (the plugin's exact lines) archived both
      bookmarks and tagged them `read-2026-10`; archived bookmarks are
      still listed by tag, so they keep syncing;
    - "To Read" undid it.
  - Cleanup: the original title, abstract (re-fetched from
    translation-server) and Extra were restored. The test note is in
    Zotero's trash. The unused `read-2026-10` tag was deleted.
  - The Reading List plugin is installed in the owner's Zotero desktop,
    and read status was verified with it on 2026-10-16. Marking the test
    paper Read in Zotero archived both linked bookmarks on the next run and
    tagged them `read-2026-10`. The paper is left marked Read: it was a real
    read.
- **paperbridge v2 (0.4.0): done**, verified live on 2026-10-16:
  highlights Zotero → Karakeep (paperbridge, "Sync", Annotations).
  - Test: the owner made throwaway annotations on the test paper's PDF
    in Zotero desktop: three highlights, and an area (image) annotation
    with a comment.
    - Within one run they were in both bookmarks' notes, in reading
      order, between the (empty) comments and the Zotero links. The area
      annotation showed as `- p. 8, image: <comment>`.
    - The next runs changed nothing.
    - A Karakeep search for a highlighted phrase found the arXiv abstract
      page's bookmark, which only has it in its note.
    - No "From Karakeep" note appeared in Zotero, so the highlights stay
      out of the two-way comments.
  - Cleanup: the owner deleted the annotations in Zotero desktop. The
    next run removed the section from both notes; the run after changed
    nothing.
  - The owner's real read state on that paper (archived, `read-2026-10`)
    stayed as it was.
  - Annotations reached Zotero's server only after desktop synced. A
    comment added later needed another sync first.
- **Renovate** (2026-10-16): `renovate.json` committed (see "Dependency
  updates").
  - The first install attempt hadn't taken. developer.mend.io said no
    account of the owner's had the app.
  - The owner installed it on `mufflon`, HomeLab only, choosing "Scan and
    Alert". That is Mend's interactive mode; "Scan only" (silent) creates
    nothing in the repo.
  - Within a minute the Dependency Dashboard issue (#1) appeared, matching
    the local dry run.
  - Then: lock-file maintenance off (there are no lock files), and the
    CI's Python moved into the paperbridge group.
  - The first PRs were opened early, by ticking the dashboard's
    "create all awaiting schedule PRs" box. #2–#5 were merged on
    2026-10-16 at the owner's request:
    - #2 GitHub Actions, pinned to SHAs.
    - #3 cluster images: digests for lego, kubectl and webdav, and the
      watchdog on Python 3.15. Applied and verified:
      - webdav rolled out;
      - a lego run pulled by digest and skipped renewal;
      - a watchdog run on 3.15 found everything fine.
    - #5 paperbridge on Python 3.15, released as **0.4.1** (no code
      changes). Its CronJob is pinned to the new digest, and a live run
      synced 2 pairs and changed nothing.
    - #4 Traefik chart 39.0.9 (Traefik v3.6.13 → v3.6.15). The owner
      reran `host/01-microk8s.sh` on 2026-10-16 at 23:27 UTC (the first
      try had timed out at the sudo prompt).
      - The addon step said "already enabled"; the `helm upgrade` did the
        work (revision 4). MicroK8s stayed on `1.36/stable`, no refresh.
      - Verified from the LAN: Karakeep `200`, WebDAV `401` without a
        login, HTTP `301` to HTTPS, an unknown name `404`, the dashboard
        and its API `404` with `Host: dashboard.localhost`, Let's
        Encrypt's wildcard served.
      - No alert flipped through the WebDAV rollout or Traefik's restart.
    - #6, Traefik chart 42 (a major), was closed unmerged: as proposed it
      would have failed (see "Ingress"). It was superseded by 4b1db94,
      which rewrote the script's Traefik step. That went live on
      2026-10-16 at 23:46 UTC (Helm revision 5, Traefik v3.7.14).
      - Before it: a local render diff against the live values, and a
        server-side `helm upgrade --dry-run`.
      - Verified from the LAN as in step 1:
        - Karakeep `200`, WebDAV `401` without a login;
        - HTTP `301` to HTTPS, an unknown name `404`;
        - the dashboard and its API `404` with `Host: dashboard.localhost`;
        - Let's Encrypt's wildcard served.
      - The IngressClasses and the default TLSStore are unchanged.
      - Traefik's log shows no errors and no alert flipped. The watchdog's
        first run after the upgrade was green.
      - New startup warnings in v3.7, harmless here:
        - `aliasHeadersStrategy` unset, which matters for CGI, WSGI, PHP
          or NGINX backends; ours are Node and Go;
        - `SafeNaming` unset, with legacy names kept; we have no CRD
          routers.

        Setting both would only silence them.
    - On approval only: kubectl 1.37 (waits for MicroK8s 1.37) and
      Meilisearch 1.55.
  - Gotcha: `gh pr merge` refuses a PR that changes
    `.github/workflows/*` unless gh's token has the `workflow` scope
    (#2 went through anyway, #5 didn't). #5 was merged locally with
    `git merge --no-ff` and pushed over SSH; GitHub then marks the PR
    merged.
  - A local dry run found every pin. First updates it proposed: Traefik
    chart 39.0.9 (and 42.0.0 as a major), Python 3.15 for paperbridge,
    digests for the three tag-only images (webdav, lego, kubectl), Actions
    pinned to SHAs. Meilisearch v1.55 and kubectl 1.37 wait for approval.
- **Alerts: done**, every check tested live on 2026-10-16 (see "Alerts").
  - The owner's healthchecks.io project has the read-write API key in
    `.env`, and email as its only integration. The first watchdog run
    created the six checks.
  - The project also has the owner's own check `mini`, which the watchdog
    leaves alone.
  - `host/02-alerts.sh` ran with `test`:
    - ZED posted its "ZFS device fault for pool alerttest" report (class
      `statechange`, state `FAULTED`) to `zfs` within a second of
      `zpool offline -f`.
    - The next watchdog run reported `alerttest: DEGRADED`.
    - After the pool was destroyed, the run after that set `zfs` up again.
  - The other tests used throwaway objects in `monitoring`, deleted
    afterwards: a suspended CronJob whose run exits 1, and a Deployment
    whose pod exits 1.
    - The crashing Deployment showed `Progressing=True
      NewReplicaSetAvailable` with `Available=False`, so it counted at
      once.
    - A one-off watchdog Job also added an endpoint Traefik answers `404`,
      and set `CERT_MIN_FRACTION=1`.
    - `cronjobs`, `workloads`, `endpoints` and `certificate` each went
      down, then up again on a later run.
  - Heartbeat: the watchdog CronJob was suspended until `heartbeat` went
    down, then resumed.
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
  note why in the commit. Renovate proposes the bumps as PRs (see
  "Dependency updates"); nothing updates by itself.
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

- **Ingress.** Traefik, installed through the MicroK8s `ingress` addon
  (chart 42.0.0, Traefik v3.7.14; ingress-nginx was retired upstream in
  2026). It runs as a DaemonSet on host ports 80/443. `host/01-microk8s.sh`
  does it in two steps:
  1. **The addon** (`microk8s enable ingress --default-ssl-certificate
     ingress/wildcard-home-tls`) installs its own chart version (39.0.8 in
     MicroK8s 1.36) with its own values, and creates Traefik's default
     `TLSStore` outside Helm. On a cluster that already has the addon, it
     changes nothing.
  2. **Our chart and values.** The script pulls `TRAEFIK_CHART` and applies
     its `traefik.io` CRDs (Helm never upgrades CRDs; no Hub CRDs, and the
     addon brings the Gateway API CRDs, bundle v1.5.1). Then it runs
     `helm upgrade --reset-values` with the full configuration written into
     the script.
     - That configuration is the addon's, under chart 42's key names, plus
       our changes, marked "ours".
     - `--reset-values`, not `--reuse-values`: chart 40 renamed
       `providers.kubernetesIngressNginx` to `kubernetesIngressNGINX`, and
       its schema rejects the old key. The addon's values carry it, and
       `--set key=null` can't remove a key the chart doesn't define.
  - Ours: **no dashboard.** The addon's values create an unauthenticated
    dashboard route on `Host(dashboard.localhost)`. `api.dashboard` and
    `ingressRoute.dashboard.enabled` are off.
  - Ours: a **web → websecure** redirect,
    `ports.web.http.redirections.entryPoint.*`. Since chart 39 it sits
    under `http`; the older `ports.web.redirections` fails schema
    validation.
  - Ours: rollouts **update in place** (`maxSurge 0`, `maxUnavailable 1`).
    The chart's default surge rollout deadlocks on a single node, because
    the new pod needs host ports 80/443 that the old pod still holds.
    Ingress therefore blips per update: seconds, or as long as pulling a
    new image takes.
  - The addon's IngressClasses (`public` as the default, `nginx`) and its
    providers (Ingress, Gateway API, the ingress-nginx compatibility one)
    are kept as they were. Only `public` is used.
  - When a MicroK8s minor changes the addon's values, compare them with
    the script's (`microk8s helm get values traefik -n ingress` right
    after a fresh `enable`).
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
  don't re-issue and hit Let's Encrypt rate limits.
  - `ACME_SERVER` in `k8s/ingress/lego-config.yaml` (a lego shortcode:
    `letsencrypt` or `letsencrypt-staging`) selects both the CA and the
    state directory `/data/<shortcode>`. Use staging for experiments.
  - lego **v5** changed the CLI from v4: `run` now gets *or* renews (no
    separate `renew`), and renewal timing is automatic (ARI, about
    1/3 of the certificate's lifetime left).
  - lego v5's help text shows Loopia's two timeouts swapped. Both are set
    explicitly in the ConfigMap: wait up to 40 minutes, check every
    minute.
  - Files land in `certificates/_.home.threadsafe.se.{crt,key}`.

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
| `hddpool/k8s/paperbridge` | paperbridge's sync snapshot (`state.json`) | yes |

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
- **paperbridge:** 10004. Our own image, built to run as non-root.

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
| translation-server | `ghcr.io/mufflon/translation-server`, built by our workflow from the commit in `images/translation-server/UPSTREAM_REF` (Docker Hub's is arm64-only), pinned by digest | ClusterIP `:1969` only | none |
| karakeep | `ghcr.io/karakeep-app/karakeep` | `karakeep.home.threadsafe.se` | `karakeep` |
| meilisearch | `getmeili/meilisearch:v1.41.0`, the version Karakeep's own manifests pin (newest upstream is v1.55) | ClusterIP only | `meilisearch` |
| chrome | `ghcr.io/karakeep-app/karakeep-chrome` (runs as 65534) | ClusterIP only | none |
| paperbridge | `ghcr.io/mufflon/paperbridge`, built from `services/paperbridge` on `v*` tags (the version is in its `pyproject.toml`), pinned by digest; runs as 10004 | CronJob every 15 min, no ingress | `paperbridge` (the sync's snapshot); the handoff's state is Karakeep tags and Zotero's `Extra` |

**Zotero WebDAV.** One user, basic auth, password read from the Secret
through `{env}`. It is plain rather than bcrypt because the server checks
it on every request, and the extra protection only matters once the
cluster is already compromised (the owner's bar is "sturdy, not
maximally hardened"),
HTTPS only. Zotero desktop: Settings → Sync → File Syncing → WebDAV, URL
`https://zotero-dav.home.threadsafe.se/`. Zotero appends `zotero/` itself,
and that directory already exists. Zotero on mobile works at home, or over
the router VPN when away.

**Karakeep.** Its own login. Set `DISABLE_SIGNUPS=true` once the owner's
account exists. `NEXTAUTH_SECRET` and `MEILI_MASTER_KEY` come from `.env`.

### Alerts: healthchecks.io

Nothing on the server notifies anyone directly. Every check reports pass
or fail to its own check in a [healthchecks.io](https://healthchecks.io)
project. healthchecks.io alerts on changes (up → down, down → up) through
the project's integrations.
- The owner chose this on 2026-10-16. The channel is picked in
  healthchecks.io, with no change here; email to the account's address
  comes with sign-up.
- healthchecks.io also notices when reports **stop**, which nothing on the
  server can.

**Watchdog** (`k8s/monitoring/`): a CronJob every 5 minutes.
`watchdog.py` (standard library only) is mounted from a ConfigMap into the
stock `python` image and runs as UID 10005. Its settings are in
`watchdog.env`.

| Check (slug) | Fails when | How |
|---|---|---|
| `zfs` | a pool in `POOLS` isn't imported, or an imported pool isn't `ONLINE` (e.g. `DEGRADED`, `SUSPENDED` when the enclosure drops); or ZED sends a notification | `/proc/spl/kstat/zfs/<pool>/state` through a read-only hostPath; ZED on the host (below) |
| `cronjobs` | a CronJob's latest finished Job failed; or it had no success within its `homelab/stale-after` annotation (paperbridge `1h`, lego `26h`), suspended or not | API, all namespaces |
| `workloads` | a Deployment's rollout is stuck (`Progressing=False`), or it is unavailable outside a rollout; a DaemonSet has fewer available pods than desired | API, all namespaces |
| `endpoints` | Karakeep `/api/health` isn't `200`, WebDAV without a login isn't `401`, Jellyfin's `/health` (on the node, port 8096) isn't `200` | through Traefik under the public names, so DNS, ingress and certificate count too |
| `certificate` | the certificate Traefik serves has less than `CERT_MIN_FRACTION` (0.2) of its lifetime left, or TLS fails | TLS handshake |
| `heartbeat` | no ping for 5 + 10 minutes | pinged last in every run |

- **Each run upserts every check** through the Management API
  (`unique: ["slug"]`). Name, period, grace, description and integrations
  (`channels: "*"`, i.e. all of the project's) are therefore defined in
  `watchdog.py`.
  - Edits made in the healthchecks.io UI are reset within 5 minutes.
  - An integration added later reaches every check on the next run.
  - Then it pings: success with a summary, or `/fail` with one problem per
    line.
- **Only the heartbeat alerts on silence quickly** (period 5 minutes, grace
  10). The others have a period of a week, so a dead server raises one
  alert, not six.
- Workloads and endpoints that fail are checked again after
  `RETRY_SECONDS` (60) before they count, so a pod restart or Traefik's
  few-second rollout blip doesn't alert.
- Detected problems don't fail the watchdog's Job. Only its own errors do
  (e.g. healthchecks.io unreachable), and the next run's `cronjobs` check
  then reports that.
- The certificate threshold is a fraction because Let's Encrypt lifetimes
  are shrinking (90 days now, 45 by 2028). lego renews with a third left,
  so an alert means renewal has failed for a while; the `cronjobs` check
  should have said so first.
- Messages go to a third party, so endpoint URLs keep `{HOST_IP}` rather
  than the LAN address.
- Without the `healthchecks` Secret (an optional `secretRef`), it only
  logs: `kubectl -n monitoring logs job/<latest>`. To try a failure, run
  a Job from the CronJob with overridden settings (e.g. an extra
  `ENDPOINTS` entry, `CERT_MIN_FRACTION=1`), as in "Status".

**ZED** (`host/02-alerts.sh`), ZFS's event daemon on the host, reports to
the same `zfs` check through its **mail hook**.
- `ZED_EMAIL_PROG=/usr/local/sbin/zed-healthchecks` posts the notification
  to the check's ping URL plus `/fail`.
- ZED calls it as `<prog> -s '<subject>' root` (the default
  `ZED_EMAIL_OPTS`), with the message on stdin. `ZED_EMAIL_ADDR` must be
  set for ZED to call it at all.
- ZED's own zedlets decide what's worth a notification:
  - a disk `FAULTED`, `DEGRADED`, `REMOVED` or `UNAVAIL`;
  - data errors;
  - scrubs and resilvers that end unhealthy (`ZED_NOTIFY_VERBOSE` stays
    0).

  It sends at most one per pool and kind of event per hour
  (`ZED_NOTIFY_INTERVAL_SECS=3600`).
- The ping URL is looked up once, with the Secret's key, and kept
  root-only in `/etc/zfs/zed.d/healthchecks-ping-url`. Rerun the script if
  the check is ever recreated.
- If the pool is still `ONLINE`, the watchdog's next run sets the check
  back to up. A corrected error therefore gives a down/up pair, with ZED's
  text in the down alert.
- OpenZFS 2.4.1 also has ntfy, Pushover, Slack and Gotify built into
  `zed.rc`. They're unused: one hub instead.
- Ubuntu's cron scrubs on the second Sunday of each month; that only
  alerts when it finds something.

**Not covered:**
- USB resets of the enclosure that cause no ZFS error;
- SMART (pass-through unverified);
- host services other than Jellyfin;
- ZED itself stopping;
- the UPS (deferred).

### Dependency updates: Renovate

Everything is pinned, so nothing updates by itself. Mend's hosted
**Renovate** GitHub App (installed by the owner on this repo only) reads
`renovate.json` and opens PRs. No automerge: every PR is merged, and then
deployed, by hand.

- **Calm by design.** PRs are only opened on Mondays (Europe/Stockholm),
  grouped by what has to happen after merging. Majors come as a separate
  PR per group (Renovate's default). The "Dependency Dashboard" issue lists
  everything, including updates waiting for the schedule or for approval.
- `config:best-practices`: Docker images get tag **and** digest (it pins
  digests that are missing), and GitHub Actions are pinned to commit SHAs
  with the version in a comment.

| Group | What | After merging |
|---|---|---|
| cluster images | every `image:` in `k8s/**/*.yaml` | `kubectl apply -k k8s/` |
| paperbridge dependencies | `pyproject.toml` (dependencies, the `test` extra, hatchling), the Dockerfile's base image, and the CI's `setup-python` version (so the PR is tested on the new Python) | a paperbridge release: bump `version`, push the `vX.Y.Z` tag, pin the new digest in its CronJob, apply |
| GitHub Actions | the actions in `.github/workflows` | nothing |
| host scripts | `TRAEFIK_CHART` in `host/01-microk8s.sh` | rerun that script with sudo |
| translation-server upstream | `images/translation-server/UPSTREAM_REF`, following `zotero/translation-server` `master` (`git-refs`) | the workflow builds the image; then pin its tag and digest in the Deployment, apply |

- **Only on request**, by ticking the box in the dashboard
  (`dependencyDashboardApproval`):
  - **Kubernetes minor:** the MicroK8s channel and `alpine/kubectl`
    (lego's CronJob, pinned to the cluster's minor) move together.
    `host/01-microk8s.sh` refreshes the snap when `CHANNEL` differs from
    what it tracks; upgrade one minor at a time.
  - **Meilisearch**, which stays on Karakeep's pinned version (see "Verify
    before relying on").
- **Not tracked:** `ghcr.io/mufflon/*` images. We set their tag and digest
  when we release them.
- How each pin is found:
  - The `kubernetes` manager has no default file pattern;
    `renovate.json` sets `k8s/**/*.yaml`.
  - The `poetry` manager is off. It claims `pyproject.toml` but ignores
    `build-system.requires`; `pep621` reads all of it.
  - The MicroK8s channel uses the `github-releases` of
    `canonical/microk8s`. A release (`v1.36`) is published when a minor
    reaches stable.
  - The Traefik chart comes from `https://traefik.github.io/charts`. The
    MicroK8s addon installs its own version first; the script's `helm upgrade`
    replaces it (see "Ingress").
  - `karakeep-chrome` tags look like `154.0.8037.57-r1`, plus `-amd64` and
    `-arm64` variants. A `regex:` versioning keeps only the plain ones.
- Our PRs run paperbridge's tests (the workflow also triggers on
  `pull_request`). Nothing else has CI.
- To check a config change locally:
  `npx --package renovate renovate-config-validator --strict`. A dry run,
  `renovate --platform=local --dry-run=lookup`, only sees committed files
  (it lists them with git), so run it in a scratch clone with the change
  committed. Renovate 44 wants Node 24; Node 25 works but logs an error.

### paperbridge: Karakeep → Zotero → Karakeep

**One entry point for files and structure** (owner, 2026-10-16). This is
the governing rule; everything below follows from it.

- Karakeep is the single entry point for files (PDFs) and for structure
  (lists → collections). Both flow one way, Karakeep → Zotero.
- Papers added to Zotero directly stay in Zotero and are never synced
  back. To bring one into Karakeep, download its PDF from Zotero and add
  it there. paperbridge links it to the existing item instead of making a
  duplicate (0.2.1, tested).
- Comments and metadata may sync both ways (step 6, "Sync" below).

A Python CronJob, every 15 minutes. It must be idempotent. Tags and Zotero's
`Extra` field carry the handoff's state. Since v1 (0.3.0), a snapshot of
last-synced values serves the two-way sync (below).
- If `state.json` is lost, every pair has a first sync again, where
  Zotero's value wins. Karakeep-side edits made since the last run would
  then be lost; restore the file from a sanoid snapshot instead.

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
   Observed from a real Zotero desktop upload on 2026-10-16:
   - `<KEY>.zip` is a plain zip holding the file under its original name
     (e.g. `databehov.pdf`).
   - `<KEY>.prop` is one line:
     `<properties version="1"><mtime>1792105737901</mtime><hash>84c1…</hash></properties>`.
     `mtime` is in milliseconds since the epoch. `hash` is the MD5 of the
     **unzipped file**, not of the zip (verified by comparing both).
5. **Back in Karakeep:** tag `in-zotero`, and add the Zotero item link to
   the bookmark's note.

Papers get the `paper` tag manually, or from a Karakeep rule matching
doi.org / arxiv.org URLs.
- Karakeep's rule engine can do this. v0.33.2's source has `urlContains`
  conditions and `or` groups.
- Rules are set in the UI and stored in Karakeep's database, not in this
  repo.

**Structure: chosen Karakeep lists are Zotero's collections** (decided by
the owner on 2026-10-16; built in 0.2.0, `mirror.py`). The structure for
papers is kept in Karakeep, and Zotero follows it one-way, every run.
Karakeep also keeps things that aren't papers, so only chosen lists are
mirrored.

- **Which lists:** `MIRROR_LISTS` in `k8s/research/paperbridge/paperbridge.env`
  names top-level Karakeep lists, comma-separated; empty means off. It is
  set to `Papers` (owner, 2026-10-16), with sublists `Digital twin`,
  `Forecasting` and `Research`. Zotero has the same tree.
  - Each named list becomes a top-level collection of the same name. Its
    manual sublists become subcollections, at any depth, created as they
    appear.
  - With one root list (e.g. `Papers`, with sublists for digital twin,
    forecasting, research), new topics need no repo change.
  - Adding or removing a root is a one-line edit plus apply. Both are
    expected to be rare.
- **Items:** each item paperbridge handed off sits, within the mirrored
  trees, in exactly the collections of its bookmarks' lists. Moving a
  bookmark moves the item within 15 minutes. Moving it inside a mirrored
  tree in Zotero is undone by the next run. Its collections outside the
  mirrored trees are left alone.
- **Never touched:**
  - items without a `Karakeep:` line, i.e. papers added in Zotero
    directly. Bookmarking one later links it by DOI, and from then on it
    follows Karakeep;
  - collections outside the mirrored trees, and collections already in
    the trash (the API lists them with `deleted: true`; the owner's
    library has one);
  - the tree of a root that's no longer mirrored, or whose Karakeep list
    is gone. It stays as it is; delete it by hand if wanted.
- **Removing:** inside a mirrored tree, a collection whose list is gone
  goes to Zotero's **trash** (`PATCH {"deleted": true}`, restorable), but
  only once it holds nothing at all: no items, not even trashed ones or
  papers added directly, and no subcollections. Items are never deleted.
- Collections are matched by their path of names, because Zotero
  collections have no field to hold a list ID. A renamed list therefore
  becomes a new collection, and the old one goes to the trash once
  emptied.
- Smart lists (saved searches) are skipped, because their contents shift
  on their own.
- Zotero has no manual order within a collection; it sorts by a column.
- Tested live on 2026-10-16 with throwaway lists: file, move, remove a
  list, rerun (no changes). The test collections are in Zotero's trash.

**Sync (step 6; built in 0.3.0, `sync.py`).** Comments and metadata sync
both ways. Files and structure don't (see the rule at the top).

- **State is needed.** To sync both ways, paperbridge has to know which
  side changed since the last sync.
  - v1 keeps a small snapshot per linked item: the last-synced values
    plus Zotero's item version.
  - It lives on a new ZFS dataset, `hddpool/k8s/paperbridge`. That needs
    a `host/00-prep.sh` run with the owner's sudo password.
  - Rejected: "newest whole item wins", with no state. It overwrites
    edits made on the other side, and paperbridge's own writes would make
    the two sides keep overwriting each other.
- **Metadata** (title, authors, date, abstract): a three-way merge per
  field, against the snapshot.
  - A field changed on one side is copied to the other.
  - A field changed on both sides: **Zotero wins**, since that's where
    bibliographic data gets curated.
- **Comments:** the Karakeep bookmark's note and one Zotero child note,
  "From Karakeep", stay in sync both ways.
  - paperbridge's own Zotero-link lines in the bookmark's note are left
    out.
  - Changed on both sides: keep both texts, with a conflict marker.
    Nothing is ever dropped.
- **Annotations** (v2, 0.4.0): Zotero highlights and their comments go
  one way, Zotero → Karakeep, into the bookmark's note. A Karakeep search
  then also finds highlighted passages.
  - The note reads: comments, then a `Highlights from Zotero:` section,
    then the Zotero links last.
  - Each entry is `- p. 3: "quoted text"`, with the comment on indented
    lines below, in reading order (`annotationSortIndex`). Sticky notes
    and other annotations without text show `- p. 7, note: comment`. An
    annotation with neither text nor comment is left out.
  - The two-way comment sync only sees the part above the section.
  - **No conflicts:** the section is rebuilt from Zotero every run, so
    anything typed inside it in Karakeep is undone.
  - **Nothing flows back.** Karakeep's own highlights are anchored to
    offsets in its text view of a page. A Zotero annotation needs a
    position on a PDF page, and there is no reliable mapping between the
    two. Karakeep's highlights stay searchable in Karakeep.
  - **Open problem** (owner, 2026-10-16): highlights are therefore
    one-way, unlike comments. It may be solvable later; it's unknown
    whether it will be. Ideas, none built:
    - Copy Karakeep highlights into Zotero as text, e.g. a section in the
      "From Karakeep" note. That's searchable in Zotero, but they would
      not be annotations on the PDF.
    - If Karakeep ever offers highlights on PDFs with page positions, map
      those to Zotero annotations.
  - Cost: annotations and attachments are read whole (`itemType=...`),
    but only when the library version moved since the last build. Each
    item's section is cached in `state.json`. A run that writes to
    Zotero moves the version itself, so the run after it re-reads once.
- **Read status:** Zotero → Karakeep. When the Zotero Reading List plugin
  marks a paper Read, archive the bookmark in Karakeep and add a
  read-date tag. This replaces the separate read-date CronJob idea.
  - The plugin (v1.7.1, checked in its source on 2026-10-16) writes
    `Read_Status: <name>` and `Read_Status_Date: <ISO time>` lines into
    Extra, replacing only its own lines.
  - `READ_STATUS` (default `Read`) is the status name that counts, since
    the plugin's names are configurable.
  - The tag is `READ_TAG_FORMAT` (`read-%Y-%m`) of `Read_Status_Date`:
    monthly, so the tag list stays short. Zotero keeps the exact date.
  - Leaving that status unarchives the bookmark and removes the tag.
    paperbridge remembers which tag it applied.

**Edge cases and intent.**

- **No PDF on the bookmark** (only the landing page was saved): try an
  open-access PDF. Otherwise leave it to "Find Available PDF" in Zotero
  desktop.
- **Two copies of each PDF is on purpose.** Highlights live in Zotero's
  database. Karakeep's copy lets Claude read the full paper through
  Karakeep's MCP server. The Zotero MCP server in web-API mode cannot reach
  WebDAV files.
- **Later, same service:** EPUB/OPDS.

**v0 as built** (`services/paperbridge/`):

- The `Extra` line is `Karakeep: https://karakeep.home.threadsafe.se/dashboard/preview/<bookmarkId>`.
  That is the bookmark's own link, not the page URL; the item's URL field
  already holds the page URL. A rerun finds the item by that line, so a
  crash between creating the item and tagging the bookmark doesn't make a
  duplicate.
- **Lookups read the whole library**: every top-level item, 100 per
  request, once per run that has bookmarks to hand off. Matching by DOI
  (the field, or a `DOI:` line in Extra) and by the `Karakeep:` line
  happens in paperbridge.
  - The API's quick search can't do it. Tested 2026-10-16: `q=` with
    `qmode=everything` matches title, creators, year, notes and full text,
    but neither DOI nor Extra.
  - For a large library, v1's incremental sync (`since=<library
    version>`) can replace the full read.
- Metadata order:
  1. An arXiv ID in the URL → `/search arXiv:<id>`.
  2. A doi.org link → `/search <doi>`.
  3. Otherwise `/web <url>`, then a DOI found in the URL.
  4. A bookmarked PDF: the arXiv ID or DOI in its text, accepted only if
     the found title also appears in that text.
- **Existing items**, so nothing is duplicated:
  1. A match by DOI.
  2. Otherwise a match by exact normalized title: the translated title,
     the title in a Zotero-style file name (`Author - Year - Title.pdf`,
     as when a PDF is taken out of Zotero), or Karakeep's title.
  3. Only when nothing was translated: the earliest library title in the
     first 1500 normalized characters of the PDF's text.
  - Titles under 20 characters never match.
  - A match gets a `Karakeep:` line added to its Extra, and our PDF only
    if it has none.
- arXiv preprints get their **v1 date** from arXiv's API, which fixes the
  step 3 gotcha.
- No metadata: retry each run until the bookmark is
  `METADATA_GRACE_HOURS` (24) old, then create a minimal item tagged
  `needs-metadata`.
- Link bookmarks Karakeep is still crawling wait, because a PDF link
  becomes an asset bookmark once crawled.
- PDF: paperbridge chooses the keys itself, sent with `"version": 0`. It
  writes `<KEY>.zip` and `<KEY>.prop` first, then creates the parent, its
  notes and the attachment in one API call, with `md5` and `mtime` set.
  Zotero's API allows that for WebDAV libraries.
  - The file name follows Zotero's default:
    `Author et al. - Year - Title.pdf`.
- The bookmark's note gets `zotero://select/library/items/<KEY>` and the
  web-library link. `in-zotero` is tagged last.

### PDF importer

`scripts/import_pdfs.py`: local PDFs into Karakeep, the entry point;
paperbridge then takes over as for any paper. One-way and additive, and
rerunnable: the owner downloads papers gradually.

- **Input:** a JSON manifest, one entry per PDF: path, DOI or arXiv ID,
  title, source URL, list path, tags, note. Any source can produce one; a
  source-specific builder stays out of this repo.
- Only PDFs: a paper that needed a manual download can't be a link.
- **Identity, so nothing is ever added twice:**
  - The ID is `doi:<DOI>`, else `arxiv:<ID>`, else `sha256:<file hash>`,
    written into the bookmark's note as `import-id: …`.
  - A PDF is present when a bookmark has its import ID, its DOI or arXiv
    ID in a URL or source URL (arXiv's `10.48550/arXiv.*` DOIs included),
    the same asset file name, or the same title (20+ characters).
  - Checked per file right before upload, so an interrupted run simply
    continues next time.
  - A bookmark it created but didn't finish (no `paper` tag) is finished.
    Two bookmarks with one import ID are reported, never touched.
- **Dry run by default**; `--apply --limit N` uploads.
- **Per new file:** upload the asset, create the bookmark with
  `sourceUrl` (the DOI link) and title, put it in its list (created if
  missing), add the other tags, and `paper` last.
- **Retries:** GET and PUT, and POSTs that are safe to repeat (tags,
  asset upload), retry for about 2 minutes. `POST /bookmarks` never
  repeats blindly: after a failure it re-reads Karakeep for the import ID
  first. A blind retry while Karakeep restarted made a duplicate
  (2026-10-21).
- **Big imports:** batches of 25, one paperbridge run after each, with
  the CronJob suspended meanwhile. Batches of 50 (~140 PDFs) OOMKilled
  Karakeep five times at 3Gi; it now has 8Gi.
- **Deliberately not done:** deletions, moves after import (organise in
  Karakeep), replacing an imported PDF.
- **Known gap:** an uncatalogued paper saved as two *different* files
  (preprint and publisher PDF) gives two bookmarks. Zotero still gets one
  item, since paperbridge matches by DOI or title.
- Uses paperbridge's Karakeep API key unless `.env` has
  `KARAKEEP_IMPORT_API_KEY`.

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
6. **paperbridge v1** (0.3.0, done): read status back, plus metadata and
   comments both ways. Lists → collections came early, in 0.2.0. Then
   **v2** (0.4.0): annotations → Karakeep.

## Repo layout (target)

```
CLAUDE.md
README.md                 # prerequisites + rebuild procedure
renovate.json             # weekly dependency-update PRs (see "Dependency updates")
.env.example              # secret names only
.gitignore                # .env, kubeconfig*, *.secret.yaml
host/
  00-prep.sh              # ZFS datasets + sanoid
  01-microk8s.sh          # MicroK8s + hardened Traefik ingress addon
  02-alerts.sh            # ZED → healthchecks.io (after the watchdog has run once)
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
  monitoring/             # the watchdog CronJob (see "Alerts")
scripts/secrets.sh        # .env → Kubernetes Secrets
images/translation-server/  # UPSTREAM_REF: which upstream commit we build
services/paperbridge/     # Python package + Dockerfile
.github/workflows/        # image builds → ghcr.io/mufflon/* (public, inherits repo visibility)
```

Config that a pod reads as environment variables at start-up belongs in a
kustomize `configMapGenerator` (e.g. `k8s/research/karakeep/karakeep.env`).
The generated name carries a content hash, so editing the file and
re-applying rolls the pod. A plain ConfigMap would change silently and need
a manual restart that the repo doesn't capture.

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

- Meilisearch: stay on Karakeep's pinned v1.41.0, or verify a newer server
  with Karakeep's client first.
