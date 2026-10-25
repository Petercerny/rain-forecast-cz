# Forecast snapshots (data branch)

This branch holds the **append-only history of forecast snapshots** collected hourly by the
[rain-forecast-cz](../../tree/main) pipeline. Do not edit by hand; it is written by GitHub Actions.

```
snapshots/source=<open-meteo|met-no>/date=<YYYY-MM-DD>/part-<fetch time>.parquet
cache/met_state.json     # MET Norway Expires / Last-Modified bookkeeping
```

Each Parquet row is one forecast hour as predicted at `fetched_at` (UTC). Read it with DuckDB:

```sql
SELECT * FROM read_parquet('snapshots/*/*/*.parquet', hive_partitioning = true);
```

## Attribution (CC BY 4.0)

These files are derived from data of **Open-Meteo.com** and the **Norwegian Meteorological Institute (MET Norway)**,
both licensed CC BY 4.0. The values were reformatted (parsed, time-aligned, stored as Parquet) but not otherwise
modified. See `DATA_LICENSES.md` for details. The snapshots are provided as-is, with no warranty.
