"""
Data Siege defense.

Strategy
--------
* Payload metadata is constant across clean and faulty events, so every event
  gets exactly ONE metered call (the cheapest tool that can see its fault).
  A missed fault costs ~50/n_faulty points while one call costs ~20/budget
  points, so skipping calls to save budget is never worth it.
* ctx.baseline publishes mean +/- 3 sigma bounds. We turn each bound back into
  a (mu, sigma) pair and score every metric as a z-score, so the thresholds
  move with the published baseline instead of being hard-coded numbers.
* Large deviations on one metric -> alert (classic threshold check).
  Several moderate deviations at once -> alert via a composite
  (sum of squared z-scores, chi-square style), which catches subtle faults
  whose individual metrics still sit inside the 3-sigma band.
* Scoring asymmetry: a TP is worth ~0.5/n_faulty, an FP costs ~0.3/n_clean.
  With ~3x more clean than faulty events, alerting pays off once the fault
  probability passes roughly 15-20%, so thresholds sit near 2.5-2.8 sigma
  rather than at the very edge of the clean distribution.
"""
import math

from api import Verdict


# Typical clean centres for metrics whose baseline only publishes an upper
# bound (mu + 3 sigma). sigma is derived from the baseline at runtime.
NULL_RATE_MU = 0.0045
STALENESS_MU = 4.2
FRESHNESS_MU = 5.0
LINEAGE_DURATION_MU = 4150.0
EMBED_SHIFT_MU = 0.02
DOC_AGE_MU = 26.0
EXPECTED_UPSTREAM = {"raw.orders", "raw.customers"}

# Decision thresholds (in sigma units unless stated otherwise).
SINGLE_Z = 3.0            # two-sided metrics (rows, mean_amount): published bound
# One-sided metrics only fail in one direction, so the same false-alarm rate
# allows a lower cut. Observed clean values stay under ~2 sigma.
ONE_SIDED_Z = 2.5
# chi-square, 4 dims (rows, mean_amount, null_rate, staleness): ~p=0.017.
# One metric at ~2.3 sigma alone is ordinary clean variance; three metrics
# drifting together by ~2 sigma each is not.
DATA_COMPOSITE = 12.0
CONTRACT_FRESH_Z = 2.5
LINEAGE_DUR_Z = 2.5
FEATURE_SIGMA_THRESHOLD = 0.65   # clean max ~0.47, subtle skew starts >1.5
EMBED_SHIFT_Z = 2.5
DOC_AGE_Z = 2.4
EMBED_COMPOSITE = 7.5     # chi-square, 2 dims: ~p=0.025


def register(ctx):
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


# ---------------------------------------------------------------- helpers

def _band(lo, hi):
    """(mu, sigma) from a published mean +/- 3 sigma band."""
    return (lo + hi) / 2.0, (hi - lo) / 6.0


def _upper(mu, upper_bound):
    """sigma from a published mu + 3 sigma upper bound."""
    return max((upper_bound - mu) / 3.0, 1e-9)


def _z(x, mu, sigma):
    return (x - mu) / sigma


