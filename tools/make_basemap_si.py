"""Basemap for the Slovenia page -> site/slovenija/basemap-si.json.

Input: Natural Earth 1:10m GeoJSON (public domain) from
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson
  ne_10m_admin_0_boundary_lines_land, ne_10m_coastline, ne_10m_rivers_lake_centerlines, ne_10m_lakes

  python tools/make_basemap_si.py <folder with the geojson files> > site/slovenija/basemap-si.json
"""
import json
import sys
from pathlib import Path

W, E, S, N = 11.2, 18.3, 44.3, 47.8


def lines_of(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    if t == "Polygon":
        return c
    if t == "MultiPolygon":
        return [r for p in c for r in p]
    return []


def clip(line):
    inside = [W <= p[0] <= E and S <= p[1] <= N for p in line]
    runs, cur = [], []
    for i, p in enumerate(line):
        near = inside[i] or (i and inside[i - 1]) or (i + 1 < len(line) and inside[i + 1])
        if near:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [[v for p in r for v in (round(p[0], 3), round(p[1], 3))] for r in runs if len(r) > 1]


def load(folder, name, keep=lambda props: True):
    gj = json.load(open(Path(folder) / f"{name}.geojson"))
    out = []
    for f in gj["features"]:
        if f.get("geometry") and keep(f.get("properties") or {}):
            for ln in lines_of(f["geometry"]):
                out += clip(ln)
    return out


def main():
    d = sys.argv[1]
    res = {
        "borders": load(d, "ne_10m_admin_0_boundary_lines_land"),
        "coast": load(d, "ne_10m_coastline"),
        "rivers": load(d, "ne_10m_rivers_lake_centerlines",
                       lambda p: p.get("featurecla", "").startswith("River") and (p.get("scalerank") or 99) <= 9),
        "lakes": load(d, "ne_10m_lakes"),
    }
    json.dump(res, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
