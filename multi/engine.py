"""Walk-forward engine: several signals, each weighted by how well it has recently predicted returns.

At each monthly rebalance date i:
  1. Score every eligible stock on every signal, using data up to day i only.
  2. Weight each signal by its trailing rank correlation (IC) with returns that were ALREADY KNOWN at day i
     (IC observations from day j count only if j + horizon <= i). Negative IC gets zero weight, and the weights
     are shrunk toward equal. If no signal has a positive trailing IC, the model abstains (holds the benchmark).
  3. Hold the top-N stocks by combined score, equal weight, for `horizon` days. Periods never overlap, so the
     standard error is std/sqrt(n).
Strategies compared on identical dates: adaptive, equal_weight (same signals, fixed weights), only:<signal>.
"""
import math
import statistics
from array import array
from dataclasses import dataclass, field

from .features import (NAN, BenchStats, NewsBuzz, PriceExtras, fwd_return, high_52w, isnan, momentum_12_1, new_array,
                       rank_norm, reversal_1m, spearman, trading_day_counts, trend_200d)

PRICE_SIGNALS = {"momentum": momentum_12_1, "trend": trend_200d, "high52": high_52w, "reversal": reversal_1m}
EXTRA_SIGNALS = ("resmom", "lowvol", "volspike")          # need returns/volume: computed by PriceExtras
CLASSIC_SIGNALS = ("momentum", "trend", "high52")
NEW_PRICE_SIGNALS = ("reversal", "lowvol", "resmom", "volspike")
ALL_SIGNALS = CLASSIC_SIGNALS + NEW_PRICE_SIGNALS + ("news",)


@dataclass
class Params:
    horizon: int = 20
    top_n: int = 30
    ic_step: int = 5
    ic_window: int = 126
    min_ic_obs: int = 12
    shrink: float = 0.5
    min_price: float = 5.0
    min_dollar_volume: float = 300_000.0
    min_stocks: int = 100
    cost_bps: float = 10.0
    start_idx: int = 252
    min_pairs: int = 30
    signals: tuple = ("momentum", "trend", "high52")


