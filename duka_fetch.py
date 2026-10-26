"""Pull Dukascopy NDX 1m week by week; on a failed week, retry day by day and skip bad days.
Usage: python3 duka_fetch.py 2019-01-01 2025-01-01 duka/oos"""
import subprocess, sys
from datetime import date, timedelta
from pathlib import Path

start, end, out = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]), Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)


def pull(a, b):
    r = subprocess.run(["npx", "-y", "dukascopy-node", "-i", "usatechidxusd", "-from", a.isoformat(), "-to", b.isoformat(),
                        "-t", "m1", "-f", "csv", "-dir", str(out), "-r", "3", "-rp", "2000"], capture_output=True, text=True)
    return "File saved" in r.stdout


skipped = []
d = start
while d < end:
    e = min(d + timedelta(days=7), end)
    if not pull(d, e):
        x = d
        while x < e:
            if x.weekday() != 5 and not pull(x, x + timedelta(days=1)):
                skipped.append(x.isoformat())
            x += timedelta(days=1)
    d = e
print("skipped days:", skipped)
