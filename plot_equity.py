"""Equity curve + per-year stats from the day-by-day result CSVs (PDRA target).
Usage: python3 plot_equity.py   → writes results/equity.png and prints a yearly table."""
import re
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

FILES = [("Out-of-sample 2019–2024", "results/out_of_sample_2019_2024_days.csv"),
         ("In-sample 2025–2026", "results/in_sample_2025_2026_days.csv")]
PAT = re.compile(r"(?:stop|target|time) ([+-]\d+)")


def trades(path):
    d = pd.read_csv(path)
    rows = []
    for date, cell in zip(d["date"], d["pdra"]):
        if isinstance(cell, str):
            rows += [(pd.Timestamp(date), int(p)) for p in PAT.findall(cell)]
    return pd.DataFrame(rows, columns=["date", "pnl"])


def pf(s):
    loss = -s[s < 0].sum()
    return round(s[s > 0].sum() / loss, 2) if loss else float("inf")


fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150)
all_t = []
for label, path in FILES:
    t = trades(path)
    all_t.append(t)
    ax.plot(t["date"], t["pnl"].cumsum(), lw=1.4, label=f"{label}  ({len(t)} trades, PF {pf(t['pnl'])})")
ax.axhline(0, color="#999", lw=0.8)
ax.set_title("Session sweep → inverted FVG, frozen rules, $500 risk/trade (NDX 5m, costs included)")
ax.set_ylabel("Cumulative P&L ($)")
ax.legend(frameon=False, loc="upper left")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig("results/equity.png")

t = pd.concat(all_t)
print("| Year | Trades | Win % | Profit factor | Net P&L |\n|---|---|---|---|---|")
for y, g in t.groupby(t["date"].dt.year):
    print(f"| {y} | {len(g)} | {100 * (g.pnl > 0).mean():.0f}% | {pf(g.pnl)} | ${g.pnl.sum():+,.0f} |")
