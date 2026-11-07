#!/usr/bin/env python3
"""Startup report: for every stream in a Speaka.log, who started it, how long after bluetoothd launched, how fast the
PC's packets arrived at first, and when they first reached real time (v9.9.5).

The open question (docs/CHANGELOG.md, v9.9.5): AAC from Windows arrives at a third of real time until ~42 s after a
bluetoothd launch. This separates elapsed launch time from everything else, for the A-D matrix: run it on each test's
Speaka.log. Usage: tools/startup_report.py Speaka.log [--json]"""
import json
import re
import sys
from datetime import datetime

FULL = 46.875 * 0.9          # packets/s: 90% of 48 kHz AAC (1024 frames per packet)


def _t(stamp):
    return datetime.strptime(stamp, "%H:%M:%S.%f")


def report(lines):
    streams, launch, current = [], None, None
    for line in lines:
        m = re.match(r"(\d\d:\d\d:\d\d\.\d+) (.*)", line.strip())
        if not m:
            continue
        stamp, body = m.groups()
        if "loaded into bluetoothd" in body:
            launch, current = stamp, None
            continue
        start = re.search(r"V882_CONTROL .*msg=2 sig=7 active=1 by=(\w+)(?: since_launch_ms=(\d+))?", body)
        if start and launch:
            since = int(start.group(2)) if start.group(2) else int((_t(stamp) - _t(launch)).total_seconds() * 1000)
            current = {"start": stamp, "by": start.group(1), "since_launch_ms": since, "windows": [], "full_at_s": None}
            streams.append(current)
            continue
        if current and re.search(r"V882_CONTROL .*msg=2 sig=(9|8|10) active=0", body):
            current = None
            continue
        rx = re.search(r"V893_RX (\d+)ms forwarded=(\d+) .*bytes=(\d+)", body)
        if current and rx and int(rx.group(1)) > 1500:
            n, ms = int(rx.group(2)), int(rx.group(1))
            rate = n / ms * 1000.0
            at = round((_t(stamp) - _t(launch)).total_seconds(), 1)
            current["windows"].append({"at_s": at, "pkt_per_s": round(rate, 1), "bytes_per_pkt": int(rx.group(3)) // max(n, 1)})
            if current["full_at_s"] is None and rate >= FULL:
                current["full_at_s"] = at
    for s in streams:
        first = s["windows"][0]["pkt_per_s"] if s["windows"] else None
        s["first_pkt_per_s"] = first
        s["slow_start"] = first is not None and first < FULL
    return streams


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8", errors="replace") as f:
        streams = report(f.read().splitlines())
    if "--json" in argv:
        print(json.dumps(streams, indent=1))
        return 0
    print("start          by     after launch  first window   full rate from")
    for s in streams:
        first = "%5.1f/s" % s["first_pkt_per_s"] if s["first_pkt_per_s"] is not None else "   -   "
        full = "+%.1f s" % s["full_at_s"] if s["full_at_s"] is not None else "never"
        print("%s  %-5s  %8.1f s    %s %s  %s" % (s["start"], s["by"], s["since_launch_ms"] / 1000.0, first,
                                                ("SLOW" if s["slow_start"] else "    "), full))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
