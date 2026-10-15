#!/usr/bin/env bash
# Create or update the cluster's Secrets from .env (gitignored). Run from your workstation:
#   scripts/secrets.sh            (uses $KUBECONFIG, default ~/.kube/homelab)
# A Secret is skipped, not failed, until every value it needs is filled in.
# Values never appear on a command line: they go through a temp file that is removed on exit.
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab}
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "no .env: copy .env.example to .env and fill it in" >&2; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Value of KEY in .env: text after '=', minus a trailing ' # comment' and surrounding blanks.
val() {
  grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- \
    | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' || true
}

# secret NAMESPACE NAME SECRET_KEY=ENV_KEY ...
secret() {
  local ns=$1 name=$2 file="$TMP/$1.$2.env" pair v missing=""
  shift 2
  for pair in "$@"; do
    v=$(val "${pair#*=}")
    [[ -n $v ]] || missing="$missing ${pair#*=}"
    printf '%s=%s\n' "${pair%%=*}" "$v" >> "$file"
  done
  if [[ -n $missing ]]; then echo "skip  $ns/$name (not set yet:$missing)"; return; fi
  kubectl get namespace "$ns" >/dev/null 2>&1 || kubectl create namespace "$ns" >/dev/null
  kubectl -n "$ns" create secret generic "$name" --from-env-file="$file" --dry-run=client -o yaml \
    | kubectl apply --server-side --field-manager=secrets-sh -f - >/dev/null
  echo "ok    $ns/$name"
}

# lego (k8s/ingress): Let's Encrypt account email + LoopiaAPI credentials for DNS-01.
secret ingress lego-credentials \
  LEGO_EMAIL=ACME_EMAIL LOOPIA_API_USER=LOOPIA_API_USER LOOPIA_API_PASSWORD=LOOPIA_API_PASSWORD