class Panel:
    def __init__(self, days, opens, closes, vols, bench_open, bench_close, params, news=None):
        """opens/closes/vols: {symbol: array('d') aligned to `days`}; news: {symbol: {effective_day: count}}."""
        self.days, self.p = days, params
        self.syms = sorted(closes)
        self.o, self.c, self.v = opens, closes, vols
        self.bo, self.bc = bench_open, bench_close
        n = len(days)
        self.bench_fwd = new_array(n)
        for i in range(n - 1):
            self.bench_fwd[i] = fwd_return(bench_open, bench_close, i, params.horizon)
        self.buzz = {}
        if "news" in params.signals and news:
            for s in self.syms:
                if s in news:
                    counts = trading_day_counts(news[s], days)
                    self.buzz[s] = NewsBuzz(counts, opens[s], closes[s], self.bench_fwd, params.horizon)
        self._raw, self._ic = {}, {}
        self._bench_stats, self._extras = None, {}

    def extras(self, s):
        if self._bench_stats is None:
            self._bench_stats = BenchStats(self.bc)
        if s not in self._extras:
            self._extras[s] = PriceExtras(self.c[s], self.v[s], self._bench_stats)
        return self._extras[s]

    # ---------------------------------------------------------- per-date cross-section
    def eligible(self, i):
        p, out = self.p, []
        for s in self.syms:
            c = self.c[s]
            if isnan(c[i]) or c[i] < p.min_price or i + 1 >= len(self.days) or isnan(self.o[s][i + 1]):
                continue
            dv = [c[k] * self.v[s][k] for k in range(i - 19, i + 1) if not isnan(c[k] * self.v[s][k])]
            if len(dv) < 10 or statistics.median(dv) < p.min_dollar_volume:
                continue
            out.append(s)
        return out

    def raw_scores(self, i):
        """({signal: [score per eligible stock, NaN if unavailable]}, eligible symbols) for day i."""
        if i not in self._raw:
            syms = self.eligible(i)
            scores = {}
            for name in self.p.signals:
                if name == "news":
                    scores[name] = [self.buzz[s].score(i) if s in self.buzz else NAN for s in syms]
                elif name in EXTRA_SIGNALS:
                    scores[name] = [getattr(self.extras(s), name)(i) for s in syms]
                else:
                    f = PRICE_SIGNALS[name]
                    scores[name] = [f(self.c[s], i) for s in syms]
            self._raw[i] = (scores, syms)
        return self._raw[i]

    def ic_at(self, i):
        """{signal: rank correlation between day-i scores and the forward returns that followed} (NaN if thin)."""
        if i not in self._ic:
            scores, syms = self.raw_scores(i)
            fwd = [fwd_return(self.o[s], self.c[s], i, self.p.horizon) for s in syms]
            res = {}
            for name, sc in scores.items():
                pairs = [(a, b) for a, b in zip(sc, fwd) if not isnan(a) and not isnan(b)]
                if name == "news":
                    pairs = [(a, b) for a, b in pairs if a != 0.0]   # stocks with no buzz carry no information
                res[name] = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= self.p.min_pairs else NAN
            self._ic[i] = res
        return self._ic[i]

    # ---------------------------------------------------------- weights
    def trailing_ic(self, i):
        """Mean IC per signal over observations known by day i, and the number of observations used."""
        p = self.p
        last_j = i - p.horizon
        first_j = max(p.start_idx - 0, last_j - p.ic_window)
        js = [j for j in range(self._first_ic_day(), last_j + 1, p.ic_step) if j >= first_j]
        sums = {s: [] for s in p.signals}
        for j in js:
            for name, v in self.ic_at(j).items():
                if not isnan(v):
                    sums[name].append(v)
        return {s: (statistics.fmean(v) if v else NAN) for s, v in sums.items()}, {s: len(v) for s, v in sums.items()}

    def _first_ic_day(self):
        return self.p.start_idx - 0

    def weights_at(self, i):
        """(weights or None if abstaining/warming up, status) with status in 'ok', 'warmup', 'abstain'."""
        p = self.p
        mean_ic, n_obs = self.trailing_ic(i)
        active = [s for s in p.signals if n_obs[s] >= p.min_ic_obs]       # too little history: sits out at weight 0
        if not active:
            return None, "warmup"
        pos = {s: max(mean_ic[s], 0.0) for s in active}
        total = sum(pos.values())
        if total <= 0:
            return None, "abstain"
        k = len(active)
        w = {s: 0.0 for s in p.signals}
        for s in active:
            w[s] = (1 - p.shrink) * pos[s] / total + p.shrink / k
        return w, "ok"

    # ---------------------------------------------------------- portfolio
    def portfolio(self, i, weights):
        """Top-N symbols by weighted sum of cross-sectional ranks (a missing score counts as neutral)."""
        scores, syms = self.raw_scores(i)
        if len(syms) < self.p.min_stocks:
            return []
        combined = [0.0] * len(syms)
        for name, w in weights.items():
            if w <= 0:
                continue
            col = scores[name]
            idx = [k for k, x in enumerate(col) if not isnan(x)]
            if len(idx) < self.p.min_pairs:
                continue
            for k, r in zip(idx, rank_norm([col[k] for k in idx])):
                combined[k] += w * r
        order = sorted(range(len(syms)), key=lambda k: -combined[k])
        return [syms[k] for k in order[:self.p.top_n]]

    def period_return(self, i, held):
        rets = [fwd_return(self.o[s], self.c[s], i, self.p.horizon) for s in held]
        rets = [r for r in rets if not isnan(r)]
        return statistics.fmean(rets) if rets else NAN

    # ---------------------------------------------------------- run
    def run(self, progress=print):
        """Evaluate every strategy on the same non-overlapping rebalance dates. Returns a list of row dicts."""
        p, n = self.p, len(self.days)
        rows, i, done = [], p.start_idx, 0  # equal_weight / only:<s> use all signals from the first date
        cost = 2 * p.cost_bps / 1e4
        strategies = ["adaptive", "equal_weight"] + [f"only:{s}" for s in p.signals]
        while i + p.horizon < n:
            bench = self.bench_fwd[i]
            w_adapt, status = self.weights_at(i)
            if status != "warmup" and not isnan(bench):
                for strat in strategies:
                    if strat == "adaptive":
                        weights, abstain = w_adapt, w_adapt is None
                    elif strat == "equal_weight":
                        weights, abstain = {s: 1.0 / len(p.signals) for s in p.signals}, False
                    else:
                        name = strat.split(":", 1)[1]
                        weights, abstain = {s: (1.0 if s == name else 0.0) for s in p.signals}, False
                    if abstain:
                        rows.append(dict(day=self.days[i], strategy=strat, port=bench, bench=bench, active=0.0,
                                         held=0, status="abstain", weights=None))
                        continue
                    held = self.portfolio(i, weights)
                    r = self.period_return(i, held) if held else NAN
                    if isnan(r):
                        continue
                    net = r - cost
                    rows.append(dict(day=self.days[i], strategy=strat, port=net, bench=bench, active=net - bench,
                                     held=len(held), status="ok",
                                     weights=dict(weights) if strat == "adaptive" else None))
            done += 1
            if done % 6 == 0:
                progress(f"   evaluated {done} rebalance dates (through {self.days[i]})")
            i += p.horizon
        return rows


