"""Build the data for the Slovenia page (site/slovenija/) from two high-resolution models.

ALADIN/SI  ARSO, https://meteo.arso.gov.si/uploads/probase/www/model/data/
           one ZIP per run (00/06/12/18 UTC), hourly GRIB1 files to +72 h, already cut to Slovenia.
ICON-D2    DWD Open Data, https://opendata.dwd.de/weather/nwp/icon-d2/grib/
           one bz2 GRIB2 file per field and hour (runs every 3 h, to +48 h), cropped here.

Both are cropped to the ALADIN domain and written in the same sprite format as build.py:

  site/slovenija/data/<model>/meta.json, <layer>.png   (model = aladin | icon)
  site/slovenija/data/relief.json, relief.png           hillshade + land fraction (from ICON-D2)

  python build_si.py                   # both models, latest runs
  python build_si.py --models aladin
"""
from __future__ import annotations

import argparse
import bz2
import concurrent.futures as cf
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
from PIL import Image

UA = {"User-Agent": "europe-weather-viewer/1.0 (static site build)"}
CACHE = Path(os.environ.get("EW_CACHE", ".cache")) / "si"

ARSO = "https://meteo.arso.gov.si/uploads/probase/www/model/data"
DWD = "https://opendata.dwd.de/weather/nwp/icon-d2/grib"

# Output domain = the ALADIN/SI product domain (degrees)
WEST, EAST, SOUTH, NORTH = 11.625, 17.895, 44.642, 47.407

# Display layers. "src" maps a model to the raw field key(s) make() needs.
LAYERS = [
    dict(id="t2m", name="Temperatura 2 m", unit="°C", bits=16, lo=-40, hi=45, kind="scalar"),
    dict(id="rh2m", name="Relativna vlaga 2 m", unit="%", bits=8, lo=0, hi=100, kind="scalar"),
    dict(id="precip", name="Padavine (1 h)", unit="mm", bits=16, lo=0, hi=100, kind="scalar"),
    dict(id="snow", name="Snežna odeja", unit="cm", bits=16, lo=0, hi=400, kind="scalar", models=["icon"]),
    dict(id="dbz", name="Radarska odbojnost (sim.)", unit="dBZ", bits=8, lo=0, hi=75, kind="scalar", models=["icon"]),
    dict(id="wind10", name="Veter 10 m", unit="m/s", bits=8, lo=-50, hi=50, kind="vector"),
    dict(id="gust", name="Sunki vetra (najv., 1 h)", unit="m/s", bits=8, lo=0, hi=60, kind="scalar", models=["icon"]),
    dict(id="msl", name="Zračni tlak (morska gl.)", unit="hPa", bits=16, lo=950, hi=1060, kind="scalar"),
    dict(id="tcc", name="Oblačnost", unit="%", bits=8, lo=0, hi=100, kind="scalar"),
    dict(id="cape", name="CAPE", unit="J/kg", bits=8, lo=0, hi=4000, kind="scalar", models=["icon"]),
    dict(id="hzerocl", name="Ničta izoterma", unit="m", bits=16, lo=0, hi=5000, kind="scalar", models=["icon"]),
    dict(id="t850", name="Temperatura 850 hPa", unit="°C", bits=16, lo=-40, hi=35, kind="scalar"),
    dict(id="wind850", name="Veter 850 hPa", unit="m/s", bits=8, lo=-80, hi=80, kind="vector"),
    dict(id="z500", name="Geopotencial 500 hPa", unit="dam", bits=16, lo=480, hi=600, kind="scalar"),
]


def make(layer_id: str, f: dict, prev: dict | None):
    """Display-unit array(s) for one frame from the (common-key) raw fields, or None."""
    try:
        if layer_id == "t2m":
            return f["t2m"] - 273.15
        if layer_id == "rh2m":
            return f["rh2m"]
        if layer_id == "precip":
            if prev is None:
                return None
            return np.clip(f["tp"] - prev["tp"], 0, None)
        if layer_id == "snow":
            return f["hsnow"] * 100.0
        if layer_id == "dbz":
            return f["dbz"]
        if layer_id == "wind10":
            return f["u10"], f["v10"]
        if layer_id == "gust":
            return f["gust"]
        if layer_id == "msl":
            return f["msl"] / 100.0
        if layer_id == "tcc":
            return f["tcc"]
        if layer_id == "cape":
            return f["cape"]
        if layer_id == "hzerocl":
            return f["hzerocl"]
        if layer_id == "t850":
            return f["t850"] - 273.15
        if layer_id == "wind850":
            return f["u850"], f["v850"]
        if layer_id == "z500":
            return f["z500"] / 98.0665  # m2/s2 -> dam
    except KeyError:
        return None
    raise ValueError(layer_id)


