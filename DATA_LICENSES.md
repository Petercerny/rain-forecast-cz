# Data licenses and attribution

The **code** in this repository is MIT licensed (see `LICENSE`). The **weather data** it fetches, displays,
and stores is *not* covered by that license. It comes from two third parties, each under CC BY 4.0.

## Open-Meteo

- Source: <https://open-meteo.com/> (forecast API, `https://api.open-meteo.com/v1/forecast`)
- License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), see <https://open-meteo.com/en/license>
- Attribution: **Weather data by [Open-Meteo.com](https://open-meteo.com/)**
- Terms: the free API is for **non-commercial use only** (see <https://open-meteo.com/en/terms>).
  This project is a non-commercial hobby/portfolio project with no ads and no monetization.

## MET Norway (Norwegian Meteorological Institute)

- Source: <https://api.met.no/> (Locationforecast 2.0, `https://api.met.no/weatherapi/locationforecast/2.0/compact`)
- License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), see the
  [terms of service](https://api.met.no/doc/TermsOfService)
- Attribution: **Data from the Norwegian Meteorological Institute (MET Norway), CC BY 4.0**
- The data is reformatted and summarised here; this project is not endorsed by MET Norway.
  It is not affiliated with Yr or NRK.

## Map tiles and libraries (not weather data)

- Map tiles: vector tiles served by [OpenFreeMap](https://openfreemap.org/), a free service that needs no key.
  **&copy; OpenMapTiles** (<https://openmaptiles.org/>) schema and **&copy; OpenStreetMap contributors**
  (data under the [ODbL](https://www.openstreetmap.org/copyright)). The map shows this attribution.
- [MapLibre GL JS](https://maplibre.org/) (BSD-3-Clause) draws the map; [Chart.js](https://www.chartjs.org/) (MIT) draws the charts.
  Both are loaded from a CDN. The weather icons and animations are drawn by this project.
- The [Inter](https://rsms.me/inter/) font (SIL Open Font License) is loaded from Google Fonts.

## Derived data

- The forecast history in the Postgres database is derived from the same two sources and carries the same attribution.
- The Parquet snapshots on the `data` branch are derived from the two sources above (parsed, time-aligned,
  stored in a different format). They carry the same CC BY 4.0 attribution requirement: if you reuse
  them, credit Open-Meteo.com and MET Norway and indicate that the data was changed.
- The short per-location summaries on the website are generated from those numbers. They are not
  an official forecast.
