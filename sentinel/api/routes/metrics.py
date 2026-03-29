"""Metrics endpoints — time-series pipeline activity and operational overview."""

from __future__ import annotations

from fastapi import APIRouter, Query

from sentinel.api.deps import get_db
from sentinel.config import settings

router = APIRouter(tags=["metrics"])


@router.get("/metrics/timeseries")
async def timeseries(
    hours: int = Query(6, ge=1, le=72),
    bucket_minutes: int = Query(5, ge=1, le=60),
) -> list[dict]:
    """Bucketed pipeline activity over the last *hours*.

    Returns one row per time bucket with counts for:
    - trades ingested
    - signals generated
    - LLM T1 and T2 calls
    - high-suspicion alerts (score >= 80)
    """
    db = get_db()

    rows = db.execute(
        """
        WITH buckets AS (
            SELECT
                time_bucket(
                    INTERVAL (? || ' minutes'),
                    generate_series
                ) AS bucket
            FROM generate_series(
                date_trunc('minute', CURRENT_TIMESTAMP - INTERVAL (? || ' hours')),
                date_trunc('minute', CURRENT_TIMESTAMP),
                INTERVAL (? || ' minutes')
            )
        ),
        trade_counts AS (
            SELECT
                time_bucket(INTERVAL (? || ' minutes'), timestamp) AS bucket,
                COUNT(*) AS cnt
            FROM v_deduped_trades
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
            GROUP BY 1
        ),
        signal_counts AS (
            SELECT
                time_bucket(INTERVAL (? || ' minutes'), created_at) AS bucket,
                COUNT(*) AS cnt
            FROM signals
            WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
            GROUP BY 1
        ),
        reasoning_counts AS (
            SELECT
                time_bucket(INTERVAL (? || ' minutes'), created_at) AS bucket,
                COUNT(*) FILTER (WHERE tier1_model IS NOT NULL) AS t1,
                COUNT(*) FILTER (WHERE tier2_model IS NOT NULL) AS t2,
                COUNT(*) FILTER (WHERE suspicion_score >= 80)   AS alerts
            FROM signal_reasoning
            WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL (? || ' hours')
            GROUP BY 1
        )
        SELECT
            b.bucket,
            COALESCE(tc.cnt, 0)     AS trades,
            COALESCE(sc.cnt, 0)     AS signals,
            COALESCE(rc.t1, 0)      AS llm_t1,
            COALESCE(rc.t2, 0)      AS llm_t2,
            COALESCE(rc.alerts, 0)  AS alerts
        FROM buckets b
        LEFT JOIN trade_counts    tc ON b.bucket = tc.bucket
        LEFT JOIN signal_counts   sc ON b.bucket = sc.bucket
        LEFT JOIN reasoning_counts rc ON b.bucket = rc.bucket
        ORDER BY b.bucket
        """,
        [
            bucket_minutes, hours, bucket_minutes,  # buckets CTE
            bucket_minutes, hours,                   # trade_counts
            bucket_minutes, hours,                   # signal_counts
            bucket_minutes, hours,                   # reasoning_counts
        ],
    ).fetchall()

    return [
        {
            "bucket": r[0].isoformat(),
            "trades": r[1],
            "signals": r[2],
            "llm_t1": r[3],
            "llm_t2": r[4],
            "alerts": r[5],
        }
        for r in rows
    ]


