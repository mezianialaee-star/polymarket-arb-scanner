# Polymarket Basket Arbitrage Scanner

A read-only research tool that watches every multi-outcome market on Polymarket and measures
**how often a riskless "basket" trade exists, how much it is worth after fees, and how long it lasts.**

## The idea

In a multi-outcome event ("Who will win X?"), exactly one outcome resolves YES. Therefore:

- buying **1 YES share of every outcome** always pays exactly **$1**;
- buying **1 NO share of every outcome** always pays exactly **$(N − 1)**.

If the order books let you buy a whole basket for less than its payout — after fees and within the
exchange's order rules — the profit is locked in whatever happens. It is the same
"sum of the parts = the whole" logic as ETF arbitrage.

## What runs every 10 minutes (GitHub Actions)

There are about 6,000 eligible events and 65,000 outcomes, so reading every full order book every
10 minutes would mean ~130,000 books per scan. The scan works in two stages instead:

1. **List events** (Gamma API, rebuilt every hour, kept in the Actions cache) and keep only events
   where a basket is truly riskless (see *Safety rules*).
2. **Stage 1 — screen everything.** One `POST /prices` request per 250 outcomes gives the best bid and
   best ask of every YES token (~260 requests). A basket can only be profitable if the sum of its best
   asks is below its payout (fees and deeper levels only add cost), so every other event is ruled out.
   The NO best ask is `1 − YES best bid` (YES and NO share one order book).
3. **Stage 2 — decide on the candidates.** For the few events left, read the full order books of every
   YES and NO token (`POST /books`) and find the optimal basket size, after fees and order minimums.
   Only stage 2 can report an opportunity.
4. **Record** every opportunity, a summary of the scan, and every 3 hours a snapshot of how far each
   basket is from its payout.

Nothing is ever traded: the tool only reads public data.

## The math

Let `q` be the number of baskets, `P` the payout per basket (`1` for YES, `N − 1` for NO), and for leg `i`
let `c_i(q)` be the cost of buying `q` shares by walking its asks from the cheapest. Polymarket's
documented taker fee for one share bought at price `p` is `f(p) = rate · p(1 − p)`.

```
Profit(q) = q·P − Σ_i [ c_i(q) + fees_i(q) ]
```

The marginal cost of one more basket, `Σ_i [p_i + f(p_i)]`, can only go up as we walk the books
(asks are sorted and `p + f(p)` increases with `p` because `rate ≤ 0.25`). So `Profit` is **concave**
and the optimum is found greedily: keep buying while the next basket costs less than `P`.

Polymarket also rejects marketable buy orders under a minimum size (shares) and under $1 of value.
If the unconstrained optimum is below these minimums, concavity means the best allowed size is exactly
the minimum; the opportunity only counts if that size is still profitable.

All arithmetic uses exact decimals, so rounding can never create a fake profit.

## Safety rules (no fake arbitrage)

- Only **standard negative-risk** events (exactly one outcome resolves YES, complete list fixed at creation).
  **Augmented** events are excluded: outcomes can be added later and "Other" changes meaning.
- Every outcome must be tradable. An outcome is left out only if it already resolved NO (prices exactly 0 / 1).
- One negative-risk group per event; every market must belong to it.
- YES/NO token ids are read as documented (v1: `clobTokenIds`, v2: `positionIds`, index 0 = YES) and
  cross-checked against each order book's condition id and `neg_risk` flag.
- A YES basket is evaluated only if the displayed YES prices add up to at least 0.90 (complete set), and
  is recorded only after the event's full outcome list is re-confirmed with `GET /events/{id}`.
  (A NO basket stays riskless even if an outcome were missing: then every NO leg pays.)
- Unknown fee configuration ⇒ the highest documented rate (7%) is assumed.
- Edges above 5% are flagged for manual review.

## How it was verified

- **Automated tests** (`tests/`): official fee tables, the optimiser compared with a brute-force search on
  4,000 random order books, every eligibility rule, a proof by random testing that stage 1 never screens
  out a basket the full books show as profitable (3,000 random events, ~1,400 opportunities), and
  end-to-end runs against a fake Polymarket API (pagination, retries, refused ids, both possible meanings
  of the `/prices` sides, malformed data, blocked access, incomplete listings) on Python 3.11–3.14.
- **Live self-check** (`python3 -m arbscanner selfcheck`, run by the `verify` workflow at every change):
  on real data it lists every open event, runs a real two-stage scan, and checks the YES/NO mapping,
  condition ids, that stage-1 prices equal the full books, that the NO book mirrors the YES book exactly,
  the completeness of event listings, and that baskets cost about their payout or more, as theory predicts.
- **Every scan** re-checks itself: which `/prices` side is the ask is decided from the data (the best bid
  is always below the best ask; Polymarket's docs contradict each other on this), and stage-1 prices of the
  candidates are compared with their full books.

## Data layout

| Path | Content |
| --- | --- |
| `data/scans/YYYY-MM.csv` | one row per scan: coverage, candidates, books read, timings, checks, opportunities |
| `data/opportunities/YYYY-MM-DD.csv` | every opportunity: event, basket, best size, profit after fees, cost, link |
| `data/snapshots/YYYY-MM-DD/HH.csv.gz` | every 3 hours: top-of-book cost of every basket vs its payout |
| `state/universe.json.gz` | list of eligible events (Actions cache, not in git) |
| `report/` | results (built by the `analyze` workflow) |

## Results

See [`report/report.md`](report/report.md).

## Limitations

- Scans every 10 minutes: opportunities lasting seconds are missed and durations are approximate.
- Detection is not execution: each leg is a separate order and prices can move in between
  (sports markets also delay marketable orders by a few seconds).
- New events are picked up when the list is rebuilt (at most one hour later).
- Polymarket rounds fees per trade (differences below $0.00001).

## Run it

```bash
python3 -m arbscanner selfcheck          # live check on real data (~10 min: lists every event)
python3 -m arbscanner scan --data data   # one scan (the first one builds the list of events)
python3 -m unittest discover -s tests -t .
pip install -r analysis/requirements.txt && python3 analysis/analyze.py --data data --out report
```

The scanner itself uses only the Python standard library (tested on Python 3.11–3.14).
