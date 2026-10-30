#!/usr/bin/env bash
# Host bootstrap (see README, "Rebuild"): MicroK8s + Traefik ingress addon, hardened.
# Idempotent, safe to re-run. On the server: sudo bash 01-microk8s.sh 2>&1 | tee 01-microk8s.log
set -euo pipefail

CHANNEL=1.36/stable
TRAEFIK_CHART=42.2.0                     # replaces the addon's own (39.0.8 in MicroK8s 1.36); Renovate proposes bumps
TRAEFIK_REPO=https://traefik.github.io/charts
DEFAULT_CERT=ingress/wildcard-home-tls   # written by the lego CronJob (k8s/ingress)
OWNER=${SUDO_USER:-mufflon}

step() { printf '\n==== %s ====\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

step "MicroK8s $CHANNEL"
# A changed CHANNEL (e.g. a Renovate PR) upgrades the cluster: one minor at a time.
if ! snap list microk8s >/dev/null 2>&1; then
  snap install microk8s --classic --channel="$CHANNEL"
elif [[ $(snap info microk8s | awk '/^tracking:/{print $2}') != "$CHANNEL" ]]; then
  snap refresh microk8s --channel="$CHANNEL"
fi
usermod -aG microk8s "$OWNER"
microk8s status --wait-ready --timeout 300 >/dev/null
echo "running: $(snap list microk8s | awk 'NR==2{print $2, $4}')"

step "Ingress addon: Traefik, wildcard certificate as the default"
# The addon installs its own chart version with its own values, and creates the default TLSStore
# (outside Helm). The Secret may not exist yet; Traefik serves its self-signed fallback until lego
# has run. On a cluster that already has the addon, this changes nothing.
microk8s enable ingress --default-ssl-certificate "$DEFAULT_CERT" </dev/null

step "Traefik chart $TRAEFIK_CHART: CRDs first, then the chart with our values"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
microk8s helm pull traefik --repo "$TRAEFIK_REPO" --version "$TRAEFIK_CHART" --untar --untardir "$tmp" </dev/null
# Helm never upgrades CRDs, so the chart's upgrade notes say to apply them first. Only traefik.io's:
# Traefik Hub isn't used, and the addon brings the Gateway API CRDs.
for crd in "$tmp"/traefik/crds/traefik.io_*.yaml; do
  microk8s kubectl apply --server-side --force-conflicts -f "$crd"
done
# The whole configuration, applied with --reset-values so nothing the addon set survives: its
# values use a key that chart 40+ rejects (providers.kubernetesIngressNginx). Below is the addon's
# configuration as of MicroK8s 1.36, in chart 42's key names, plus our changes, marked "ours".
cat > "$tmp/values.yaml" <<'EOF'
deployment:
  kind: DaemonSet
# Ours: replace in place. The chart's default surge rollout (maxSurge 1) deadlocks here: the new
# pod needs host ports 80/443 that the old pod still holds on the only node.
updateStrategy:
  rollingUpdate:
    maxSurge: 0
    maxUnavailable: 1
ports:
  web:
    port: 8000
    exposedPort: 80
    hostPort: 80
    protocol: TCP
    http:
      redirections:              # ours: HTTP redirects to HTTPS
        entryPoint:
          to: websecure
          scheme: https
          permanent: true
  websecure:
    port: 8443
    exposedPort: 443
    hostPort: 443
    protocol: TCP
# Ours: no dashboard. The addon's values expose an unauthenticated one on Host(dashboard.localhost),
# reachable by anyone on the LAN who sets that Host header.
api:
  dashboard: false
ingressRoute:
  dashboard:
    enabled: false
ingressClass:
  enabled: true
  isDefaultClass: false
  name: traefik
extraObjects:                    # `public` is the class our Ingresses use
  - apiVersion: networking.k8s.io/v1
    kind: IngressClass
    metadata:
      name: public
      annotations:
        ingressclass.kubernetes.io/is-default-class: "true"
    spec:
      controller: traefik.io/ingress-controller
  - apiVersion: networking.k8s.io/v1
    kind: IngressClass
    metadata:
      name: nginx
    spec:
      controller: traefik.io/ingress-controller
providers:
  kubernetesIngress:
    enabled: true
    allowEmptyServices: true
    publishedService:
      enabled: true
  kubernetesGateway:
    enabled: true
  kubernetesIngressNGINX:
    enabled: true
gateway:
  listeners:
    web:
      namespacePolicy:
        from: All
EOF
microk8s helm upgrade traefik "$tmp/traefik" -n ingress --reset-values -f "$tmp/values.yaml" </dev/null

step "Result"
microk8s kubectl -n ingress get daemonset,tlsstore,ingressroute 2>&1
