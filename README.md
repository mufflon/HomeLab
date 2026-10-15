# HomeLab

Everything needed to rebuild the home server `minisforum` and its Kubernetes
services from scratch. Secrets are the one exception: their names are in
`.env.example`, and their values live only in your local `.env`.

The first stack is a research pipeline:

- **Karakeep**, the bookmark register.
- **Zotero** with our own **WebDAV** file storage.
- **paperbridge**, the glue that hands papers from Karakeep to Zotero.

Services are reachable on the home LAN only, under `*.home.threadsafe.se`
with a Let's Encrypt certificate. Remote access is the router's VPN. The
server itself runs no VPN.

Design and rules are in [CLAUDE.md](CLAUDE.md). Build status is in its
"Status" section. Steps marked *(planned)* below are not in the repo yet.

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
   - A LoopiaAPI user allowed to read, add and remove DNS records. It is
     used for certificate renewal.
4. **Router**
   - The VPN server for remote access.
   - **No** port forwards to this server.
   - DNS rebind protection, if any, must allow `home.threadsafe.se`.
5. **Accounts**
   - A Zotero account, and an API key with library read/write.
   - The Karakeep API key is created during the build (step 4).
6. **Your machine**
   - A clone of this repo, and `.env` filled in from `.env.example`.
   - `kubectl`, or just SSH to the server and use `microk8s kubectl`.

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
   `.env`. *(planned)*
5. **Everything else:** `kubectl apply -k k8s/`. *(planned)*
   - Namespaces, storage (StorageClass + PVs), the lego certificate
     CronJob, and all apps.
   - No Helm charts and no CRDs of our own, so a single apply is enough.
6. **First certificate:** run the CronJob once instead of waiting for its
   schedule:

   ```bash
   kubectl -n ingress create job lego-now --from=cronjob/lego
   ```

   *(planned)*
7. **First-run steps per app:**
   - Karakeep: create your account, set `DISABLE_SIGNUPS=true`, create an
     API key, add it to `.env`, then rerun `scripts/secrets.sh`.
   - Zotero desktop: Settings → Sync → File Syncing → WebDAV, URL
     `https://zotero-dav.home.threadsafe.se/`, then "Verify Server".

## What this does not cover

- **Off-site backup.** sanoid snapshots live on the same pool, so they
  protect against mistakes, not against losing the pool. Off-site copies
  (e.g. `syncoid` to another machine) are not set up.
- **The media library, Jellyfin and NFS.** They run on the host, outside
  Kubernetes, and are not managed here.
