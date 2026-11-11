#!/usr/bin/env python3
"""Stop a running batch if the OpenRouter key's spend crosses a ceiling.

Usage: python3 -I analysis/spend_guard.py <run_dir> <usage_ceiling_usd> [--every 60]

Polls the key's cumulative usage (/api/v1/key) while the batch's status is
running; past the ceiling it sends SIGTERM to the batch worker's whole
process group (the worker and every `inspect eval` child), records why in
<run_dir>/spend_guard.txt, and exits. Episodes already finished keep their
logs; the ones cut off are lost, which is the point.
"""
import json
import os
import signal
import sys
import time
import urllib.request

run, ceiling = sys.argv[1], float(sys.argv[2])
every = int(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 60
env = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l and not l.startswith("#"))


def usage() -> float:
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer " + env["OPENROUTER_API_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=15))["data"]["usage"]


def state() -> str:
    try:
        return json.load(open(os.path.join(run, "status.json"))).get("state") or "running"
    except (OSError, ValueError):
        return "running"


note = os.path.join(run, "spend_guard.txt")
while state() in ("running", "launched", "starting"):
    try:
        u = usage()
    except Exception as e:  # a failed poll is not a reason to stop the batch
        time.sleep(every)
        continue
    if u >= ceiling:
        pid = int(open(os.path.join(run, "worker.pid")).read().split()[0])
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        open(note, "w").write(f"{time.strftime('%H:%M:%S')} stopped: key usage {u:.2f} >= ceiling {ceiling:.2f}\n")
        sys.exit(0)
    time.sleep(every)
open(note, "w").write(f"{time.strftime('%H:%M:%S')} batch ended on its own; ceiling {ceiling:.2f} not reached\n")
