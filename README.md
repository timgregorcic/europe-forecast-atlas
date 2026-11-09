# Europe Forecast Atlas

Animated ECMWF forecast maps for Europe in a static web page. No server, no AI, no running costs:
a GitHub Action fetches each new 00 and 12 UTC run of ECMWF's IFS model from **ECMWF Open Data**,
crops it to Europe, and publishes the result to GitHub Pages. All drawing happens in the browser.

Inspired by, and partly based on, [Weather Explorer](https://github.com/tehoro/weather-explorer-web)
by Neil Gordon (MIT licence). The way GRIB messages are fetched (`.index` files + HTTP Range
requests) follows its backend, and the coastline and border outlines are cropped from its
`basemap.json` (public-domain Natural Earth 1:50m data).

## What it shows

- **Map:** Lambert conformal conic projection (standard parallels 35° N and 65° N, centred on
  12° E), the usual projection for European synoptic charts. Data domain 35° W–55° E, 25°–75° N,
  0.25° grid (IFS HRES Open Data).
- **Time:** the latest 00 or 12 UTC run, every 3 h from +0 h to +144 h (49 frames).
- **Layers:**
  - Surface: 2 m temperature, 10 m wind, gusts (max over previous 3 h), mean sea-level pressure,
    total cloud cover
  - Precipitation: 3-hourly precipitation, 3-hourly snowfall (water equivalent), total column water vapour
  - Upper air: CAPE (most unstable), 850 hPa temperature, 500 hPa geopotential height, 250 hPa wind (jet)
- **Overlays:** isobars every 4 hPa with H/L centres, country borders, animated wind flow.
- **Point forecast:** click or tap the map for a time series of the current layer at that point.
  Hover (with a mouse) for the value under the cursor.
- **Controls:** space plays/pauses, ← → step through time, mouse wheel or pinch zooms, drag pans.

**This is raw model guidance, not a forecast or warning.** For warnings use your national weather
service or [MeteoAlarm](https://meteoalarm.org).

## Files

| File | What it does |
|---|---|
| `build.py` | Downloads the needed GRIB messages, crops to Europe, converts units, writes `site/data/` |
| `site/index.html` | The whole viewer: HTML, CSS and JavaScript in one file, no build step |
| `site/basemap-eu.json` | Coastlines and borders (Natural Earth) cropped to Europe |
| `tools/make_basemap_eu.py` | Rebuilds `basemap-eu.json` from Weather Explorer's `basemap.json` |
| `.github/workflows/update.yml` | Builds the data twice a day and deploys to GitHub Pages |

### Data format (`site/data/`)

- `meta.json`: run time, valid times, grid, and for each layer its units and encoding range.
- `<layer>.png`: all frames of a layer stacked vertically in one PNG. Values are packed into pixel
  channels: 16-bit layers use red (high byte) and green (low byte); 8-bit layers use red;
  wind layers put u in red and v in green. `lsm.png` is the land–sea mask.

The whole data set is about 25–35 MB per run; the page loads one layer at a time.

## Publish your own copy (GitHub Pages)

1. Create a new GitHub repository and push these files to its `main` branch.
2. In the repository: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Open **Actions → Update forecast → Run workflow** to build the first time (5–15 minutes).
4. The site appears at `https://<your-user>.github.io/<repository>/`. It then updates by itself
   at 08:17 and 20:17 UTC.

Note: GitHub turns off scheduled workflows in a repository with no activity for 60 days. If that
happens, re-enable it in the Actions tab.

## Run it locally

```bash
pip install -r requirements.txt
python build.py                # latest complete 00/12 UTC run → site/data/
cd site && python -m http.server 8000
```

Then open http://localhost:8000. Downloaded messages are cached in `.cache/` (delete it now and then).
If one mirror is throttling you, choose the order: `EW_ROOTS=https://data.ecmwf.int/forecasts python build.py`.

## Changing things

- **Domain:** `WEST, EAST, SOUTH, NORTH` in `build.py`. For a different region also change the
  projection centre in `LCC` and the box in `fitEurope()` in `index.html`.
- **A new layer:** add the GRIB message to `MESSAGES`, a layer to `LAYERS` and a branch to `make()`
  in `build.py`, then a colour scale to `STYLE` in `index.html`. Only fields in the Open Data
  `.index` files are available.
- **Forecast length:** `MAX_STEP` (Open Data HRES goes to +240 h; 6-hourly after +144 h, which
  `build.py` does not handle yet).

## Licences and attribution

- Code: MIT (see `LICENSE`).
- Weather data: © ECMWF, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  Cropped and unit-converted. Keep this attribution on any page that shows it.
- Coastlines and borders: [Natural Earth](https://www.naturalearthdata.com) (public domain), via
  Weather Explorer's basemap (MIT, © 2026 Neil Gordon).
- This project is not an ECMWF service and is not affiliated with Weather Explorer.
