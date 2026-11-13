# Umay interpretation tool, version 2

The `baseline`, `analyze`, `sleep`, `intraday`, `sessions`, `physiology`, `longitudinal` and
`relationship` actions of `health_archive` run through `read_health` and the chat API. `inventory`,
`daily`, `records` and `coverage` keep their version 1 behavior. The source archive and manual
entries are not changed; only the existing, disposable daily cache is written.

- Personal baseline: by default the 56 previous calendar days (14–90 selectable), requiring at
  least 14 eligible days and 50% calendar support. Under 7 days is `cold_start`. The evaluated day
  and later days are not part of the baseline. Returns median, unscaled MAD, min/max, linear p10/p90
  and mid-rank percentile. Z = (value − median)/(1.4826 × MAD); with a zero MAD there is no z.
- Gaps, uncertain sleep, conflicts, partial days and missing step intervals do not count as
  comparison support. A measured zero stays. Missing days do not become zero. No source ranking is
  applied. A rename is linked only with existing `raw_source_devices` membership evidence; recording
  method and measurement definition stay separate. Sources without evidence are not merged.
- `analyze.candidates`: at most five observation candidates from eligible summaries at least 40
  percentile points from the mid-rank, by descending score, ties in summary order. This is not a
  clinical significance or treatment score. `summary_index` refers to the whole result array;
  `summary_offset` is the start of the page.
- Sleep carries the existing session and stage evidence as is. Stage totals produce no timeline; an
  unsupported timeline is `null`, and unknown and overlapping durations stay separate.
- Heart rate: linear duration estimate only between samples at most 300 seconds apart, with no
  extension at the ends. The mean is weighted by real UTC seconds. The sample distribution comes
  only from the day's own samples. Step rate is an estimate of equal distribution over the interval;
  statistics of source interval counts are given in their own unit, and there is no exact movement
  burst.
- A session's physical duration, reported active time, distance/energy and overlapping sessions stay
  separate. Without segment evidence, continuous running time is `null`.
- Physiology supports only existing parser fields; method detail, definition and unit are carried.
  Recency is 7 days from the query's last day, 30 days for VO₂. These thresholds are not clinical
  validity limits. Absence and unsupported format are separate reasons.
- In quantity summaries, active and total energy and source intervals stay separate; different
  interval lengths are different definitions; interval amounts are not counted as a daily total or
  clipped to the window. Weight and body change is computed within the same source, definition and
  method; simultaneous conflicts are outside the computation but visible in the evidence. Manual
  measurements flagged as test entries are excluded from both computation and evidence by the shared
  reader.
- Relationship: at most five pre-selected lags of −14…14 days; with a positive lag, Y comes after.
  Calendar pairs and bound record references are returned. Requires at least 20 pairs and 50%
  support. Spearman uses mid-ranks; both calendar halves of the window need at least 8 pairs, the
  same sign and a coefficient difference of at most 0.35. A constant or unstable series yields no
  eligible finding; there is no p-value, causation or confounder-control claim.

The new actions are limited to 90 days, relationship to 180 days; input to 50,000 relevant records
and 16 MiB of raw JSON; the result to 1,000 summaries and relationship to 16 regimes; a returned
page to 64 KiB. Exceeding a budget produces a structured refusal; JSON is never truncated.
`next_cursor` is bound to version, query, input and the complete computed result. `limit` may change;
other parameters must stay the same. On a change of value, membership, deletion, quarantine, device
decision or relevant manual flag, the tool starts over with `stale_cursor`. Statistics and candidate
order do not depend on page size. A PostgreSQL read uses a single transaction snapshot; verifying
against real PostgreSQL and Google or a model is outside this repository's tests.

The input budget applies to the types an action uses; for example, a minute-level heart-rate
archive does not block a personal daily baseline. In actions using the cache, the inputs of the five
daily adapters are counted. On a dense archive `lookback_days` can be narrowed as well; a refusal is
not a missing-data finding.
