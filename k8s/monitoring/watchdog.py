"""Homelab watchdog: one pass over the checks below, each reported to its own healthchecks.io check.

Runs every 5 minutes as a CronJob (k8s/monitoring/cronjob.yaml), on the stock Python image with
only the standard library. For every check it upserts the healthchecks.io check through the
Management API, so its period, grace and integrations are defined here, then pings it: success,
or /fail with what is wrong. healthchecks.io alerts on changes (up -> down, down -> up) through
the project's integrations. Detected problems don't fail the Job; only the watchdog's own errors do.

Without HEALTHCHECKS_API_KEY it only prints the results.
"""

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HC_API = "https://healthchecks.io/api/v3/checks/"
K8S_API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
KSTAT = "/host/zfs"                   # the host's /proc/spl/kstat/zfs, read-only
STALE_AFTER = "homelab/stale-after"   # CronJob annotation: alert without a success for this long

MINUTE, HOUR, DAY = 60, 3600, 86400
WEEK = 7 * DAY

# slug: (name, period, grace, description). The heartbeat is pinged last, so it goes down when
# runs stop or crash. The others are pinged every run as well, but only go down by silence after a
# week: a dead server is the heartbeat's to report.
CHECKS = {
    "zfs": ("ZFS pool", WEEK, DAY,
            "Pool state from the host's /proc/spl/kstat/zfs (watchdog), and ZFS events from ZED on "
            "the host (host/02-alerts.sh). hddpool's four disks share one USB enclosure."),
    "cronjobs": ("CronJobs", WEEK, DAY,
                 "Every CronJob: its last finished run failed, or it had no successful run within "
                 f"its {STALE_AFTER} annotation (paperbridge 1h, lego 26h)."),
    "workloads": ("Workloads", WEEK, DAY,
                  "Every Deployment and DaemonSet has its pods available. A rollout in progress "
                  "only counts once it is stuck."),
    "endpoints": ("Endpoints", WEEK, DAY,
                  "Karakeep, Zotero WebDAV and Jellyfin answer as expected (ENDPOINTS in "
                  "k8s/monitoring/watchdog.env)."),
    "certificate": ("Certificate", WEEK, DAY,
                    "The wildcard certificate Traefik serves. Fails with less than CERT_MIN_FRACTION "
                    "of its lifetime left; lego renews with a third left."),
    "heartbeat": ("Watchdog heartbeat", 5 * MINUTE, 10 * MINUTE,
                  "Pinged at the end of every watchdog run, every 5 minutes. Down: the server, the "
                  "cluster or the watchdog has stopped."),
}
RETRIED = ("workloads", "endpoints")  # checked again after RETRY_SECONDS, so blips don't alert


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_duration(value):
    """'90m', '26h', '2d'."""
    return timedelta(seconds=int(value[:-1]) * {"m": MINUTE, "h": HOUR, "d": DAY}[value[-1]])


def k8s(path):
    with open(f"{SA_DIR}/token") as f:
        token = f.read()
    request = urllib.request.Request(K8S_API + path, headers={"Authorization": f"Bearer {token}"})
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(request, context=context, timeout=20) as response:
        return json.load(response)["items"]


def check_zfs():
    states = {}
    for pool in sorted(os.listdir(KSTAT)):
        path = os.path.join(KSTAT, pool, "state")
        if os.path.isfile(path):
            with open(path) as f:
                states[pool] = f.read().strip()
    problems = [f"{pool}: not imported" for pool in os.environ["POOLS"].split() if pool not in states]
    problems += [f"{pool}: {state}" for pool, state in states.items() if state != "ONLINE"]
    return problems, ", ".join(f"{pool} {state}" for pool, state in states.items())


def finished(job):
    """The Job's Complete or Failed condition, or None while it runs."""
    for condition in job["status"].get("conditions", []):
        if condition["type"] in ("Complete", "Failed") and condition["status"] == "True":
            return condition
    return None


def check_cronjobs():
    runs = {}  # (namespace, cronjob) -> [(finish time, job name, condition)]
    for job in k8s("/apis/batch/v1/jobs"):
        condition = finished(job)
        for owner in job["metadata"].get("ownerReferences", []):
            if owner["kind"] == "CronJob" and condition:
                key = (job["metadata"]["namespace"], owner["name"])
                runs.setdefault(key, []).append(
                    (condition["lastTransitionTime"], job["metadata"]["name"], condition))
    problems, names = [], []
    for cronjob in k8s("/apis/batch/v1/cronjobs"):
        namespace, name = cronjob["metadata"]["namespace"], cronjob["metadata"]["name"]
        names.append(name)
        latest = max(runs.get((namespace, name), []), default=None)
        if latest and latest[2]["type"] == "Failed":
            _, job, condition = latest
            problems.append(f"{namespace}/{name}: run {job} failed "
                            f"({condition.get('reason', '?')}: {condition.get('message', '')})")
        limit = cronjob["metadata"].get("annotations", {}).get(STALE_AFTER)
        if limit:
            last = (cronjob.get("status", {}).get("lastSuccessfulTime")
                    or cronjob["metadata"]["creationTimestamp"])
            if now() - parse_time(last) > parse_duration(limit):
                suspended = ", and it is suspended" if cronjob["spec"].get("suspend") else ""
                problems.append(f"{namespace}/{name}: no successful run since {last} "
                                f"(expected within {limit}){suspended}")
    return problems, f"{len(names)} CronJobs fine: {', '.join(sorted(names))}"