def _num(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _bump(ctx, key):
    stats = ctx.state.setdefault("alerts", {})
    stats[key] = stats.get(key, 0) + 1


def _alert(ctx, pillar, reason, confidence=1.0):
    _bump(ctx, pillar)
    return Verdict(alert=True, pillar=pillar, reason=reason, confidence=confidence)


def _quiet(pillar, reason=""):
    return Verdict(alert=False, pillar=pillar, reason=reason)


# ------------------------------------------------------- data_batch: checks

def data_batch_scores(profile, b):
    """Per-metric z-scores (positive = bad direction for one-sided metrics)."""
    rows_mu, rows_sd = _band(b["row_count_min"], b["row_count_max"])
    amt_mu, amt_sd = _band(b["mean_amount_min"], b["mean_amount_max"])
    null_sd = _upper(NULL_RATE_MU, b["null_rate_max"])
    stale_sd = _upper(STALENESS_MU, b["staleness_min_max"])

    null_rate = profile.get("null_rate") or {}
    return {
        "rows": _z(_num(profile.get("row_count"), rows_mu), rows_mu, rows_sd),
        "mean_amount": _z(_num(profile.get("mean_amount"), amt_mu), amt_mu, amt_sd),
        # one-sided: fewer nulls / fresher data is never a fault
        "null_rate": max(0.0, _z(_num(null_rate.get("customer_id"), NULL_RATE_MU), NULL_RATE_MU, null_sd)),
        "staleness": max(0.0, _z(_num(profile.get("staleness_min"), STALENESS_MU), STALENESS_MU, stale_sd)),
    }


def data_batch_decision(profile, b):
    z = data_batch_scores(profile, b)
    worst = max(z, key=lambda k: abs(z[k]))
    for k in ("rows", "mean_amount"):
        if abs(z[k]) >= SINGLE_Z:
            return True, f"{k} z={z[k]:.2f}"
    for k in ("null_rate", "staleness"):
        if z[k] >= ONE_SIDED_Z:
            return True, f"{k} z={z[k]:.2f}"
    composite = sum(v * v for v in z.values())
    if composite >= DATA_COMPOSITE:
        return True, f"composite={composite:.1f} (worst {worst} z={z[worst]:.2f})"
    return False, f"composite={composite:.1f}"


def check_data_batch(payload, ctx):
    profile = ctx.tools.batch_profile(payload["batch_id"])
    if not isinstance(profile, dict) or "error" in profile:
        return _quiet("checks", "profile unavailable")
    alert, reason = data_batch_decision(profile, ctx.baseline)
    return _alert(ctx, "checks", reason) if alert else _quiet("checks", reason)


# ----------------------------------------- contract_checkpoint: contracts

def contract_decision(diff, payload, b):
    violations = diff.get("violations") or []
    if violations:
        return True, "violations: " + ",".join(map(str, violations))
    delay = _num(diff.get("freshness_delay_min"))
    sla = (payload.get("declared_sla") or {}).get("freshness_min")
    if sla is not None and delay > _num(sla, float("inf")):
        return True, f"freshness {delay} > declared SLA {sla}"
    fresh_sd = _upper(FRESHNESS_MU, b["freshness_delay_max_min"])
    z = _z(delay, FRESHNESS_MU, fresh_sd)
    if z >= CONTRACT_FRESH_Z:
        return True, f"freshness z={z:.2f}"
    return False, f"freshness z={z:.2f}"


def check_contract_checkpoint(payload, ctx):
    diff = ctx.tools.contract_diff(payload["contract_id"], payload["checkpoint_batch_id"])
    if not isinstance(diff, dict) or "error" in diff:
        return _quiet("contracts", "diff unavailable")
    alert, reason = contract_decision(diff, payload, ctx.baseline)
    return _alert(ctx, "contracts", reason) if alert else _quiet("contracts", reason)


# ------------------------------------------------------ lineage_run: lineage

def lineage_decision(slice_, payload, b):
    upstream = set(slice_.get("actual_upstream") or [])
    declared = {i.get("name") for i in payload.get("inputs") or [] if isinstance(i, dict)}
    expected = EXPECTED_UPSTREAM | declared
    missing = expected - upstream
    if missing:
        return True, "missing upstream: " + ",".join(sorted(missing))
    downstream = slice_.get("actual_downstream_count")
    if downstream is not None and _num(downstream, 1) <= 0:
        return True, "orphan output (no downstream consumers)"
    dur_sd = _upper(LINEAGE_DURATION_MU, b["lineage_duration_ms_max"])
    z = _z(_num(slice_.get("duration_ms"), LINEAGE_DURATION_MU), LINEAGE_DURATION_MU, dur_sd)
    if z >= LINEAGE_DUR_Z:
        return True, f"runtime z={z:.2f}"
    return False, f"runtime z={z:.2f}"


def check_lineage_run(payload, ctx):
    slice_ = ctx.tools.lineage_graph_slice(payload["run_id"])
    if not isinstance(slice_, dict) or "error" in slice_:
        return _quiet("lineage", "slice unavailable")
    alert, reason = lineage_decision(slice_, payload, ctx.baseline)
    return _alert(ctx, "lineage", reason) if alert else _quiet("lineage", reason)


# --------------------------------------- feature_materialization: ai_infra

def feature_decision(drift, b):
    sigma = drift.get("mean_shift_sigma")
    if sigma is None:
        train_std = _num(drift.get("train_std"), 1.0) or 1.0
        sigma = abs(_num(drift.get("serve_mean")) - _num(drift.get("train_mean"))) / train_std
    sigma = _num(sigma)
    # The published bound (~0.41) is tighter than the clean stream's real
    # spread (~0.47); real skew starts well above 1. Never go below baseline.
    threshold = max(FEATURE_SIGMA_THRESHOLD, b["feature_mean_shift_sigma_max"])
    if sigma >= threshold:
        return True, f"mean_shift_sigma={sigma:.3f}"
    return False, f"mean_shift_sigma={sigma:.3f}"


def check_feature_materialization(payload, ctx):
    drift = ctx.tools.feature_drift(payload["feature_view"], payload["batch_id"])
    if not isinstance(drift, dict) or "error" in drift:
        return _quiet("ai_infra", "drift unavailable")
    alert, reason = feature_decision(drift, ctx.baseline)
    return _alert(ctx, "ai_infra", reason) if alert else _quiet("ai_infra", reason)


# ------------------------------------------------ embedding_batch: ai_infra

def embedding_decision(drift, b):
    shift_sd = _upper(EMBED_SHIFT_MU, b["embedding_centroid_shift_max"])
    age_sd = _upper(DOC_AGE_MU, b["corpus_avg_doc_age_days_max"])
    z_shift = max(0.0, _z(_num(drift.get("centroid_shift")), EMBED_SHIFT_MU, shift_sd))
    z_age = max(0.0, _z(_num(drift.get("avg_doc_age_days")), DOC_AGE_MU, age_sd))
    if z_shift >= EMBED_SHIFT_Z:
        return True, f"centroid_shift z={z_shift:.2f}"
    if z_age >= DOC_AGE_Z:
        return True, f"doc age z={z_age:.2f}"
    composite = z_shift ** 2 + z_age ** 2
    if composite >= EMBED_COMPOSITE:
        return True, f"embedding composite={composite:.1f}"
    return False, f"shift z={z_shift:.2f} age z={z_age:.2f}"


def check_embedding_batch(payload, ctx):
    drift = ctx.tools.embedding_drift(payload["corpus"], payload["chunk_batch_id"])
    if not isinstance(drift, dict) or "error" in drift:
        return _quiet("ai_infra", "drift unavailable")
    alert, reason = embedding_decision(drift, ctx.baseline)
    return _alert(ctx, "ai_infra", reason) if alert else _quiet("ai_infra", reason)
