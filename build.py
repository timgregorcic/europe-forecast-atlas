"""Build the data for the Europe Weather viewer from ECMWF Open Data.

Fetches the latest IFS HRES 0.25 deg run (00 or 12 UTC, out to +144 h every 3 h), crops each field
to the Europe domain and writes, into site/data/:

  meta.json        run time, valid times, domain, and one entry per layer (units, encoding range)
  <layer>.png      every frame of that layer stacked vertically in one PNG ("sprite")

Encoding (decoded in the browser from the PNG's pixels):
  bits 16 scalar : value = lo + (R*256 + G) / 65535 * (hi - lo)
  bits 8  scalar : value = lo + R / 255 * (hi - lo)
  bits 8  vector : u = lo + R / 255 * (hi - lo), v = lo + G / 255 * (hi - lo)

Only the GRIB messages needed are downloaded, using each file's .index and HTTP Range requests.
Data: (c) ECMWF, CC BY 4.0. Regridded (cropped) and unit-converted.

  python build.py                 # latest 00/12 UTC run
  python build.py --run 2026110900
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
from PIL import Image

# Mirrors, tried in order. Override with EW_ROOTS="url1,url2" (e.g. if one is throttling you).
ROOTS = [r for r in os.environ.get("EW_ROOTS", "").split(",") if r] or [
    "https://data.ecmwf.int/forecasts", "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"]
UA = {"User-Agent": "europe-weather-viewer/1.0 (static site build)"}
CACHE = Path(os.environ.get("EW_CACHE", ".cache"))

# Europe domain on the 0.25 deg grid (inclusive). Wide enough to fill a Lambert view of Europe.
WEST, EAST, SOUTH, NORTH = -35.0, 55.0, 25.0, 75.0
RES = 0.25
MAX_STEP = 144
STEPS = list(range(0, MAX_STEP + 1, 3))

# (param, levelist) messages needed per step
MESSAGES = [
    ("2t", None), ("msl", None), ("10u", None), ("10v", None), ("10fg", None), ("tp", None),
    ("sf", None), ("tcc", None), ("mucape", None), ("tcwv", None),
    ("t", 850), ("gh", 500), ("u", 250), ("v", 250),
]

# Layer catalogue. "make" turns raw fields for one step (and the previous step) into display units.
LAYERS = [
    dict(id="t2m", name="Temperatura 2 m", unit="°C", bits=16, lo=-50, hi=50, kind="scalar"),
    dict(id="precip", name="Padavine (3 h)", unit="mm", bits=16, lo=0, hi=150, kind="scalar", accum=True),
    dict(id="snow", name="Sneg (3 h, kot voda)", unit="mm", bits=16, lo=0, hi=100, kind="scalar", accum=True),
    dict(id="wind10", name="Veter 10 m", unit="m/s", bits=8, lo=-60, hi=60, kind="vector"),
    dict(id="gust", name="Sunki vetra (najv., 3 h)", unit="m/s", bits=8, lo=0, hi=80, kind="scalar"),
    dict(id="msl", name="Zračni tlak (morska gl.)", unit="hPa", bits=16, lo=920, hi=1080, kind="scalar"),
    dict(id="tcc", name="Oblačnost", unit="%", bits=8, lo=0, hi=100, kind="scalar"),
    dict(id="tcwv", name="Vodna para (stolpec)", unit="kg/m²", bits=8, lo=0, hi=80, kind="scalar"),
    dict(id="cape", name="CAPE (najbolj nestabilna)", unit="J/kg", bits=8, lo=0, hi=5000, kind="scalar"),
    dict(id="t850", name="Temperatura 850 hPa", unit="°C", bits=16, lo=-50, hi=40, kind="scalar"),
    dict(id="z500", name="Geopotencial 500 hPa", unit="dam", bits=16, lo=450, hi=620, kind="scalar"),
    dict(id="jet250", name="Vetrovni stržen 250 hPa", unit="m/s", bits=8, lo=-120, hi=120, kind="vector"),
]


def make(layer_id: str, f: dict, prev: dict | None):
    """Display-unit array(s) for one frame, or None if it cannot be made (e.g. no previous step)."""
    g = f.get
    try:
        if layer_id == "t2m":
            return f["2t"] - 273.15
        if layer_id == "msl":
            return f["msl"] / 100.0
        if layer_id == "wind10":
            return f["10u"], f["10v"]
        if layer_id == "gust":
            return g("10fg")
        if layer_id in ("precip", "snow"):
            key = "tp" if layer_id == "precip" else "sf"
            if prev is None:
                return None
            return np.clip((f[key] - prev[key]) * 1000.0, 0, None)  # m -> mm over the interval
        if layer_id == "tcc":
            return f["tcc"] * 100.0
        if layer_id == "tcwv":
            return f["tcwv"]
        if layer_id == "cape":
            return g("mucape")
        if layer_id == "t850":
            return f["t850"] - 273.15
        if layer_id == "z500":
            return f["gh500"] / 10.0
        if layer_id == "jet250":
            return f["u250"], f["v250"]
    except KeyError:
        return None
    raise ValueError(layer_id)


# --------------------------------------------------------------------------------------- download

def get(url: str, headers: dict | None = None, attempts: int = 8) -> bytes:
    """GET with retries and jittered backoff: S3 answers bursts of range requests with 503 Slow Down."""
    h = dict(UA, **(headers or {}))
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
        except Exception:
            pass
        time.sleep(min(30, 2 ** i) * (0.5 + random.random()))
    raise RuntimeError(f"failed: {url}")


def get_message(rel: str, a: int, n: int, first_root: str) -> bytes:
    """One GRIB message (cached on disk), trying the mirrors in order."""
    cache = CACHE / f"{rel.replace('/', '_')}_{a}_{n}.grib2"
    if cache.exists():
        return cache.read_bytes()
    roots = [first_root] + [r for r in ROOTS if r != first_root]
    last = None
    for root in roots * 3:  # a couple of quick tries per mirror, then the next one, three rounds
        try:
            data = get(f"{root}/{rel}.grib2", {"Range": f"bytes={a}-{a + n - 1}"}, attempts=2)
            if len(data) != n:
                raise RuntimeError(f"short read {len(data)} != {n}")
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            return data
        except Exception as e:
            last = e
    raise RuntimeError(f"could not download {rel} @{a}: {last}")


def rel_path(run: datetime, step: int) -> str:
    return f"{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc"


def index_rows(run: datetime, step: int) -> tuple[str, list[dict]]:
    last = None
    for root in ROOTS:
        try:
            txt = get(f"{root}/{rel_path(run, step)}.index").decode()
            return root, [json.loads(l) for l in txt.splitlines() if l.strip()]
        except Exception as e:  # try the next mirror
            last = e
    raise RuntimeError(f"no index for {run:%Y%m%d%H} +{step}h: {last}")


def run_complete(run: datetime) -> bool:
    try:
        index_rows(run, MAX_STEP)
        return True
    except Exception:
        return False


def latest_run() -> datetime:
    now = datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 12) * 12)
    for _ in range(8):
        if run_complete(t):
            return t
        t -= timedelta(hours=12)
    raise RuntimeError("no complete 00/12 UTC run found in the last 4 days")


def crop_indices(lats: np.ndarray, lons: np.ndarray):
    lons = ((lons + 180) % 360) - 180
    want_lat = np.round(np.arange(NORTH, SOUTH - 1e-6, -RES), 4)
    want_lon = np.round(np.arange(WEST, EAST + 1e-6, RES), 4)
    lat_ix = {round(float(v), 4): i for i, v in enumerate(lats)}
    lon_ix = {round(float(v), 4): i for i, v in enumerate(lons)}
    return np.array([lat_ix[v] for v in want_lat]), np.array([lon_ix[v] for v in want_lon])


_crop_cache: dict = {}


def decode_crop(msg: bytes) -> np.ndarray:
    gid = eccodes.codes_new_from_message(msg)
    try:
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        la1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lo1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        dj = eccodes.codes_get(gid, "jDirectionIncrementInDegrees")
        di = eccodes.codes_get(gid, "iDirectionIncrementInDegrees")
        jpos = eccodes.codes_get(gid, "jScansPositively")
        vals = eccodes.codes_get_values(gid).reshape(nj, ni)
        if eccodes.codes_get(gid, "bitmapPresent"):
            miss = eccodes.codes_get(gid, "missingValue")
            vals = np.where(vals == miss, np.nan, vals)
    finally:
        eccodes.codes_release(gid)
    key = (ni, nj, la1, lo1, dj, di, jpos)
    if key not in _crop_cache:
        lats = la1 + (dj if jpos else -dj) * np.arange(nj)
        lons = lo1 + di * np.arange(ni)
        _crop_cache[key] = crop_indices(lats, lons)
    jix, iix = _crop_cache[key]
    return vals[np.ix_(jix, iix)].astype(np.float32)


def fetch_step(run: datetime, step: int) -> dict[str, np.ndarray]:
    root, rows = index_rows(run, step)
    rel = rel_path(run, step)
    out = {}
    for param, lev in MESSAGES + ([("lsm", None)] if step == 0 else []):
        row = next((r for r in rows if r["param"] == param
                    and (lev is None or str(r.get("levelist")) == str(lev))
                    and (lev is not None or r.get("levtype") == "sfc")), None)
        if row is None:
            continue  # e.g. 10fg, mucape absent at step 0
        a, n = row["_offset"], row["_length"]
        msg = get_message(rel, a, n, root)
        out[param if lev is None else f"{param}{lev}"] = decode_crop(msg)
    return out


# --------------------------------------------------------------------------------------- encode

def encode(layer: dict, frames: list) -> tuple[Image.Image, list[int]]:
    lo, hi, bits = layer["lo"], layer["hi"], layer["bits"]
    h, w = None, None
    for fr in frames:
        if fr is not None:
            a = fr[0] if isinstance(fr, tuple) else fr
            h, w = a.shape
            break
    img = np.zeros((h * len(frames), w, 3), np.uint8)
    missing = []

    def q(a, levels):
        x = np.nan_to_num((a - lo) / (hi - lo), nan=0.0)
        return np.round(np.clip(x, 0, 1) * levels).astype(np.uint32)

    for k, fr in enumerate(frames):
        sl = slice(k * h, (k + 1) * h)
        if fr is None:
            missing.append(k)
            continue
        if layer["kind"] == "vector":
            img[sl, :, 0] = q(fr[0], 255)
            img[sl, :, 1] = q(fr[1], 255)
        elif bits == 16:
            v = q(fr, 65535)
            img[sl, :, 0] = v >> 8
            img[sl, :, 1] = v & 255
        else:
            img[sl, :, 0] = q(fr, 255)
    return Image.fromarray(img, "RGB"), missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="YYYYMMDDHH (default: latest complete 00/12 UTC run)")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    run = (datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
           if args.run else latest_run())
    print(f"run {run:%Y-%m-%d %H} UTC, {len(STEPS)} steps", flush=True)

    t0 = time.time()
    fields: dict[int, dict] = {}
    with cf.ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(fetch_step, run, s): s for s in STEPS}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            fields[futs[fut]] = fut.result()
            print(f"  {i}/{len(STEPS)} steps ({time.time() - t0:.0f}s)", flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta_layers = []
    sample = fields[STEPS[0]]["2t"]
    for layer in LAYERS:
        frames = []
        for i, s in enumerate(STEPS):
            prev = fields[STEPS[i - 1]] if i > 0 else None
            frames.append(make(layer["id"], fields[s], prev))
        img, missing = encode(layer, frames)
        img.save(out / f"{layer['id']}.png", optimize=True)
        meta_layers.append({k: v for k, v in layer.items()} | {"missing": missing})
        print(f"  {layer['id']}: {(out / (layer['id'] + '.png')).stat().st_size / 1e6:.2f} MB", flush=True)

    # land-sea mask (one frame) for the map's base shading
    lsm = fields[STEPS[0]].get("lsm")
    if lsm is not None:
        Image.fromarray(np.dstack([np.round(np.clip(lsm, 0, 1) * 255).astype(np.uint8)] * 3), "RGB").save(
            out / "lsm.png", optimize=True)

    meta = {
        "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "steps": STEPS,
        "times": [(run + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%MZ") for s in STEPS],
        "grid": {"west": WEST, "east": EAST, "south": SOUTH, "north": NORTH, "res": RES,
                 "nx": int(sample.shape[1]), "ny": int(sample.shape[0])},
        "model": "ECMWF IFS HRES (Open Data, 0.25°)",
        "attribution": "Data: © ECMWF, CC BY 4.0. Cropped and unit-converted.",
        "layers": meta_layers,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"done in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
