"""Signal definitions for the multi-signal model.

Rule for every function here: it may only use data up to and including day i. The tests enforce this by
scrambling everything after day i and checking that nothing computed for day i changes.

Series are `array('d')` aligned to one shared trading calendar, with NaN where a stock has no data.
"""
import math
from array import array
from bisect import bisect_left, bisect_right

NAN = float("nan")


def isnan(x):
    return x != x


def new_array(n, fill=NAN):
    return array("d", [fill]) * n


# ---------------------------------------------------------------- price signals

def momentum_12_1(c, i):
    """Return from 12 months ago to 1 month ago (the latest month is skipped because it tends to reverse)."""
    if i < 252:
        return NAN
    a, b = c[i - 21], c[i - 252]
    if isnan(a) or isnan(b) or b <= 0:
        return NAN
    return a / b - 1


def trend_200d(c, i):
    """How far the close sits above its 200-day average."""
    if i < 199:
        return NAN
    total = sum(c[i - 199:i + 1])
    if isnan(total) or total <= 0:
        return NAN
    return c[i] / (total / 200) - 1


def high_52w(c, i):
    """Close relative to the 52-week high (0 = at the high, negative = below it)."""
    if i < 251:
        return NAN
    window = c[i - 251:i + 1]
    if isnan(sum(window)):
        return NAN
    top = max(window)
    return c[i] / top - 1 if top > 0 else NAN


def fwd_return(o, c, i, h):
    """Return from the NEXT day's open to the close h days after day i (what a trade placed after day i earns)."""
    k = i + h
    if k >= len(c):
        return NAN
    a, b = o[i + 1], c[k]
    if isnan(a) or isnan(b) or a <= 0:
        return NAN
    return b / a - 1


# ---------------------------------------------------------------- ranking helpers

def average_ranks(values):
    values = list(values)
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def rank_norm(values):
    """Cross-sectional rank scaled to roughly -0.5 .. +0.5 (ties share their average rank)."""
    n = len(values)
    return [(r - 0.5) / n - 0.5 for r in average_ranks(values)]


