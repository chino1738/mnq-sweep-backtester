"""Resample a Dukascopy 1m CSV into 5m and 15m CSVs (same format, ms timestamps)."""
import sys
import pandas as pd

src = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else "NDX"
d = pd.read_csv(src)
d.index = pd.to_datetime(d.timestamp, unit="ms", utc=True)
for rule, tag in (("5min", "5m"), ("15min", "15m")):
    r = d.resample(rule, label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    r.insert(0, "timestamp", (r.index.astype("int64") // 10**6))
    r.to_csv(f"{prefix}_{tag}.csv", index=False)
    print(tag, len(r), r.index.min(), r.index.max())