@router.get("/metrics/overview")
async def overview() -> dict:
    """Aggregate operational metrics for the metrics dashboard.

    Returns:
    - funnel: trades -> signals -> classified -> high suspicion (today)
    - classification: INFORMED vs NOISE counts (today)
    - score_distribution: suspicion score histogram (today)
    - top_markets: markets with most signals (last 24h)
    """
    db = get_db()

    # ── Detection funnel (today) ────────────────────────────────────
    funnel_row = db.execute("""
        SELECT
            (SELECT COUNT(*) FROM v_deduped_trades
             WHERE timestamp >= CURRENT_DATE)                     AS trades_today,
            (SELECT COUNT(*) FROM signals
             WHERE created_at >= CURRENT_DATE)                    AS signals_today,
            (SELECT COUNT(*) FROM signal_reasoning
             WHERE created_at >= CURRENT_DATE)                    AS classified_today,
            (SELECT COUNT(*) FROM signal_reasoning
             WHERE created_at >= CURRENT_DATE
               AND suspicion_score >= 80)                         AS high_suspicion_today
    """).fetchone()

    funnel = {
        "trades": funnel_row[0],
        "signals": funnel_row[1],
        "classified": funnel_row[2],
        "high_suspicion": funnel_row[3],
    }

    # ── Classification breakdown (today) ────────────────────────────
    class_rows = db.execute("""
        SELECT
            classification,
            COUNT(*) AS cnt
        FROM signal_reasoning
        WHERE created_at >= CURRENT_DATE
          AND classification IS NOT NULL
        GROUP BY 1
    """).fetchall()

    classification = {r[0]: r[1] for r in class_rows}

    # ── Score distribution (today) ──────────────────────────────────
    dist_rows = db.execute("""
        SELECT
            CASE
                WHEN COALESCE(sr.suspicion_score, s.statistical_score) < 20  THEN '0-19'
                WHEN COALESCE(sr.suspicion_score, s.statistical_score) < 40  THEN '20-39'
                WHEN COALESCE(sr.suspicion_score, s.statistical_score) < 60  THEN '40-59'
                WHEN COALESCE(sr.suspicion_score, s.statistical_score) < 80  THEN '60-79'
                ELSE '80-100'
            END AS bucket,
            COUNT(*) AS cnt
        FROM signals s
        LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        WHERE s.created_at >= CURRENT_DATE
        GROUP BY 1
        ORDER BY 1
    """).fetchall()

    # Ensure all buckets present
    score_distribution = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    for r in dist_rows:
        score_distribution[r[0]] = r[1]

    # ── Top flagged markets (last 24h) ──────────────────────────────
    market_rows = db.execute("""
        SELECT
            s.market_id,
            m.question,
            m.category,
            COUNT(*) AS signal_count,
            MAX(COALESCE(sr.suspicion_score, s.statistical_score)) AS max_score,
            AVG(COALESCE(sr.suspicion_score, s.statistical_score)) AS avg_score
        FROM signals s
        LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        LEFT JOIN markets m ON s.market_id = m.market_id
        WHERE s.created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        GROUP BY s.market_id, m.question, m.category
        ORDER BY signal_count DESC
        LIMIT 10
    """).fetchall()

    top_markets = [
        {
            "market_id": r[0],
            "question": r[1],
            "category": r[2],
            "signal_count": r[3],
            "max_score": r[4],
            "avg_score": round(float(r[5]), 1) if r[5] else None,
        }
        for r in market_rows
    ]

    # ── Top traded markets by volume (last 24h) ─────────────────────
    traded_rows = db.execute("""
        SELECT
            t.market_id,
            m.question,
            COUNT(*) AS trade_count,
            SUM(t.size_usd) AS volume_usd,
            COUNT(DISTINCT t.wallet) AS unique_wallets
        FROM v_deduped_trades t
        LEFT JOIN markets m ON t.market_id = m.market_id
        WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        GROUP BY t.market_id, m.question
        ORDER BY trade_count DESC
        LIMIT 10
    """).fetchall()

    top_traded_markets = [
        {
            "market_id": r[0],
            "question": r[1],
            "trade_count": r[2],
            "volume_usd": round(float(r[3]), 2) if r[3] else 0,
            "unique_wallets": r[4],
        }
        for r in traded_rows
    ]

    # ── Tier 2 coverage (today) ─────────────────────────────────────
    t2_row = db.execute("""
        SELECT
            COUNT(*) FILTER (WHERE tier2_used = TRUE)  AS real_t2,
            COUNT(*) FILTER (WHERE tier2_used = FALSE) AS fallback,
            COUNT(*)                                    AS total
        FROM signal_reasoning
        WHERE created_at >= CURRENT_DATE
    """).fetchone()

    tier2_coverage = {
        "real": t2_row[0] if t2_row else 0,
        "fallback": t2_row[1] if t2_row else 0,
        "total": t2_row[2] if t2_row else 0,
    }

    # ── Market coverage (attractiveness scoring progress) ────────────
    coverage_row = db.execute("""
        SELECT
            COUNT(*) FILTER (WHERE active = true AND resolved = false)         AS total,
            COUNT(*) FILTER (WHERE active = true AND resolved = false
                               AND attractiveness_score IS NOT NULL
                               AND attractiveness_score >= ?)                  AS hot_eligible,
            COUNT(*) FILTER (WHERE attractiveness_score IS NOT NULL)           AS scored,
            COUNT(*) FILTER (WHERE attractiveness_score IS NULL
                               AND active = true AND resolved = false)         AS unscored,
            ROUND(AVG(attractiveness_score) FILTER (
                WHERE attractiveness_score IS NOT NULL
                  AND active = true AND resolved = false
                  AND attractiveness_score >= ?
            ), 1)                                                               AS avg_hot_score
        FROM markets
    """, [settings.hot_market_min_score, settings.hot_market_min_score]).fetchone()

    market_coverage = {
        "total": coverage_row[0] if coverage_row else 0,
        "hot_eligible": coverage_row[1] if coverage_row else 0,
        "scored": coverage_row[2] if coverage_row else 0,
        "unscored": coverage_row[3] if coverage_row else 0,
        "avg_hot_score": float(coverage_row[4]) if coverage_row and coverage_row[4] else None,
        "hot_capacity": settings.hot_market_count,
    }

    return {
        "funnel": funnel,
        "classification": classification,
        "score_distribution": score_distribution,
        "top_markets": top_markets,
        "top_traded_markets": top_traded_markets,
        "tier2_coverage": tier2_coverage,
        "market_coverage": market_coverage,
    }


