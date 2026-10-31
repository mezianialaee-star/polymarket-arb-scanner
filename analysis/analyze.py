"""Turn the collected data into numbers and charts for the write-up.

    pip install pandas matplotlib
    python analysis/analyze.py --data data --out report

Writes report/report.md, report/summary.json and a few PNG charts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

MIN_PROFIT = 0.01  # an "opportunity" must be worth at least one cent


def load_csvs(pattern_dir: Path, pattern: str) -> pd.DataFrame:
    files = sorted(pattern_dir.glob(pattern)) if pattern_dir.exists() else []
    frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in files if f.stat().st_size > 0]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def num(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")  # empty strings become NaN
    return df


def build_episodes(opps: pd.DataFrame, ok_scan_times: list) -> pd.DataFrame:
    """Group detections of the same (event, basket) seen on consecutive successful scans."""
    if opps.empty:
        return pd.DataFrame(columns=["event_id", "basket", "start", "end", "n_scans", "max_profit",
                                     "max_size", "median_roi_pct", "title", "category", "n_outcomes", "end_date",
                                     "outcomes", "url"])
    index = {t: i for i, t in enumerate(sorted(ok_scan_times))}
    opps = opps[opps["scan_time"].isin(list(index))].copy()
    opps["scan_idx"] = opps["scan_time"].map(index)
    rows = []
    for (event_id, basket), g in opps.sort_values("scan_idx").groupby(["event_id", "basket"], sort=False):
        current = None
        for _, r in g.iterrows():
            if current is not None and r["scan_idx"] == current["last_idx"] + 1:
                current["last_idx"] = r["scan_idx"]
                current["end"] = r["scan_time"]
                current["n_scans"] += 1
                current["profits"].append(r["exec_profit"])
                current["sizes"].append(r["exec_size"])
                current["rois"].append(r["roi_pct"])
                continue
            if current is not None:
                rows.append(current)
            current = {
                "event_id": event_id, "basket": basket, "start": r["scan_time"], "end": r["scan_time"],
                "last_idx": r["scan_idx"], "n_scans": 1, "profits": [r["exec_profit"]],
                "sizes": [r["exec_size"]], "rois": [r["roi_pct"]], "title": r["event_title"],
                "category": r["category"], "n_outcomes": r["n_outcomes"], "url": r["url"],
                "end_date": r.get("end_date", ""),  # when the basket pays: for the yearly return
                "outcomes": r.get("outcomes", ""),  # YES baskets: check by hand that they cover everything
            }
        if current is not None:
            rows.append(current)
    ep = pd.DataFrame(rows)
    ep["max_profit"] = ep["profits"].apply(max)
    ep["max_size"] = ep["sizes"].apply(max)
    ep["median_roi_pct"] = ep["rois"].apply(lambda v: float(pd.Series(v).median()))
    return ep.drop(columns=["profits", "sizes", "rois", "last_idx"])


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)
    data, out = Path(args.data), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    scans = load_csvs(data / "scans", "*.csv")
    opps = load_csvs(data / "opportunities", "*.csv")
    snaps = load_csvs(data / "snapshots", "*/*.csv.gz")

    summary: dict = {}
    if scans.empty:
        (out / "report.md").write_text("# Results\n\nNo scan data yet.\n", encoding="utf-8")
        (out / "summary.json").write_text(json.dumps({"scans": 0}, indent=2), encoding="utf-8")
        print("no data yet")
        return 0

    scans = num(scans, ["screened_events", "universe_events", "universe_markets", "candidate_events",
                        "events_evaluated", "duration_s", "stage1_s", "stage2_s", "http_requests",
                        "opportunities_yes", "opportunities_no", "check_tokens", "check_ask_equal",
                        "check_bid_equal", "check_mirror_equal"])
    scans["t"] = pd.to_datetime(scans["scan_time"], utc=True)
    ok = scans[scans["status"] == "ok"].sort_values("t")
    first, last = scans["t"].min(), scans["t"].max()
    interval_min = float(ok["t"].diff().dt.total_seconds().median() / 60) if len(ok) > 1 else float("nan")

    def med(col):
        return float(ok[col].median()) if len(ok) and col in ok and ok[col].notna().any() else 0.0

    def share(col):
        if col not in ok or "check_tokens" not in ok:
            return None
        total = float(ok["check_tokens"].sum())
        return round(float(ok[col].sum()) / total, 4) if total else None

    refreshed = ok[ok["universe_refreshed"] == "1"] if "universe_refreshed" in ok else ok.iloc[0:0]
    summary.update({
        "period_start": str(first), "period_end": str(last),
        "days": round((last - first).total_seconds() / 86400, 2),
        "scans": int(len(scans)), "scans_ok": int(len(ok)),
        "scans_failed": int((scans["status"] != "ok").sum()),
        "failure_kinds": {k: int(v) for k, v in scans.loc[scans["status"] != "ok", "status"].value_counts().items()},
        "median_scan_interval_min": round(interval_min, 1) if interval_min == interval_min else None,
        "median_events_per_scan": med("screened_events"),
        "median_outcomes_per_scan": med("universe_markets"),
        "median_candidate_events_per_scan": med("candidate_events"),
        "median_scan_duration_s": med("duration_s"),
        "median_stage1_s": med("stage1_s"),
        "median_stage2_s": med("stage2_s"),
        "median_requests_per_scan": med("http_requests"),
        "listing_refreshes": int(len(refreshed)),
        "listing_complete_share": round(float((refreshed["gamma_complete"] == "1").mean()), 4) if len(refreshed) else None,
        "stage1_vs_books_ask_equal": share("check_ask_equal"),
        "stage1_vs_books_bid_equal": share("check_bid_equal"),
        "no_book_mirror_equal": share("check_mirror_equal"),
    })

    if not opps.empty:
        opps = num(opps, ["exec_profit", "exec_size", "exec_cost", "roi_pct", "net_edge_top", "n_outcomes"])
        opps = opps[(opps["suspicious"] != "1") & (opps["exec_profit"] >= MIN_PROFIT)]
        # YES baskets count only if the full outcome list was confirmed during the scan.
        opps = opps[(opps["basket"] != "YES") | (opps["verified"] == "1")]
    episodes = build_episodes(opps, list(ok["scan_time"]))
    if not episodes.empty:
        episodes["duration_min_est"] = episodes["n_scans"] * (interval_min if interval_min == interval_min else 10)
    summary.update({
        "detections": int(len(opps)),
        "episodes": int(len(episodes)),
        "episodes_yes": int((episodes["basket"] == "YES").sum()) if len(episodes) else 0,
        "episodes_no": int((episodes["basket"] == "NO").sum()) if len(episodes) else 0,
        "episodes_profit_ge_1": int((episodes["max_profit"] >= 1).sum()) if len(episodes) else 0,
        "episodes_profit_ge_10": int((episodes["max_profit"] >= 10).sum()) if len(episodes) else 0,
        "total_profit_usd": round(float(episodes["max_profit"].sum()), 2) if len(episodes) else 0.0,
        "median_profit_usd": round(float(episodes["max_profit"].median()), 2) if len(episodes) else 0.0,
        "median_duration_min": round(float(episodes["duration_min_est"].median()), 1) if len(episodes) else 0.0,
        "share_scans_with_opportunity": round(float(opps["scan_time"].nunique() / max(len(ok), 1)), 4) if len(opps) else 0.0,
    })

    if not snaps.empty and "yes_cost" in snaps:
        snaps = num(snaps, ["yes_cost", "no_cost", "yes_fee_top", "no_fee_top", "n_outcomes"])
        # YES baskets of events whose outcome list looks incomplete are never arbitrage: leave them out.
        yes_rows = snaps[snaps["yes_cost"].notna() & (snaps["yes_guarded"] != "1")]
        yes_cost = yes_rows["yes_cost"]
        no_rows = snaps[snaps["no_cost"].notna() & (snaps["n_outcomes"] > 1)]
        no_cost = (no_rows["no_cost"] / (no_rows["n_outcomes"] - 1)).dropna()
        # Of the baskets priced below their payout at the best asks, how many could really be
        # executed after fees and order minimums (same scans)?
        cheap_yes = yes_rows[yes_rows["yes_cost"] < 1]
        cheap_no = no_rows[no_rows["no_cost"] < no_rows["n_outcomes"] - 1]
        snap_times = set(snaps["scan_time"])
        exec_at_snaps = opps[opps["scan_time"].isin(list(snap_times))] if len(opps) else opps
        n_cheap = len(cheap_yes) + len(cheap_no)
        summary.update({
            "baskets_below_payout_at_snapshots": int(n_cheap),
            "baskets_executable_at_snapshots": int(len(exec_at_snaps)),
            "executable_share_of_below_payout": round(len(exec_at_snaps) / n_cheap, 4) if n_cheap else None,
        })
        summary.update({
            "snapshots": int(snaps["scan_time"].nunique()),
            "snapshot_rows": int(len(snaps)),
            "events_monitored": int(snaps["event_id"].nunique()),
            "yes_basket_cost_median": round(float(yes_cost.median()), 4) if len(yes_cost) else None,
            "yes_basket_cost_below_1_share": round(float((yes_cost < 1).mean()), 4) if len(yes_cost) else None,
            "yes_basket_cost_within_1pct_share": round(float((yes_cost < 1.01).mean()), 4) if len(yes_cost) else None,
            "no_basket_cost_ratio_median": round(float(no_cost.median()), 4) if len(no_cost) else None,
        })
        if len(yes_cost):
            fig, ax = plt.subplots(figsize=(7, 4))
            ax.hist(yes_cost.clip(upper=1.5), bins=60, color="#4C72B0")
            ax.axvline(1.0, color="black", linestyle="--", linewidth=1)
            ax.set_xlabel(r"Cost of buying YES on every outcome at the best asks (\$) — the basket pays \$1")
            ax.set_ylabel("event snapshots")
            ax.set_title(r"How far multi-outcome baskets are from \$1")
            fig.tight_layout()
            fig.savefig(out / "basket_cost_distribution.png", dpi=150)
            plt.close(fig)

    if len(episodes):
        ep = episodes.copy()
        ep["day"] = pd.to_datetime(ep["start"], utc=True).dt.date
        per_day = ep.groupby(["day", "basket"]).size().unstack(fill_value=0)
        fig, ax = plt.subplots(figsize=(7, 4))
        per_day.plot(kind="bar", stacked=True, ax=ax)
        ax.set_ylabel("new arbitrage episodes")
        ax.set_title("Arbitrage episodes per day")
        fig.tight_layout()
        fig.savefig(out / "episodes_per_day.png", dpi=150)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(ep["max_profit"], bins=40, color="#55A868")
        ax.set_xscale("log")
        ax.set_xlabel(r"Best executable profit of the episode (\$, after fees)")
        ax.set_ylabel("episodes")
        ax.set_title("Size of arbitrage opportunities")
        fig.tight_layout()
        fig.savefig(out / "profit_distribution.png", dpi=150)
        plt.close(fig)

        ep.sort_values("max_profit", ascending=False).to_csv(out / "episodes.csv", index=False)

    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Results",
        "",
        f"Monitoring period: {summary['period_start'][:16]} → {summary['period_end'][:16]} UTC "
        f"({summary['days']} days, {summary['scans_ok']} successful scans every ~{summary['median_scan_interval_min']} min).",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Multi-outcome events monitored | {summary.get('events_monitored', 'n/a')} |",
        f"| Median events / outcomes checked per scan | {summary['median_events_per_scan']:.0f} / "
        f"{summary['median_outcomes_per_scan']:.0f} |",
        f"| Median scan time (stage 1 / stage 2) | {summary['median_scan_duration_s']:.0f} s "
        f"({summary['median_stage1_s']:.0f} s / {summary['median_stage2_s']:.0f} s) |",
        f"| Median full-book reads per scan (candidate events) | {summary['median_candidate_events_per_scan']:.0f} |",
        f"| Stage-1 best ask = full-book best ask (candidates) | {_pct(summary.get('stage1_vs_books_ask_equal'))} |",
        f"| Arbitrage episodes (≥ ${MIN_PROFIT} after fees and order minimums) | {summary['episodes']} "
        f"(YES {summary['episodes_yes']}, NO {summary['episodes_no']}) |",
        f"| Episodes worth ≥ $1 / ≥ $10 | {summary['episodes_profit_ge_1']} / {summary['episodes_profit_ge_10']} |",
        f"| Sum of best executable profit per episode | ${summary['total_profit_usd']} |",
        f"| Median profit per episode | ${summary['median_profit_usd']} |",
        f"| Median observed duration | ~{summary['median_duration_min']} min |",
        f"| Share of scans with at least one opportunity | {summary['share_scans_with_opportunity']:.1%} |",
        f"| Median cost of a YES basket (payout $1) | {summary.get('yes_basket_cost_median', 'n/a')} |",
        f"| Share of event snapshots where the YES basket cost < $1 (before fees) | {_pct(summary.get('yes_basket_cost_below_1_share'))} |",
        f"| Baskets below their payout that were executable after fees and order minimums | "
        f"{summary.get('baskets_executable_at_snapshots', 'n/a')} of {summary.get('baskets_below_payout_at_snapshots', 'n/a')} "
        f"({_pct(summary.get('executable_share_of_below_payout'))}) |",
        "",
    ]
    if len(episodes):
        lines += ["## Largest episodes", "", "| Event | Basket | Outcomes | Best profit ($) | Seen on scans | Link |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for _, r in episodes.sort_values("max_profit", ascending=False).head(10).iterrows():
            lines.append(f"| {r['title']} | {r['basket']} | {r['n_outcomes']} | {r['max_profit']:.2f} | {r['n_scans']} | {r['url']} |")
        lines.append("")
    yes_eps = episodes[episodes["basket"] == "YES"] if len(episodes) else episodes
    if len(yes_eps):
        lines += ["## YES baskets to check by hand", "",
                  "A YES basket pays $1 only if its outcomes cover every possibility. Read the rules of each "
                  "event: if some result is not listed, it was not an arbitrage.", "",
                  "| Event | Outcomes | Best profit ($) | Ends | Link |", "| --- | --- | --- | --- | --- |"]
        for _, r in yes_eps.sort_values("max_profit", ascending=False).iterrows():
            lines.append(f"| {r['title']} | {str(r.get('outcomes', ''))[:200]} | {r['max_profit']:.2f} | "
                         f"{str(r.get('end_date', ''))[:10]} | {r['url']} |")
        lines.append("")
    for img in ("episodes_per_day.png", "profit_distribution.png", "basket_cost_distribution.png"):
        if (out / img).exists():
            lines += [f"![{img}]({img})", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
