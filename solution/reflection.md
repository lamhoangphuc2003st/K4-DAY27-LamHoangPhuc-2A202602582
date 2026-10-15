# Reflection

**Results:** practice 50.0 (TPR 1.0, FPR 0), public 48.18 (TPR 1.0, FPR 0, −1.82 from the
240/220 overage), private **32.2** (TPR 0.648, FPR 0.007, no overage).

**Approach.** The payload metadata is the same for clean and faulty events, so there is no
free filter. Every event gets exactly one metered call, to the cheapest tool that can see its
fault. A missed fault costs about 50/n_faulty points and one call costs about 20/budget, so
skipping calls to save budget never paid off. Each published `mean ± 3σ` baseline is turned
back into (μ, σ), and each metric becomes a z-score, so the thresholds follow the baseline
instead of being hard-coded numbers. The rules:
- two-sided metrics (row count, mean amount) alert at |z| ≥ 3; one-sided metrics (null rate,
  staleness, contract freshness, lineage runtime, doc age, centroid shift) alert at about 2.5σ;
- composite checks (sum of z², chi-square style) catch batches where several metrics drift
  together while each one stays inside its band;
- structural checks: any contract violation, freshness above the declared SLA, a missing
  `raw.orders`/`raw.customers` upstream, or zero downstream consumers;
- feature skew uses 0.65σ, because the published 0.41 sits inside real clean variance (~0.47).

**Hardest faults.** Subtle `data_batch` faults were the hardest. A mean amount of 88.5
(z ≈ 2.3) looked like a distribution shift on practice, where clean σ was about 1.9. On public
it turned out to be ordinary clean variance (the true σ matches the baseline's ~3.0). A
single-metric rule in that range gives false alarms, and a composite needs several metrics to
move. Shifts that are subtle on one metric only are close to impossible to separate. Subtle
embedding drift (centroid shift ≈ 0.040 against a clean maximum of ≈ 0.039) and corpus
staleness (age 48 against a clean maximum of 41) sit right on the edge of the clean
distribution. The private TPR of 0.65 shows that most misses are these near-boundary
instances. My thresholds were set where clean data stopped, not where faults started.

**What I'd change.** The scoring is asymmetric: a TP is worth about 0.5/n_faulty and an FP
costs about 0.3/n_clean. With about 3× more clean events than faulty ones, alerting is
worth it once P(fault) passes roughly 15–20%. I tuned too conservatively toward FPR = 0
(private FPR came in at 0.7%). On another pass I would:
1. lower the one-sided cuts to about 2.0–2.2σ and the composite cut to a chi-square p of
   about 0.05, and accept a few FPs for a larger TPR gain;
2. use `ctx.state` to keep running robust statistics (median/MAD) per metric and blend them
   with the baseline. That gives each run its own clean σ, which matters because the
   practice σ for mean amount was misleading;
3. add CUSUM/EWMA per metric in `ctx.state`, so a small shift that persists across several
   consecutive batches builds up enough evidence to alert;
4. use `spend_so_far()` only as a guard. Full coverage fits private's budget, so cost was
   never the binding constraint there.