@router.get("/metrics/ingestion")
async def ingestion() -> dict:
    """Ingestion source breakdown — book events + REST trades.

    Note: The WS channel only delivers order-book events (price_changes),
    not trade executions.  All trade data comes from REST polling the
    Polymarket data-api ``/trades`` endpoint.

    Returns:
    - sources: per-source trade counts for today and all-time
    - hourly: last 24h per-source trade counts bucketed by hour
    - totals: aggregate counts across all sources
    """
    db = get_db()

    # ── Trade counts (today + all-time) ─────────────────────────────
    totals_row = db.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE timestamp >= CURRENT_DATE) AS today
        FROM v_deduped_trades
    """).fetchone()

    total_all = totals_row[0] if totals_row else 0
    total_today = totals_row[1] if totals_row else 0

    # ── Hourly breakdown (last 24h) ─────────────────────────────────
    hourly_rows = db.execute("""
        SELECT
            date_trunc('hour', timestamp) AS hour_bucket,
            COUNT(*) AS cnt
        FROM v_deduped_trades
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        GROUP BY 1
        ORDER BY 1
    """).fetchall()

    hourly: list[dict] = []
    for r in hourly_rows:
        hourly.append({
            "bucket": r[0].isoformat(),
            "trades": r[1],
        })

    # ── Latest trade + active counts ────────────────────────────────
    latest_row = db.execute("""
        SELECT
            MAX(timestamp) AS latest_rest,
            COUNT(DISTINCT market_id) FILTER (WHERE timestamp >= CURRENT_DATE) AS markets_today,
            COUNT(DISTINCT wallet) FILTER (WHERE timestamp >= CURRENT_DATE) AS wallets_today
        FROM v_deduped_trades
    """).fetchone()

    return {
        "totals": {
            "all_time": total_all,
            "today": total_today,
        },
        "latest": {
            "rest": latest_row[0].isoformat() if latest_row[0] else None,
        },
        "markets_active_today": latest_row[1] or 0,
        "wallets_active_today": latest_row[2] or 0,
        "hourly": hourly,
    }


@router.get("/metrics/accuracy")
async def accuracy() -> list[dict]:
    """Classification accuracy on resolved markets.

    For each resolved market that has signals, computes how well INFORMED
    classifications predicted the outcome (BUY→YES, SELL→NO).
    """
    db = get_db()

    rows = db.execute("""
        SELECT
            s.market_id,
            m.question,
            m.category,
            m.resolved_price,
            COUNT(*)                                                        AS signal_count,
            COUNT(*) FILTER (WHERE sr.classification = 'INFORMED')          AS informed_count,
            COUNT(*) FILTER (WHERE sr.classification = 'NOISE')             AS noise_count,
            COUNT(*) FILTER (
                WHERE sr.classification = 'INFORMED'
                  AND (
                      (s.side = 'BUY'  AND m.resolved_price >= 0.95) OR
                      (s.side = 'SELL' AND m.resolved_price <= 0.05)
                  )
            )                                                               AS correct_informed
        FROM signals s
        JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        JOIN markets m ON s.market_id = m.market_id
        WHERE m.resolved = TRUE
          AND m.resolved_price IS NOT NULL
          AND sr.classification IS NOT NULL
        GROUP BY s.market_id, m.question, m.category, m.resolved_price
        HAVING COUNT(*) >= 1
        ORDER BY signal_count DESC
        LIMIT 30
    """).fetchall()

    result = []
    for r in rows:
        informed = r[5] or 0
        correct = r[7] or 0
        result.append({
            "market_id": r[0],
            "question": r[1],
            "category": r[2],
            "resolved_price": float(r[3]) if r[3] is not None else None,
            "signal_count": r[4],
            "informed_count": informed,
            "noise_count": r[6] or 0,
            "correct_informed": correct,
            "accuracy_pct": round(correct / informed * 100) if informed > 0 else None,
        })
    return result


@router.get("/metrics/accuracy/summary")
async def accuracy_summary() -> dict:
    """Global precision, recall, F1 across all resolved markets.

    Uses the ``v_signal_outcomes`` view which joins signals → signal_reasoning
    → markets and categorises each into TP/FP/FN/TN.
    """
    db = get_db()

    row = db.execute("""
        SELECT
            COUNT(*) FILTER (WHERE confusion = 'TP')  AS tp,
            COUNT(*) FILTER (WHERE confusion = 'FP')  AS fp,
            COUNT(*) FILTER (WHERE confusion = 'FN')  AS fn,
            COUNT(*) FILTER (WHERE confusion = 'TN')  AS tn,
            COUNT(*) FILTER (WHERE confusion IS NOT NULL) AS total
        FROM v_signal_outcomes
    """).fetchone()

    tp, fp, fn, tn, total = row
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None

    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "total_evaluated": total,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1_score": round(f1, 4) if f1 is not None else None,
    }


@router.get("/metrics/accuracy/calibration")
async def accuracy_calibration() -> list[dict]:
    """Calibration curve: for each score bucket, what fraction of trades were correct.

    Reveals whether higher scores actually predict better outcomes.
    """
    db = get_db()

    rows = db.execute("""
        SELECT
            CASE
                WHEN COALESCE(suspicion_score, statistical_score) < 40 THEN '30-39'
                WHEN COALESCE(suspicion_score, statistical_score) < 60 THEN '40-59'
                WHEN COALESCE(suspicion_score, statistical_score) < 80 THEN '60-79'
                ELSE '80+'
            END AS score_bucket,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE trade_correct = TRUE) AS correct,
            COUNT(*) FILTER (WHERE classification = 'INFORMED') AS predicted_informed,
            COUNT(*) FILTER (WHERE confusion = 'TP') AS true_positives
        FROM v_signal_outcomes
        GROUP BY 1
        ORDER BY 1
    """).fetchall()

    return [
        {
            "score_bucket": r[0],
            "total": r[1],
            "correct": r[2],
            "accuracy_pct": round(r[2] / r[1] * 100, 1) if r[1] > 0 else None,
            "predicted_informed": r[3],
            "true_positives": r[4],
        }
        for r in rows
    ]


@router.get("/metrics/patterns")
async def patterns() -> list[dict]:
    """Hour-of-day trading patterns over the last 7 days.

    Returns 24 buckets (hours 0-23) with trade counts, signal counts, and
    INFORMED classification counts to reveal temporal anomaly patterns.
    """
    db = get_db()

    rows = db.execute("""
        SELECT
            EXTRACT(hour FROM t.timestamp)::INTEGER                                    AS hour,
            COUNT(*)                                                                    AS total_trades,
            COUNT(DISTINCT s.signal_id)                                                AS signals,
            COUNT(DISTINCT sr.signal_id) FILTER (WHERE sr.classification = 'INFORMED') AS informed
        FROM v_deduped_trades t
        LEFT JOIN signals s ON t.trade_id = s.trade_id
        LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
        WHERE t.timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        GROUP BY 1
        ORDER BY 1
    """).fetchall()

    # Ensure all 24 hours are present
    by_hour = {r[0]: r for r in rows}
    return [
        {
            "hour": h,
            "trades": by_hour[h][1] if h in by_hour else 0,
            "signals": by_hour[h][2] if h in by_hour else 0,
            "informed": by_hour[h][3] if h in by_hour else 0,
        }
        for h in range(24)
    ]