def spearman(x, y):
    """Rank correlation (the 'information coefficient'). NaN if undefined."""
    n = len(x)
    if n < 3:
        return NAN
    rx, ry = average_ranks(x), average_ranks(y)
    mid = (n + 1) / 2
    sxx = sum((a - mid) ** 2 for a in rx)
    syy = sum((b - mid) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return NAN
    return sum((a - mid) * (b - mid) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)


# ---------------------------------------------------------------- news buzz

def trading_day_counts(effective_counts, days):
    """Map {effective_calendar_day: count} onto the trading calendar.

    Weekend and holiday news lands on the next trading day, so every article is counted exactly once."""
    arr = array("d", [0.0]) * len(days)
    for d, n in effective_counts.items():
        k = bisect_left(days, d)
        if k < len(days):
            arr[k] += n
    return arr


def buzz_series(counts, base=60, win=3, min_rate=0.05):
    """Unusualness of the last `win` days of news vs the stock's own trailing `base`-day rate.

    A Poisson-style z-score: (recent - expected) / sqrt(expected + 1). NaN when the stock gets too little
    news (under `min_rate` articles/day) for the baseline to mean anything."""
    n = len(counts)
    z = new_array(n)
    prefix = [0.0]
    for x in counts:
        prefix.append(prefix[-1] + x)
    for i in range(base + win, n):
        recent = prefix[i + 1] - prefix[i + 1 - win]
        rate = (prefix[i + 1 - win] - prefix[i + 1 - win - base]) / base
        if rate < min_rate:
            continue
        expected = rate * win
        z[i] = (recent - expected) / math.sqrt(expected + 1.0)
    return z


class NewsBuzz:
    """News attention, discounted for stocks whose past buzz-driven moves tended to reverse.

    score(i) = buzz strength x direction of the 3-day price reaction x credibility(i)

    credibility(i) is built from this stock's own earlier "buzz events" (unusual news plus a >=1% 3-day
    move). Each event's follow-through is the benchmark-relative return over the next `horizon` days in the
    direction of the move. Only events whose outcome was already known by day i are used, and the average is
    shrunk toward zero when there are few events. Credibility runs 0 (consistent reversals) to 1 (consistent
    follow-through), and is 0.5 for a stock with no history.
    """

    def __init__(self, counts, opens, closes, bench_fwd, horizon, event_z=2.0, event_move=0.01, react_min=0.005,
                 min_gap=5, prior_events=4, full_credit=0.03):
        self.z, self.c = buzz_series(counts), closes
        self.react_min, self.prior_events, self.full_credit = react_min, prior_events, full_credit
        self.known_idx, self.cum = [], [0.0]
        last = -10 ** 9
        for e in range(3, len(closes)):
            z = self.z[e]
            if isnan(z) or z < event_z:
                continue
            base = closes[e - 3]
            if isnan(base) or isnan(closes[e]) or base <= 0:
                continue
            move = closes[e] / base - 1
            if abs(move) < event_move or e - last < min_gap:
                continue
            last = e
            fwd, bench = fwd_return(opens, closes, e, horizon), bench_fwd[e]
            if isnan(fwd) or isnan(bench):
                continue
            self.known_idx.append(e + horizon)           # the day this event's outcome becomes known
            self.cum.append(self.cum[-1] + (1 if move > 0 else -1) * (fwd - bench))

    def credibility(self, i):
        k = bisect_right(self.known_idx, i)
        mean = self.cum[k] / (k + self.prior_events)
        return 0.5 + 0.5 * max(-1.0, min(1.0, mean / self.full_credit))

    def score(self, i):
        z = self.z[i]
        if isnan(z) or z <= 0 or i < 3:
            return 0.0
        base = self.c[i - 3]
        if isnan(base) or isnan(self.c[i]) or base <= 0:
            return 0.0
        move = self.c[i] / base - 1
        if abs(move) < self.react_min:
            return 0.0
        return (min(z, 5.0) / 5.0) * (1 if move > 0 else -1) * self.credibility(i)


# ---------------------------------------------------------------- extra price signals

def reversal_1m(c, i):
    """Short-term reversal: last month's LOSERS score high (hypothesis: they bounce back)."""
    if i < 21:
        return NAN
    a, b = c[i], c[i - 21]
    if isnan(a) or isnan(b) or b <= 0:
        return NAN
    return -(a / b - 1)


class BenchStats:
    """Benchmark daily returns with prefix sums, shared by every stock's PriceExtras."""

    def __init__(self, bc):
        n = len(bc)
        self.rb = array("d", [0.0]) * n
        self.s = array("d", [0.0]) * (n + 1)
        self.s2 = array("d", [0.0]) * (n + 1)
        for k in range(n):
            if k >= 1 and not isnan(bc[k]) and not isnan(bc[k - 1]) and bc[k - 1] > 0:
                self.rb[k] = bc[k] / bc[k - 1] - 1
            self.s[k + 1] = self.s[k] + self.rb[k]
            self.s2[k + 1] = self.s2[k] + self.rb[k] ** 2


class PriceExtras:
    """Signals needing daily returns or volume, via prefix sums (O(1) per date). Uses data up to day i only.

    A window is used only if every day in it has data; otherwise the signal is NaN."""

    def __init__(self, c, v, bench):
        n = len(c)
        self.bench = bench
        mk = lambda: array("d", [0.0]) * (n + 1)
        self.ok, self.r, self.r2, self.rrb, self.vok, self.vs = mk(), mk(), mk(), mk(), mk(), mk()
        for k in range(n):
            good = k >= 1 and not isnan(c[k]) and not isnan(c[k - 1]) and c[k - 1] > 0
            r = c[k] / c[k - 1] - 1 if good else 0.0
            self.ok[k + 1] = self.ok[k] + (1 if good else 0)
            self.r[k + 1] = self.r[k] + r
            self.r2[k + 1] = self.r2[k] + r * r
            self.rrb[k + 1] = self.rrb[k] + r * bench.rb[k]
            vgood = not isnan(v[k])
            self.vok[k + 1] = self.vok[k] + (1 if vgood else 0)
            self.vs[k + 1] = self.vs[k] + (v[k] if vgood else 0.0)

    @staticmethod
    def _sum(p, lo, hi):
        return p[hi + 1] - p[lo]

    def resmom(self, i, long=252, skip=21):
        """Return from days [i-long+1, i-skip] left over after removing the part explained by the benchmark."""
        lo, hi = i - long + 1, i - skip
        if lo < 1:
            return NAN
        n = hi - lo + 1
        if self._sum(self.ok, lo, hi) < n:
            return NAN
        sr, srb = self._sum(self.r, lo, hi), self._sum(self.bench.s, lo, hi)
        cov = self._sum(self.rrb, lo, hi) - sr * srb / n
        var = self._sum(self.bench.s2, lo, hi) - srb * srb / n
        if var <= 1e-12:
            return NAN
        return sr - (cov / var) * srb

    def lowvol(self, i, win=60):
        """Negative daily-return volatility over the last `win` days (calm stocks score high)."""
        lo = i - win + 1
        if lo < 1 or self._sum(self.ok, lo, i) < win:
            return NAN
        sr = self._sum(self.r, lo, i)
        var = (self._sum(self.r2, lo, i) - sr * sr / win) / (win - 1)
        return -math.sqrt(max(var, 0.0))

    def volspike(self, i, recent=5, base=60):
        """log(average volume of the last 5 days / average volume of the 60 days before them)."""
        a, b = i - recent + 1, i - recent
        lo = b - base + 1
        if lo < 0 or self._sum(self.vok, a, i) < recent or self._sum(self.vok, lo, b) < base:
            return NAN
        r_avg, b_avg = self._sum(self.vs, a, i) / recent, self._sum(self.vs, lo, b) / base
        return math.log(r_avg / b_avg) if r_avg > 0 and b_avg > 0 else NAN
