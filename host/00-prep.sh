#!/usr/bin/env bash
# Step 0 host prep (see CLAUDE.md): ZFS datasets for Kubernetes storage + sanoid snapshots.
# Idempotent, safe to re-run. On the server: sudo bash 00-prep.sh 2>&1 | tee 00-prep.log
set -euo pipefail

PARENT=hddpool/k8s
# Dataset -> owner uid:gid. Must match runAsUser/runAsGroup in the app's manifest.
declare -A OWNER=(
  [zotero-webdav]=10001:10001   # hacdias/webdav: single binary, runs as non-root
  [meilisearch]=10002:10002     # single binary, runs as non-root
  [certs]=10003:10003           # lego: single binary, runs as non-root
  [karakeep]=0:0                # s6-overlay /init needs root
)
declare -A MODE=([certs]=0700)  # private keys; everything else 0750
SNAPSHOTTED=(zotero-webdav karakeep certs)   # meilisearch is rebuildable from Karakeep
SANOID_CONF=/etc/sanoid/sanoid.conf
MARKER="# managed by homelab/host/00-prep.sh"

step() { printf '\n==== %s ====\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
if [[ -f $SANOID_CONF ]] && ! grep -qF "$MARKER" "$SANOID_CONF"; then
  echo "$SANOID_CONF exists and is not managed by this script; refusing to overwrite" >&2
  exit 1
fi

step "Parent dataset $PARENT (Kubernetes storage only)"
zfs list -H "$PARENT" >/dev/null 2>&1 || zfs create "$PARENT"
zfs set compression=lz4 atime=off "$PARENT"

step "Per-app datasets"
for app in "${!OWNER[@]}"; do
  ds=$PARENT/$app
  zfs list -H "$ds" >/dev/null 2>&1 || zfs create "$ds"
  mp=$(zfs get -H -o value mountpoint "$ds")
  chown "${OWNER[$app]}" "$mp"
  chmod "${MODE[$app]:-0750}" "$mp"
done
# Zotero appends zotero/ to the WebDAV URL and expects the directory to exist.
dav_owner=${OWNER[zotero-webdav]}
install -d -o "${dav_owner%%:*}" -g "${dav_owner##*:}" -m 0750 \
  "$(zfs get -H -o value mountpoint "$PARENT/zotero-webdav")/zotero"

step "sanoid snapshots"
# </dev/null: under sudo-rs with output piped (| tee), apt is not in the
# terminal's foreground group and gets stopped (SIGTTOU) when it touches the tty.
command -v sanoid >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sanoid </dev/null
install -d -m 0755 /etc/sanoid
{
  echo "$MARKER -- edit the script, not this file"
  for app in "${SNAPSHOTTED[@]}"; do
    printf '[%s/%s]\n\tuse_template = k8s_data\n\n' "$PARENT" "$app"
  done
  cat <<'EOF'
[template_k8s_data]
	frequently = 0
	hourly = 36
	daily = 30
	monthly = 6
	yearly = 0
	autosnap = yes
	autoprune = yes
EOF
} > "$SANOID_CONF"
systemctl enable --now sanoid.timer
sanoid --take-snapshots   # first snapshots now; also proves the config parses

step "Result"
zfs list -r -o name,used,avail,compression,atime,mountpoint "$PARENT"
ls -ln "$(zfs get -H -o value mountpoint "$PARENT")"
zfs list -H -t snapshot -r -o name "$PARENT"
echo "sanoid.timer: $(systemctl is-active sanoid.timer)"