def check_workloads():
    problems, count = [], 0
    for deployment in k8s("/apis/apps/v1/deployments"):
        wanted = deployment["spec"].get("replicas", 1)
        if wanted == 0:
            continue
        count += 1
        name = f"{deployment['metadata']['namespace']}/{deployment['metadata']['name']}"
        status = deployment["status"]
        conditions = {c["type"]: c for c in status.get("conditions", [])}
        progressing = conditions.get("Progressing", {})
        available = conditions.get("Available", {})
        if progressing.get("status") == "False":
            problems.append(f"{name}: rollout stuck ({progressing.get('reason')}: "
                            f"{progressing.get('message')})")
        elif (available.get("status") == "False"
              and progressing.get("reason") == "NewReplicaSetAvailable"):
            problems.append(f"{name}: {status.get('availableReplicas', 0)}/{wanted} available")
    for daemonset in k8s("/apis/apps/v1/daemonsets"):
        count += 1
        name = f"{daemonset['metadata']['namespace']}/{daemonset['metadata']['name']}"
        status = daemonset["status"]
        if status.get("numberAvailable", 0) < status.get("desiredNumberScheduled", 0):
            problems.append(f"{name}: {status.get('numberAvailable', 0)}/"
                            f"{status['desiredNumberScheduled']} available")
    return problems, f"{count} Deployments and DaemonSets available"


def check_endpoints():
    """ENDPOINTS: 'name method url expected-status' entries separated by ';'."""
    problems, names = [], []
    for entry in filter(None, (e.strip() for e in os.environ["ENDPOINTS"].split(";"))):
        name, method, url, expected = entry.split()  # messages keep {HOST_IP}, not the LAN address
        names.append(name)
        try:
            request = urllib.request.Request(
                url.replace("{HOST_IP}", os.environ.get("HOST_IP", "")), method=method)
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as e:
            status = e.code
        except OSError as e:  # refused, timed out, DNS, TLS verification
            problems.append(f"{name}: {url}: {getattr(e, 'reason', e)}")
            continue
        if status != int(expected):
            problems.append(f"{name}: {url} answered {status}, expected {expected}")
    return problems, f"{', '.join(names)} answered as expected"


def check_certificate():
    host = os.environ["CERT_HOST"]
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    start, end = (ssl.cert_time_to_seconds(cert[k]) for k in ("notBefore", "notAfter"))
    left = end - time.time()
    until = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d")
    summary = f"{host}: valid until {until}, {left / DAY:.0f} days left"
    if left < float(os.environ["CERT_MIN_FRACTION"]) * (end - start):
        return [f"{summary}; lego should have renewed it by now"], summary
    return [], summary


def run(check):
    try:
        return check()
    except Exception as e:  # a check that can't run is reported, not silent
        return [f"check failed: {type(e).__name__}: {e}"], ""


def report(api_key, slug, problems, summary):
    name, period, grace, description = CHECKS[slug]
    fields = {"name": name, "slug": slug, "timeout": period, "grace": grace, "desc": description,
              "tags": "homelab", "channels": "*", "unique": ["slug"]}
    upsert = urllib.request.Request(HC_API, data=json.dumps(fields).encode(), method="POST",
                                    headers={"X-Api-Key": api_key,
                                             "Content-Type": "application/json"})
    with urllib.request.urlopen(upsert, timeout=20) as response:
        ping_url = json.load(response)["ping_url"]
    body = "\n".join(problems) or summary
    ping = urllib.request.Request(ping_url + ("/fail" if problems else ""), data=body.encode(),
                                  method="POST")
    urllib.request.urlopen(ping, timeout=20).close()


def main():
    checks = {"zfs": check_zfs, "cronjobs": check_cronjobs, "workloads": check_workloads,
              "endpoints": check_endpoints, "certificate": check_certificate}
    results = {slug: run(check) for slug, check in checks.items()}
    if any(results[slug][0] for slug in RETRIED):
        time.sleep(int(os.environ["RETRY_SECONDS"]))
        results.update({slug: run(checks[slug]) for slug in RETRIED if results[slug][0]})
    failing = sum(1 for problems, _ in results.values() if problems)
    results["heartbeat"] = ([], f"{len(checks)} checks ran, {failing} failing")

    api_key = os.environ.get("HEALTHCHECKS_API_KEY", "")
    errors = 0
    for slug, (problems, summary) in results.items():
        print(f"{slug}: {'FAIL' if problems else 'ok'}: " + ("; ".join(problems) or summary))
        if not api_key:
            continue
        try:
            report(api_key, slug, problems, summary)
        except (OSError, KeyError, ValueError) as e:
            errors += 1
            print(f"{slug}: reporting to healthchecks.io failed: {e}", file=sys.stderr)
    if not api_key:
        print("HEALTHCHECKS_API_KEY is not set: nothing reported", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
