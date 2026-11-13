# HealthOS reference — Analytics compatibility

Part of the [technical reference](../REFERENCE.md).

## Analytics compatibility

Connect read-only analytics tools to the `analytics_*` views, such as
`analytics_heart_rate`, `analytics_sleep_sessions`, `analytics_energy_expenditure`, and
`analytics_scalar_metrics`. These views expose the normalized tables with what every reader
counts ([Derived tables](data-safety.md#derived-tables)): since `20261024_0013` (PostgreSQL) a row needs a live,
unquarantined raw row with its key, is not quarantined itself and is not a
[superseded duplicate](identity-and-versions.md#record-identity). Before, `20261021_0012` left out only quarantined rows
and rows whose raw record was marked deleted or superseded, so a normalized row without a raw row,
or one whose raw row alone was quarantined, still counted in the views (and on the dashboard,
though not in its history). The underlying normalized tables keep every row for auditing, and the
raw JSON remains available for future Pandas/Jupyter, SQL, Grafana, n8n, or
local-agent pipelines. The archive itself has no AI layer; the optional Umay chat reads it only
on request.

### Quarantined rollup records

No raw health record is deleted during quarantine. Migration `20260818_0002` preserves every raw
and normalized row, adds `is_quarantined` and `quarantine_reason`, and creates an analytics-safe
view for every normalized table. Future UPSERTs deliberately do not overwrite quarantine metadata.

The live archive contains 20,165 rollups produced before request windows were fixed to canonical
UTC-minute boundaries. They are identified by all of these conditions:

```sql
data_type IN ('calories-in-heart-rate-zone', 'total-calories')
AND (
  extract(second FROM source_start_time) <> 0
  OR extract(microseconds FROM source_start_time) <> 0
)
```

Their stable reason code is `pre_fix_rollup_non_minute_boundary_v1`. The raw rows remain in
`raw_health_data`; corresponding rows remain in `energy_expenditure` or `scalar_metrics` for audit
purposes. Use the `analytics_*` views for future analysis. If a base normalized table must be queried
directly, add `WHERE is_quarantined = false`.
