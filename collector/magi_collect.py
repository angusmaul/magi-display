#!/usr/bin/env python3
"""
MAGI display collector.

Polls the Proxmox cluster API and writes data.json next to the display page.
Deliberately cluster-scoped rather than node-scoped: /cluster/resources returns
every node in the cluster, so a node starts populating its panel the moment it
joins. No code change, no redeploy.

Config lives in /etc/magi/magi.env (mode 0600, root-owned):
    PVE_HOST=10.0.0.10
    PVE_TOKEN_ID=root@pam!magi-display
    PVE_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    PVE_CA=/etc/magi/pve-root-ca.pem

The token needs PVEAuditor on / — read-only. It cannot start, stop, or
reconfigure anything, which is the whole point: this box drives a screen.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

OUT = os.environ.get("MAGI_OUT", os.path.expanduser("~/magi/display/data.json"))
ENV = os.environ.get("MAGI_ENV", "/etc/magi/magi.env")
INTERVAL = int(os.environ.get("MAGI_INTERVAL", "5"))
CLUSTER_LABEL = os.environ.get("MAGI_CLUSTER", "MAGI")

# The console panel needs two things the rack panel does not: guest counts (its
# CODE field is the running-LXC count) and quorum state. Each is one extra API
# call per tick, so both sit behind this flag — a panel that doesn't display
# them costs the cluster nothing extra.
WANT_EXTRA = os.environ.get("MAGI_EXTRA", "0") == "1"

# Uptime Kuma status page. Fetched HERE rather than from the page because the
# panel is served from 127.0.0.1:8080 and Kuma is on another host — a browser
# fetch would be cross-origin. Doing it collector-side also means one failure
# path instead of two. Empty MAGI_KUMA_URL disables the whole section.
KUMA_URL   = os.environ.get("MAGI_KUMA_URL", "").rstrip("/")
KUMA_SLUG  = os.environ.get("MAGI_KUMA_SLUG", "main")
KUMA_BEATS = int(os.environ.get("MAGI_KUMA_BEATS", "48"))

# If your Kuma groups are named for priority tiers ("p1 — critical", "p2 — …"),
# they map straight through to the PRIORITY field rather than needing a
# threshold invented for the panel. Anything untiered scores B.
_TIERS = (("p1", "S+"), ("p2", "A+"), ("p3", "A-"))
_TIER_ORDER = {"S+": 0, "A+": 1, "A-": 2, "B": 3}


def tier_of(name):
    low = name.strip().lower()
    for prefix, tier in _TIERS:
        if low.startswith(prefix):
            return tier
    return "B"


# /cluster/tasks is polled far less often than everything else — the task log
# changes on the order of hours, and re-reading it every 5 s would be the one
# genuinely wasteful call in this collector.
TASK_INTERVAL = int(os.environ.get("MAGI_TASK_INTERVAL", "60"))

# Guest rootfs warning line, in percent. If you also run a monitoring system
# with a disk threshold, set this to the same number: two tools disagreeing
# about what counts as "too full" is worse on a wall panel than either alone.
GUEST_FS_WARN = float(os.environ.get("MAGI_GUEST_FS_WARN", "80"))
_task_cache = {"at": 0.0, "value": None}


def latest_task(cfg, ctx):
    """Most recent finished cluster task, as TYPE_STATUS (e.g. VZDUMP_OK).

    Deliberately just the latest one, with no stickiness on failures: a failed
    task scrolls out of view as soon as anything else runs. That is a known
    trade and was chosen over a sticky variant.
    """
    now = time.time()
    if now - _task_cache["at"] < TASK_INTERVAL:
        return _task_cache["value"]

    value = None
    try:
        # Newest first. Entries still running carry no status yet and would
        # render as a blank FILE, so they are skipped rather than shown.
        for task in api(cfg, ctx, "/cluster/tasks"):
            status = task.get("status")
            if not status:
                continue
            kind = "OK" if status == "OK" else "ERR"
            value = f"{task.get('type', 'task')}_{kind}".upper()
            break
    except Exception:
        # Leave the previous value in place rather than blanking the field on
        # one bad poll.
        return _task_cache["value"]

    _task_cache.update(at=now, value=value)
    return value


def kuma_get(path):
    url = f"{KUMA_URL}/api/status-page/{path}"
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.load(r)


def collect_kuma():
    """The 7 group monitors on the status page, with their recent beats.

    Uses the *published* status-page endpoints, which need no token — the same
    property that made the status page the right thing to put on a wall in the
    first place (no session to expire).
    """
    page = kuma_get(KUMA_SLUG)
    hb = kuma_get(f"heartbeat/{KUMA_SLUG}")
    beats_by_id = hb.get("heartbeatList") or {}
    uptime_by_id = hb.get("uptimeList") or {}

    services = []
    for group in page.get("publicGroupList") or []:
        for mon in group.get("monitorList") or []:
            mid = str(mon["id"])
            beats = beats_by_id.get(mid) or []
            last = beats[-1] if beats else None
            services.append({
                "id": mon["id"],
                "name": mon["name"],
                "tier": tier_of(mon["name"]),
                "up": bool(last and last.get("status") == 1),
                "ping": (last or {}).get("ping"),
                "up24": uptime_by_id.get(mid + "_24"),
                # oldest first, trimmed to what the panel actually draws
                "beats": [1 if b.get("status") == 1 else 0
                          for b in beats[-KUMA_BEATS:]],
            })
    return services

def make_ctx(cfg):
    """Verify against the PVE cluster CA — full chain AND hostname checking.

    PVE is self-signed, but it runs a real cluster CA and issues node certs
    carrying the node IP in subjectAltName, so connecting by IP verifies
    cleanly once that CA is trusted. Disabling verification would hand anyone
    on the LAN a MITM of the API token.
    """
    ca = cfg.get("PVE_CA", "/etc/magi/pve-root-ca.pem")
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def load_env(path):
    cfg = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except (FileNotFoundError, PermissionError):
        # Expected under systemd: the unit runs as an unprivileged user and the
        # env file is root-only, so systemd reads it via EnvironmentFile= and
        # hands the values down as environment variables instead. Reading the
        # file directly is only the fallback for running this by hand as root.
        pass
    return cfg


def api(cfg, ctx, path):
    url = f"https://{cfg['PVE_HOST']}:8006/api2/json{path}"
    req = urllib.request.Request(url)
    req.add_header(
        "Authorization",
        f"PVEAPIToken={cfg['PVE_TOKEN_ID']}={cfg['PVE_TOKEN_SECRET']}",
    )
    with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
        return json.load(r)["data"]


def collect(cfg, ctx):
    nodes = api(cfg, ctx, "/cluster/resources?type=node")

    # /cluster/resources has no loadavg; pull it per-node. One extra call per
    # online node, which at three nodes is trivial.
    for n in nodes:
        if n.get("status") == "online":
            try:
                st = api(cfg, ctx, f"/nodes/{n['node']}/status")
                n["loadavg"] = st.get("loadavg")

                # The PVE API exposes no sensor data at all, so there is no
                # temperature to be had here. Swap pressure is arguably the
                # more useful third vital anyway: it is the metric that tells
                # you a node is in trouble before it starts thrashing.
                sw = st.get("swap") or {}
                if sw.get("total"):
                    n["swap"] = {"used": sw.get("used", 0), "total": sw["total"]}
            except Exception:
                n["loadavg"] = None

    payload = {
        "cluster": CLUSTER_LABEL,
        "updated": int(time.time()),
        "alert": any(n.get("status") != "online" for n in nodes),
        "nodes": nodes,
    }

    if WANT_EXTRA:
        # `?type=vm` returns QEMU guests AND LXC containers; the per-item
        # `type` field is what separates them. Counted here rather than in the
        # page so the panel never has to know PVE's vocabulary.
        guests = api(cfg, ctx, "/cluster/resources?type=vm")
        payload["guests"] = len(guests)
        payload["lxcRunning"] = sum(
            1 for g in guests
            if g.get("type") == "lxc" and g.get("status") == "running"
        )

        # Guest filesystem pressure, per node. A container quietly filling its
        # own rootfs is one of the few failures that is both slow enough to
        # catch and bad enough to matter — and it rarely presents as a disk
        # problem. A full rootfs can break the DHCP client's lease file, so the
        # guest loses its reservation and the whole thing reads as a networking
        # fault instead.
        #
        # These numbers come out of the /cluster/resources call above, which the
        # collector already makes, so this costs no extra request. That is the
        # cheap reason to source it here. The real reason is that a monitoring
        # service usually runs *as a guest on the cluster it monitors*, and a
        # panel whose job is to say the cluster is in trouble must not depend on
        # something inside the cluster to tell it so — the same rule that says
        # this panel is served from the box driving the screen, not from a node.
        #
        # `worst` is emitted whether or not anything is over the line: it is the
        # useful number for a trend, and costs nothing. `over` is what the panel
        # renders, and stays empty almost always.
        fs = {}
        for g in guests:
            if g.get("type") != "lxc" or g.get("status") != "running":
                continue
            e = fs.setdefault(g.get("node"),
                              {"total": 0, "unsized": 0, "worst": None, "over": []})
            maxd = g.get("maxdisk") or 0
            if not maxd:
                # A guest reporting no size is not a guest at 0%. Count it
                # separately rather than letting it read as healthy — VMs
                # without a guest agent land here.
                e["unsized"] += 1
                continue
            pct = round(g.get("disk", 0) / maxd * 100, 1)
            entry = {"name": g.get("name"), "pct": pct}
            e["total"] += 1
            if e["worst"] is None or pct > e["worst"]["pct"]:
                e["worst"] = entry
            if pct >= GUEST_FS_WARN:
                e["over"].append(entry)
        for e in fs.values():
            e["over"].sort(key=lambda x: -x["pct"])
        payload["guestFs"] = fs
        payload["guestFsWarn"] = GUEST_FS_WARN

        # Quorum. /cluster/status is the ONLY endpoint that reports it, and it
        # reports it as a bare flag: there are no vote counts and the QDevice
        # is not listed (verified against the live cluster). So the panel says
        # HELD/LOST rather than inventing an n/m it cannot source.
        #
        # This field is load-bearing: the panel treats absent quorum as
        # critical, by design, so a collector that omits it puts the console
        # into a permanent EMERGENCY.
        status = api(cfg, ctx, "/cluster/status")
        cluster = next((i for i in status if i.get("type") == "cluster"), None)
        if cluster:
            payload["quorate"] = bool(cluster.get("quorate"))
            # Cluster members, so the panel can tell "a node is missing" from
            # "a node was never deployed" — a unit with no matching
            # cluster member renders dark rather than down.
            payload["expected"] = cluster.get("nodes")
        else:
            # No cluster entry means this is a standalone node, which is
            # trivially quorate. Without this the panel would sit in a
            # permanent alarm if the cluster were ever dissolved.
            payload["quorate"] = True

        payload["lastTask"] = latest_task(cfg, ctx)

    if KUMA_URL:
        try:
            services = payload["services"] = collect_kuma()
            payload["checksTotal"] = len(services)
            payload["checksUp"] = sum(1 for s in services if s["up"])
            down = [s for s in services if not s["up"]]
            payload["incidents"] = len(down)
            payload["priority"] = (
                min((s["tier"] for s in down), key=lambda t: _TIER_ORDER[t])
                if down else "—"
            )
        except Exception as e:
            # Explicit null, never an empty list: the panel must render its own
            # dead state for this section rather than a silently green service
            # list built from absent data. Same rule as the page's LINK LOST.
            payload["services"] = None
            payload["kumaError"] = f"{e.__class__.__name__}: {e}"

    return payload


def write(payload):
    # Atomic replace so the browser never fetches a half-written file.
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, OUT)


def main():
    cfg = load_env(ENV)
    # Environment wins over the file — that is the systemd path.
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("PVE_") and v})

    missing = [k for k in ("PVE_HOST", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET") if not cfg.get(k)]
    if missing:
        print(f"missing config in {ENV}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    ctx = make_ctx(cfg)

    while True:
        try:
            write(collect(cfg, ctx))
        except urllib.error.HTTPError as e:
            print(f"PVE API HTTP {e.code}: {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"collect failed: {e.__class__.__name__}: {e}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