# ------------------------------------------------------------------------------------- helpers

def get(url: str, attempts: int = 6, timeout: int = 120) -> bytes:
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
        except Exception:
            pass
        time.sleep(min(30, 2 ** i) * (0.5 + random.random()))
    raise RuntimeError(f"failed: {url}")


def cached(url: str, name: str) -> bytes:
    p = CACHE / name
    if p.exists():
        return p.read_bytes()
    data = get(url)
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return data


def messages(data: bytes):
    """Yield (handle) for each GRIB message in a byte string; caller must not keep handles."""
    off = 0
    while True:
        i = data.find(b"GRIB", off)
        if i < 0:
            return
        ed = data[i + 7]
        if ed == 1:
            n = int.from_bytes(data[i + 4:i + 7], "big")
        else:
            n = int.from_bytes(data[i + 8:i + 16], "big")
        gid = eccodes.codes_new_from_message(data[i:i + n])
        try:
            yield gid
        finally:
            eccodes.codes_release(gid)
        off = i + n


def grid_of(gid):
    """Latitudes (north->south) and longitudes (west->east, wrapped to +-180) and the values array
    arranged north-first."""
    ni, nj = eccodes.codes_get(gid, "Ni"), eccodes.codes_get(gid, "Nj")
    la1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
    la2 = eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")
    lo1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
    lo2 = eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees")
    vals = eccodes.codes_get_values(gid).astype(np.float32).reshape(nj, ni)
    if eccodes.codes_get(gid, "bitmapPresent"):
        miss = eccodes.codes_get(gid, "missingValue")
        vals[vals == miss] = np.nan
    lo1 = (lo1 + 180) % 360 - 180
    lo2 = (lo2 + 180) % 360 - 180
    if lo2 < lo1:
        lo2 += 360
    lats = np.linspace(la1, la2, nj)
    lons = np.linspace(lo1, lo2, ni)
    if lats[0] < lats[-1]:
        lats, vals = lats[::-1], vals[::-1]
    return lats, lons, vals


def crop(lats, lons, vals):
    """Cut to the output domain (inclusive, small tolerance)."""
    tol = 1e-3
    jj = np.where((lats <= NORTH + tol) & (lats >= SOUTH - tol))[0]
    ii = np.where((lons >= WEST - tol) & (lons <= EAST + tol))[0]
    return lats[jj], lons[ii], vals[np.ix_(jj, ii)]


def encode(layer: dict, frames: list):
    lo, hi, bits = layer["lo"], layer["hi"], layer["bits"]
    shape = next((fr[0] if isinstance(fr, tuple) else fr).shape for fr in frames if fr is not None)
    h, w = shape
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


