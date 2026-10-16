#!/usr/bin/env bash
# Alerts, host part (see CLAUDE.md, "Alerts"): ZED, ZFS's event daemon, reports pool events (faulted
# or removed disks, I/O and data errors, scrubs and resilvers that end unhealthy) to the
# healthchecks.io check `zfs`, the one the watchdog (k8s/monitoring) also reports the pool state to.
# It does that through ZED's mail hook: ZED_EMAIL_PROG points at a small script that posts the
# notification as a failure ping. ZED's own rules decide what's worth a notification, at most one
# per pool and kind of event per hour.
#
# Needs the monitoring/healthchecks Secret (scripts/secrets.sh) and one watchdog run, which creates
# the check. Idempotent, safe to re-run. On the server:
#   sudo bash 02-alerts.sh 2>&1 | tee 02-alerts.log
# Test, afterwards: `sudo bash 02-alerts.sh test` makes a throwaway pool `alerttest` on two sparse
# files, faults one of them (ZED reports it at once; the watchdog sees the pool DEGRADED on its
# next run), waits 6 minutes and destroys it.
set -euo pipefail

ZED_RC=/etc/zfs/zed.d/zed.rc
HOOK=/usr/local/sbin/zed-healthchecks
PING_URL_FILE=/etc/zfs/zed.d/healthchecks-ping-url   # root-only: the URL is all it takes to ping
SLUG=zfs
TEST_POOL=alerttest

step() { printf '\n==== %s ====\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

step "The ping URL of healthchecks.io check '$SLUG'"
key=$(microk8s kubectl -n monitoring get secret healthchecks \
  -o jsonpath='{.data.HEALTHCHECKS_API_KEY}' | base64 -d)
[[ -n $key ]] || { echo "no monitoring/healthchecks Secret: run scripts/secrets.sh first" >&2; exit 1; }
url=$(curl -fsS -H @<(printf 'X-Api-Key: %s\n' "$key") \
  "https://healthchecks.io/api/v3/checks/?slug=$SLUG" | jq -r '.checks[0].ping_url // empty')
[[ -n $url ]] || { echo "no check '$SLUG' yet: apply k8s/ and let the watchdog run once" >&2; exit 1; }
install -m 0600 /dev/null "$PING_URL_FILE"
printf '%s\n' "$url" > "$PING_URL_FILE"
echo "saved to $PING_URL_FILE"

step "ZED's mail hook: $HOOK"
cat > "$HOOK" <<EOF
#!/bin/sh
# ZED_EMAIL_PROG, installed by homelab host/02-alerts.sh: posts a ZED notification to the
# healthchecks.io check '$SLUG' as a failure. ZED calls it as: $HOOK -s <subject> <address>,
# with the message on stdin.
{ printf '%s\n\n' "\${2:-ZED notification}"; cat; } \\
  | curl -fsS -m 30 --retry 3 -o /dev/null --data-binary @- "\$(cat $PING_URL_FILE)/fail"
EOF
chmod 0755 "$HOOK"

# KEY VALUE: set an active or commented-out line in zed.rc, or append one.
set_rc() {
  if grep -qE "^#?$1=" "$ZED_RC"; then
    sed -i -E "s|^#?$1=.*|$1=\"$2\"|" "$ZED_RC"
  else
    printf '%s="%s"\n' "$1" "$2" >> "$ZED_RC"
  fi
}
set_rc ZED_EMAIL_ADDR root     # unused by the hook, but ZED only mails when an address is set
set_rc ZED_EMAIL_PROG "$HOOK"
systemctl enable --now zfs-zed
systemctl restart zfs-zed
grep -E '^ZED_(EMAIL|NOTIFY)' "$ZED_RC"
echo "zfs-zed: $(systemctl is-active zfs-zed)"

if [[ ${1:-} == test ]]; then
  step "Test: throwaway pool $TEST_POOL with a faulted device"
  if zpool list -H "$TEST_POOL" >/dev/null 2>&1; then
    echo "$TEST_POOL already exists; refusing" >&2; exit 1
  fi
  dir=$(mktemp -d /var/tmp/alerttest.XXXXXX)
  cleanup() { zpool destroy "$TEST_POOL" 2>/dev/null || true; rm -rf "$dir"; }
  trap cleanup EXIT
  truncate -s 128M "$dir/a" "$dir/b"
  zpool create -o cachefile=none -m none "$TEST_POOL" mirror "$dir/a" "$dir/b"
  zpool offline -f "$TEST_POOL" "$dir/b"
  zpool status "$TEST_POOL"
  echo "ZED reports the fault now; the watchdog sees $TEST_POOL DEGRADED within 5 minutes."
  sleep 360
  cleanup
  trap - EXIT
  echo "destroyed $TEST_POOL; the watchdog's next run reports the pool healthy again"
fi
