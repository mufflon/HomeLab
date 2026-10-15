#!/usr/bin/env bash
# Host bootstrap (see README, "Rebuild"): MicroK8s + Traefik ingress addon, hardened.
# Idempotent, safe to re-run. On the server: sudo bash 01-microk8s.sh 2>&1 | tee 01-microk8s.log
set -euo pipefail

CHANNEL=1.36/stable
TRAEFIK_CHART=39.0.8                     # the version MicroK8s 1.36's addon pins; bump deliberately
DEFAULT_CERT=ingress/wildcard-home-tls   # written by the lego CronJob (k8s/ingress)
OWNER=${SUDO_USER:-mufflon}

step() { printf '\n==== %s ====\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

step "MicroK8s $CHANNEL"
snap list microk8s >/dev/null 2>&1 || snap install microk8s --classic --channel="$CHANNEL"
usermod -aG microk8s "$OWNER"
microk8s status --wait-ready --timeout 300 >/dev/null
echo "running: $(snap list microk8s | awk 'NR==2{print $2, $4}')"

step "Ingress addon: Traefik, wildcard certificate as the default"
# The Secret may not exist yet; Traefik serves its self-signed fallback until lego has run.
microk8s enable ingress -V "$TRAEFIK_CHART" --default-ssl-certificate "$DEFAULT_CERT" </dev/null

step "Harden: no dashboard route, HTTP redirects to HTTPS"
# The addon's values expose an unauthenticated dashboard on Host(dashboard.localhost),
# reachable by anyone on the LAN who sets that Host header.
microk8s helm upgrade traefik traefik/traefik -n ingress --version "$TRAEFIK_CHART" --reuse-values \
  --set ingressRoute.dashboard.enabled=false \
  --set ports.web.redirections.entryPoint.to=websecure \
  --set ports.web.redirections.entryPoint.scheme=https \
  --set ports.web.redirections.entryPoint.permanent=true </dev/null

step "Result"
microk8s kubectl -n ingress get daemonset,tlsstore,ingressroute 2>&1