def write_model(model: str, title: str, source: str, run: datetime, steps: list[int],
                fields: dict[int, dict], lats, lons, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    meta_layers = []
    for layer in LAYERS:
        if "models" in layer and model not in layer["models"]:
            continue
        frames = [make(layer["id"], fields[s], fields[steps[i - 1]] if i else None) for i, s in enumerate(steps)]
        if all(fr is None for fr in frames):
            print(f"  {model}/{layer['id']}: no data, skipped", flush=True)
            continue
        img, missing = encode(layer, frames)
        img.save(out / f"{layer['id']}.png", optimize=True)
        meta_layers.append({k: v for k, v in layer.items() if k != "models"} | {"missing": missing})
    meta = {
        "model": title,
        "source": source,
        "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "steps": steps,
        "times": [(run + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%MZ") for s in steps],
        "grid": {"west": float(lons[0]), "north": float(lats[0]), "nx": len(lons), "ny": len(lats),
                 "dx": float((lons[-1] - lons[0]) / (len(lons) - 1)),
                 "dy": float((lats[0] - lats[-1]) / (len(lats) - 1))},
        "layers": meta_layers,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    size = sum(p.stat().st_size for p in out.glob("*.png")) / 1e6
    print(f"  {model}: {len(meta_layers)} layers, {len(steps)} steps, {size:.1f} MB", flush=True)


# ------------------------------------------------------------------------------------- ALADIN

ALADIN_KEYS = {  # (shortName, typeOfLevel, level) -> common key
    ("2t", "heightAboveGround", 2): "t2m", ("r", "heightAboveGround", 2): "rh2m",
    ("10u", "heightAboveGround", 10): "u10", ("10v", "heightAboveGround", 10): "v10",
    ("msl", "meanSea", 0): "msl", ("tcc", "surface", 0): "tcc", ("tp", "surface", 0): "tp",
    ("t", "isobaricInhPa", 850): "t850", ("u", "isobaricInhPa", 850): "u850",
    ("v", "isobaricInhPa", 850): "v850", ("z", "isobaricInhPa", 500): "z500",
}
FRACTION_TO_PERCENT = {"tcc", "rh2m"}  # ALADIN stores these as 0..1


def build_aladin(out: Path):
    listing = get(f"{ARSO}/").decode("utf-8", "replace")
    names = sorted(set(re.findall(r"nwp_(\d{8})-(\d{4})\.zip", listing)))
    if not names:
        raise RuntimeError("no ALADIN runs listed")
    d, hm = names[-1]
    run = datetime.strptime(d + hm, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    print(f"ALADIN/SI run {run:%Y-%m-%d %H} UTC", flush=True)
    z = zipfile.ZipFile(io.BytesIO(cached(f"{ARSO}/nwp_{d}-{hm}.zip", f"aladin_{d}{hm}.zip")))
    files = sorted(n for n in z.namelist() if n.endswith(".grb"))
    fields, steps, lats = {}, [], None
    for name in files:
        step = int(re.search(r"_(\d{4})\.grb$", name).group(1))
        f = {}
        for gid in messages(z.read(name)):
            key = ALADIN_KEYS.get((eccodes.codes_get(gid, "shortName"), eccodes.codes_get(gid, "typeOfLevel"),
                                   eccodes.codes_get(gid, "level")))
            if key is None:
                continue
            la, lo, v = grid_of(gid)
            la, lo, v = crop(la, lo, v)
            if key in FRACTION_TO_PERCENT:
                v = v * 100.0
            f[key] = v
            lats, lons = la, lo
        if step == 0:
            f.setdefault("tp", np.zeros_like(f["t2m"]))
        fields[step] = f
        steps.append(step)
    steps.sort()
    write_model("aladin", "ALADIN/SI", "ARSO", run, steps, fields, lats, lons, out)


# ------------------------------------------------------------------------------------- ICON-D2

ICON_SINGLE = {"t_2m": "t2m", "relhum_2m": "rh2m", "u_10m": "u10", "v_10m": "v10", "vmax_10m": "gust",
               "pmsl": "msl", "clct": "tcc", "tot_prec": "tp", "cape_ml": "cape", "dbz_cmax": "dbz",
               "hzerocl": "hzerocl", "h_snow": "hsnow"}
ICON_PRESSURE = {("t", 850): "t850", ("u", 850): "u850", ("v", 850): "v850", ("fi", 500): "z500"}


def icon_url(hh: str, run: str, step: int, field: str, level: int | None) -> str:
    if level is None:
        return f"{DWD}/{hh}/{field}/icon-d2_germany_regular-lat-lon_single-level_{run}_{step:03d}_2d_{field}.grib2.bz2"
    return f"{DWD}/{hh}/{field}/icon-d2_germany_regular-lat-lon_pressure-level_{run}_{step:03d}_{level}_{field}.grib2.bz2"


def icon_latest() -> tuple[str, str, int]:
    """(hh, run 'YYYYMMDDHH', max step) of the newest run whose files are all present."""
    best = None
    for h in range(0, 24, 3):
        hh = f"{h:02d}"
        try:
            lst = get(f"{DWD}/{hh}/t_2m/", attempts=3).decode()
        except Exception:
            continue
        runs = re.findall(r"regular-lat-lon_single-level_(\d{10})_(\d{3})_2d_t_2m", lst)
        if not runs:
            continue
        run = max(r for r, _ in runs)
        mx = max(int(s) for r, s in runs if r == run)
        if best is None or run > best[1]:
            best = (hh, run, mx)
    if best is None:
        raise RuntimeError("no ICON-D2 runs found")
    # confirm every field dir has this run's last step; otherwise fall back 3 h
    for _ in range(4):
        hh, run, mx = best
        ok = True
        for field in list(ICON_SINGLE) + sorted({f for f, _ in ICON_PRESSURE}):
            lst = get(f"{DWD}/{hh}/{field}/", attempts=3).decode()
            if f"_{run}_{mx:03d}_" not in lst:
                ok = False
                break
        if ok:
            return best
        prev = datetime.strptime(run, "%Y%m%d%H") - timedelta(hours=3)
        hh2 = f"{prev.hour:02d}"
        lst = get(f"{DWD}/{hh2}/t_2m/", attempts=3).decode()
        runs = re.findall(r"regular-lat-lon_single-level_(\d{10})_(\d{3})_2d_t_2m", lst)
        r2 = prev.strftime("%Y%m%d%H")
        best = (hh2, r2, max(int(s) for r, s in runs if r == r2))
    raise RuntimeError("no complete ICON-D2 run")


def icon_fetch(url: str):
    try:
        raw = bz2.decompress(get(url))
    except urllib.error.HTTPError:
        return None
    for gid in messages(raw):
        la, lo, v = grid_of(gid)
        return crop(la, lo, v)
    return None


def build_icon(out: Path, relief_out: Path, workers: int):
    hh, run, mx = icon_latest()
    rdt = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    steps = list(range(0, mx + 1))
    print(f"ICON-D2 run {rdt:%Y-%m-%d %H} UTC, +{mx} h", flush=True)
    jobs = {}
    for s in steps:
        for field, key in ICON_SINGLE.items():
            jobs[(s, key)] = icon_url(hh, run, s, field, None)
        for (field, lev), key in ICON_PRESSURE.items():
            jobs[(s, key)] = icon_url(hh, run, s, field, lev)
    fields = {s: {} for s in steps}
    lats = lons = None
    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(icon_fetch, u): k for k, u in jobs.items()}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            res = fut.result()
            if res is not None:
                lats, lons, v = res
                fields[futs[fut][0]][futs[fut][1]] = v
            if i % 100 == 0:
                print(f"  {i}/{len(jobs)} files ({time.time() - t0:.0f}s)", flush=True)
    fields[0].setdefault("tp", np.zeros_like(fields[0]["t2m"]))
    write_model("icon", "ICON-D2", "DWD", rdt, steps, fields, lats, lons, out)

    # relief (hillshade) + land fraction from time-invariant fields of the same run
    inv = {}
    for field in ("hsurf", "fr_land"):
        url = f"{DWD}/{hh}/{field}/icon-d2_germany_regular-lat-lon_time-invariant_{run}_000_0_{field}.grib2.bz2"
        res = icon_fetch(url)
        if res is not None:
            inv[field] = res
    if "hsurf" in inv:
        la, lo, z = inv["hsurf"]
        z = np.nan_to_num(z, nan=0.0)
        dyk = (la[0] - la[1]) * 111320.0
        dxk = (lo[1] - lo[0]) * 111320.0 * np.cos(np.radians(la))[:, None]
        gy, gx = np.gradient(z)
        sx, sy = gx / dxk, -gy / dyk  # east and north slopes
        az, alt = np.radians(315), np.radians(40)
        nx_, ny_, nz_ = -sx, -sy, np.ones_like(z)
        norm = np.sqrt(nx_ ** 2 + ny_ ** 2 + 1)
        shade = (nx_ * np.cos(alt) * np.sin(az) + ny_ * np.cos(alt) * np.cos(az) + nz_ * np.sin(alt)) / norm
        shade = np.clip(shade / np.sin(alt), 0, 1.6) / 1.6
        land = inv.get("fr_land", (la, lo, np.ones_like(z)))[2]
        rgb = np.dstack([np.round(shade * 255), np.round(np.clip(np.nan_to_num(land), 0, 1) * 255),
                         np.round(np.clip(z / 3000.0, 0, 1) * 255)]).astype(np.uint8)
        relief_out.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb, "RGB").save(relief_out / "relief.png", optimize=True)
        (relief_out / "relief.json").write_text(json.dumps({
            "west": float(lo[0]), "north": float(la[0]), "nx": len(lo), "ny": len(la),
            "dx": float((lo[-1] - lo[0]) / (len(lo) - 1)), "dy": float((la[0] - la[-1]) / (len(la) - 1)),
            "channels": "R hillshade 0-255, G land fraction 0-255, B model height 0-3000 m"}))
        print("  relief written", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/slovenija/data")
    ap.add_argument("--models", default="aladin,icon")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    out = Path(args.out)
    failed = []
    for m in args.models.split(","):
        try:
            if m == "aladin":
                build_aladin(out / "aladin")
            elif m == "icon":
                build_icon(out / "icon", out, args.workers)
        except Exception as e:  # one model failing must not stop the other
            print(f"!! {m} failed: {e}", flush=True)
            failed.append(m)
    return 1 if len(failed) == len(args.models.split(",")) else 0


if __name__ == "__main__":
    sys.exit(main())