# ------------------------------------------------------------------ reporting

def summarize(rows, strategy, lo=None, hi=None):
    """n, mean/median active return, % beating benchmark, standard error, mean weights (adaptive only)."""
    sel = [r for r in rows if r["strategy"] == strategy and (lo is None or r["day"] >= lo)
           and (hi is None or r["day"] < hi)]
    act = [r["active"] for r in sel]
    n = len(act)
    if n == 0:
        return dict(n=0)
    sd = statistics.stdev(act) if n > 1 else NAN
    return dict(n=n, mean=statistics.fmean(act), median=statistics.median(act),
                beat=sum(a > 0 for a in act) / n, se=sd / math.sqrt(n) if n > 1 else NAN,
                abstained=sum(r["status"] == "abstain" for r in sel),
                port=statistics.fmean(r["port"] for r in sel))


def _fmt(s):
    if not s.get("n"):
        return "no periods"
    se = "n/a" if isnan(s["se"]) else f"{s['se'] * 100:.1f}%"
    return (f"n={s['n']:>3}  mean {s['mean'] * 100:+6.2f}% ±{se:<6} median {s['median'] * 100:+6.2f}%  "
            f"beat {s['beat'] * 100:3.0f}%  abstained {s['abstained']}")


def mean_weights(rows, lo=None, hi=None):
    ws = [r["weights"] for r in rows if r["strategy"] == "adaptive" and r["weights"]
          and (lo is None or r["day"] >= lo) and (hi is None or r["day"] < hi)]
    if not ws:
        return {}
    return {k: statistics.fmean(w[k] for w in ws) for k in ws[0]}


def format_report(rows, params, benchmark, eval_start=None):
    strategies = ["adaptive", "equal_weight"] + [f"only:{s}" for s in params.signals]
    periods = [("ALL PERIODS", None, None)]
    if eval_start:
        periods = [(f"TRAIN (before {eval_start})", None, eval_start), (f"HOLDOUT ({eval_start} on)", eval_start, None)]
    out = [f"Active return per {params.horizon}-trading-day period vs {benchmark} (top {params.top_n} stocks, "
           f"{params.cost_bps:g} bps per side, non-overlapping periods; ± is one standard error)", ""]
    for title, lo, hi in periods:
        out.append(title)
        for st in strategies:
            out.append(f"  {st:<16} {_fmt(summarize(rows, st, lo, hi))}")
        w = mean_weights(rows, lo, hi)
        if w:
            out.append("  adaptive average weights: " + ", ".join(f"{k} {v:.2f}" for k, v in w.items()))
        out.append("")
    best = {}
    for st in strategies[2:]:
        s = summarize(rows, st, *(periods[-1][1:]))
        if s.get("n"):
            best[st] = s["mean"]
    ad = summarize(rows, "adaptive", *(periods[-1][1:]))
    if best and ad.get("n"):
        top = max(best, key=best.get)
        out.append(f"Verdict test (last block): adaptive {ad['mean'] * 100:+.2f}% vs best single signal "
                   f"{top} {best[top] * 100:+.2f}%. The combined model only earns its complexity if it beats its "
                   f"best single signal on data it never saw, with a gap larger than the ± shown.")
        out.append("Reminder: the stock list is today's survivors, so every number here is biased upward.")
    return "\n".join(out)


def rows_to_csv(rows, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "strategy", "port", "bench", "active", "held", "status"])
        for r in rows:
            w.writerow([r["day"], r["strategy"], f"{r['port']:.5f}", f"{r['bench']:.5f}", f"{r['active']:.5f}",
                        r["held"], r["status"]])


def build_panel(store, benchmark, params, news=None):
    """Load bars from the store and align everything to the benchmark's trading calendar."""
    bench_rows = store.load_bars(benchmark)
    if not bench_rows:
        raise SystemExit(f"No bars stored for benchmark {benchmark}. Run the download step first.")
    days = [d for d, *_ in bench_rows]
    index = {d: k for k, d in enumerate(days)}
    n = len(days)

    def aligned(rows):
        o, c, v = new_array(n), new_array(n), new_array(n)
        for d, ro, rc, rv in rows:
            k = index.get(d)
            if k is not None:
                o[k], c[k], v[k] = ro, rc, rv
        return o, c, v

    bo, bc, _ = aligned(bench_rows)
    opens, closes, vols = {}, {}, {}
    for s in store.kept_symbols():
        if s in (benchmark, "SPY", "IWM"):
            continue
        opens[s], closes[s], vols[s] = aligned(store.load_bars(s))
    return Panel(days, opens, closes, vols, bo, bc, params, news)
