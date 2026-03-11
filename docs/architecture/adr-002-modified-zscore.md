# ADR-002: Modified Z-Score over Standard Z-Score

**Status:** Accepted  
**Date:** 2026-03-11  
**Deciders:** Core team

## Context

The original brief specifies flagging trades where volume is **>3 standard deviations** above the 24-hour mean. This assumes trade volumes follow a **normal (Gaussian) distribution**.

In practice, prediction market volumes exhibit:
- **Fat tails**: A few markets dominate volume (elections, major events); most markets are illiquid.
- **Regime changes**: Volume spikes 10-100x during breaking news, creating non-stationary distributions.
- **Zero-inflation**: Many hour-windows have zero trades, pulling the mean down artificially.

A standard Z-score ($Z = \frac{x - \mu}{\sigma}$) is highly sensitive to these outliers — a single massive trade inflates $\sigma$, making subsequent anomalies harder to detect.

## Decision

Use the **Modified Z-Score** based on Median Absolute Deviation (MAD):

$$M_i = \frac{0.6745 \cdot (x_i - \tilde{x})}{\text{MAD}}$$

Where:
- $\tilde{x}$ = median of the volume window
- $\text{MAD} = \text{median}(|x_i - \tilde{x}|)$
- $0.6745$ = scale factor for consistency with normal distribution

**Threshold: $M_i > 3.5$** (equivalent to ~3σ for normal data, but robust to outliers).

## Implementation

```sql
-- DuckDB view for modified z-score per market per hour
CREATE OR REPLACE VIEW v_volume_anomalies AS
WITH hourly AS (
    SELECT
        market_id,
        date_trunc('hour', timestamp) AS hour,
        SUM(size_usd) AS volume
    FROM trades
    WHERE timestamp >= NOW() - INTERVAL '24 hours'
    GROUP BY 1, 2
),
stats AS (
    SELECT
        market_id,
        MEDIAN(volume) AS med_volume,
        MEDIAN(ABS(volume - MEDIAN(volume) OVER (PARTITION BY market_id)))
            AS mad_volume
    FROM hourly
    GROUP BY 1
)
SELECT
    h.market_id,
    h.hour,
    h.volume,
    s.med_volume,
    s.mad_volume,
    CASE WHEN s.mad_volume > 0
         THEN 0.6745 * (h.volume - s.med_volume) / s.mad_volume
         ELSE 0
    END AS modified_z_score
FROM hourly h
JOIN stats s ON h.market_id = s.market_id
WHERE CASE WHEN s.mad_volume > 0
           THEN 0.6745 * (h.volume - s.med_volume) / s.mad_volume
           ELSE 0
      END > 3.5;
```

## Consequences

- More robust anomaly detection in fat-tailed distributions.
- Slightly more complex SQL, but still expressible as a single DuckDB view.
- The 0.6745 constant and 3.5 threshold are configurable via `sentinel/config.py`.
- We still expose the standard Z-score as a secondary metric for comparison.

## References

- Iglewicz, B., & Hoaglin, D. C. (1993). *Volume 16: How to Detect and Handle Outliers*. ASQC Quality Press.
- [Modified Z-Score explanation](https://www.itl.nist.gov/div898/handbook/eda/section3/eda35h.htm)
