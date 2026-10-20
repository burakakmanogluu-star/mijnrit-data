#!/usr/bin/env python3
"""
GVB ön işleme betiği.

OVapi'nin ülke çapındaki sabit GTFS paketinden (gtfs-nl.zip, ~250 MB) yalnızca
GVB'yi süzer ve uygulamaya gömülecek küçük bir JSON üretir:

  routes    hat no, uzun ad, tür (GTFS route_type), renk
  stops     yalnızca GVB seferlerinin uğradığı duraklar
  shapes    hat çizgileri (sadeleştirilmiş, Google polyline kodlu, 1e-5 hassasiyet)
  patterns  (hat, yön, şekil, durak dizisi) kalıpları: her durağın şekil üzerindeki
            metresi ve ilk duraktan itibaren tarifedeki ortanca süre (sn)
  trips     trip_id -> pattern indeksi (yalnızca --days gün içinde çalışan seferler)

Kullanım:
  python3 scripts/gvb_preprocess.py gtfs-nl.zip
  python3 scripts/gvb_preprocess.py --download          # zip'i OVapi'den indirir
  python3 scripts/gvb_preprocess.py gtfs-nl.zip --days 10 --out AmsterdamCanli/Resources/gvb.json

Yalnızca Python standart kütüphanesi kullanılır.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

GTFS_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
USER_AGENT = "AmsterdamCanli-preprocess/0.1 (+https://burakakmanoglu.com)"

# GVB metro hatlarının bilinen renkleri; diğerleri için GTFS'te renk yoksa palet kullanılır.
METRO_COLORS = {
    "50": "2BA84A",  # groen
    "51": "F28C28",  # oranje
    "52": "1F9BD7",  # blauw
    "53": "E2231A",  # rood
    "54": "F7C600",  # geel
}
TRAM_PALETTE = [
    "D7263D", "1B998B", "2E86AB", "F46036", "8F2D56", "3F88C5", "E07A5F", "5B8E7D",
    "C1292E", "6A4C93", "00A6A6", "D1495B", "EDAE49", "00798C", "30638E", "B5446E",
]
BUS_COLOR = "4A5568"
NIGHT_BUS_COLOR = "2D3748"
FERRY_COLOR = "0077B6"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def open_csv(zf: zipfile.ZipFile, name: str):
    if name not in zf.namelist():
        return None
    raw = zf.open(name)
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    return csv.DictReader(text)


def parse_time(t: str) -> int | None:
    if not t:
        return None
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def mode_of(route_type: int) -> str:
    if route_type in (0, 5, 900, 901, 902, 903, 904, 905, 906):
        return "tram"
    if route_type in (1, 400, 401, 402, 403, 404, 405):
        return "metro"
    if route_type in (4, 1000, 1200):
        return "ferry"
    if route_type in (2, 100, 101, 102, 103, 106, 109):
        return "rail"
    return "bus"


def default_color(short: str, route_type: int, tram_idx: int) -> str:
    mode = mode_of(route_type)
    if mode == "metro":
        return METRO_COLORS.get(short, "7B2D8E")
    if mode == "tram":
        return TRAM_PALETTE[tram_idx % len(TRAM_PALETTE)]
    if mode == "ferry":
        return FERRY_COLOR
    if short.upper().startswith("N"):
        return NIGHT_BUS_COLOR
    return BUS_COLOR


# --- geometri -----------------------------------------------------------------

EARTH_R = 6371008.8


class LocalProj:
    """Amsterdam ölçeğinde yeterli eşdikdörtgen projeksiyon (metre)."""

    def __init__(self, lat0: float):
        self.kx = math.cos(math.radians(lat0)) * math.pi / 180 * EARTH_R
        self.ky = math.pi / 180 * EARTH_R

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky


def simplify(points: list[tuple[float, float]], proj: LocalProj, tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker (yinelemeli)."""
    n = len(points)
    if n < 3:
        return points
    xy = [proj.xy(lat, lon) for lat, lon in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol_m * tol_m
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if seg2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                qx, qy = ax + t * dx, ay + t * dy
                d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 > best:
                best, best_i = d2, i
        if best > tol2 and best_i > 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(points, keep) if k]


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10 ** precision
    out = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for delta in (ilat - plat, ilon - plon):
            v = ~(delta << 1) if delta < 0 else (delta << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    """Kodlanmış hattı, uygulamanın göreceği yuvarlanmış koordinatlara çevirir."""
    factor = 10 ** precision
    coords, idx, lat, lon = [], 0, 0, 0
    while idx < len(s):
        vals = []
        for _ in range(2):
            result = shift = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        coords.append((lat / factor, lon / factor))
    return coords


def cumulative(points_xy: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(points_xy, points_xy[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum


def project_stops(shape_xy, cum, stops_xy) -> list[float]:
    """Durakları şekil üzerine sırayla (ileri doğru) oturtur, metre döndürür."""
    result = []
    start_seg = 0
    nseg = len(shape_xy) - 1
    for px, py in stops_xy:
        best_d2, best_dist, best_seg = float("inf"), cum[start_seg] if nseg >= 0 else 0.0, start_seg
        for i in range(start_seg, max(nseg, 0)):
            ax, ay = shape_xy[i]
            bx, by = shape_xy[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx, qy = ax + t * dx, ay + t * dy
            d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 < best_d2:
                best_d2, best_seg = d2, i
                best_dist = cum[i] + t * math.sqrt(seg2)
            # Çok uzaklaştıysak (en iyi eşleşmeden 3 km sonra) dur: dönüş yapan hatlarda
            # aynı yerin ikinci geçişine atlamayı engeller.
            if cum[i] - best_dist > 3000 and best_d2 < 2500:
                break
        if nseg <= 0:
            best_dist = 0.0
        result.append(round(best_dist, 1))
        start_seg = best_seg
    return result


# --- ana akış -----------------------------------------------------------------

def download(dest: str) -> None:
    log(f"İndiriliyor: {GTFS_URL}")
    req = urllib.request.Request(GTFS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)


def service_dates(zf: zipfile.ZipFile, start: date, days: int) -> dict[str, set[str]]:
    """[start, start+days) aralığında her service_id'nin çalıştığı günler (YYYYMMDD)."""
    window = [(start + timedelta(d)) for d in range(days)]
    weekday_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    out: dict[str, set[str]] = defaultdict(set)
    removed: dict[str, set[str]] = defaultdict(set)
    added: dict[str, set[str]] = defaultdict(set)
    keys = {d.strftime("%Y%m%d") for d in window}
    cd = open_csv(zf, "calendar_dates.txt")
    if cd:
        for row in cd:
            d = row["date"]
            if d in keys:
                (added if row["exception_type"] == "1" else removed)[row["service_id"]].add(d)
    cal = open_csv(zf, "calendar.txt")
    for row in (cal or []):
        sid = row["service_id"]
        s = datetime.strptime(row["start_date"], "%Y%m%d").date()
        e = datetime.strptime(row["end_date"], "%Y%m%d").date()
        for day in window:
            k = day.strftime("%Y%m%d")
            if s <= day <= e and row[weekday_cols[day.weekday()]] == "1" and k not in removed[sid]:
                out[sid].add(k)
    for sid, ds in added.items():
        out[sid] |= ds
    return out


def active_services(zf: zipfile.ZipFile, start: date, days: int) -> set[str] | None:
    """[start, start+days) aralığında en az bir gün çalışan service_id'ler. days<=0 -> hepsi."""
    if days <= 0:
        return None
    window = {(start + timedelta(d)).strftime("%Y%m%d") for d in range(days)}
    weekday_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active: set[str] = set()
    removed: dict[str, set[str]] = defaultdict(set)

    cal = open_csv(zf, "calendar.txt")
    calendar_rows = list(cal) if cal else []
    cd = open_csv(zf, "calendar_dates.txt")
    if cd:
        for row in cd:
            d = row["date"]
            if d not in window:
                continue
            if row["exception_type"] == "1":
                active.add(row["service_id"])
            else:
                removed[row["service_id"]].add(d)
    for row in calendar_rows:
        sid = row["service_id"]
        s = datetime.strptime(row["start_date"], "%Y%m%d").date()
        e = datetime.strptime(row["end_date"], "%Y%m%d").date()
        for d in range(days):
            day = start + timedelta(d)
            if s <= day <= e and row[weekday_cols[day.weekday()]] == "1" and day.strftime("%Y%m%d") not in removed[sid]:
                active.add(sid)
                break
    return active


def main() -> int:
    ap = argparse.ArgumentParser(description="gtfs-nl.zip -> GVB JSON")
    ap.add_argument("zip", nargs="?", help="gtfs-nl.zip yolu")
    ap.add_argument("--download", action="store_true", help="zip'i OVapi'den indir")
    ap.add_argument("--agency", default="GVB")
    ap.add_argument("--days", type=int, default=8, help="trips eşlemesine kaç günlük sefer girsin (0 = hepsi)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (varsayılan: bugün, Europe/Amsterdam ~ UTC)")
    ap.add_argument("--tolerance", type=float, default=1.5, help="hat sadeleştirme toleransı (m)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "AmsterdamCanli", "Resources", "gvb.json"))
    args = ap.parse_args()

    zip_path = args.zip
    if args.download:
        zip_path = zip_path or "gtfs-nl.zip"
        download(zip_path)
    if not zip_path or not os.path.exists(zip_path):
        ap.error("gtfs-nl.zip yolu gerekli (ya da --download)")

    t0 = time.time()
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else datetime.now(timezone.utc).date()
    zf = zipfile.ZipFile(zip_path)

    # 1) agency
    agency_ids = set()
    for row in open_csv(zf, "agency.txt"):
        if row.get("agency_id", "").upper() == args.agency.upper() or args.agency.upper() in row.get("agency_name", "").upper().split():
            agency_ids.add(row.get("agency_id", ""))
    if not agency_ids:
        log(f"Ajans bulunamadı: {args.agency}")
        return 1
    log(f"Ajans: {sorted(agency_ids)}")

    # 2) routes
    routes = {}
    for row in open_csv(zf, "routes.txt"):
        if row.get("agency_id", "") in agency_ids:
            routes[row["route_id"]] = row
    log(f"GVB hat: {len(routes)}")

    # 3) servisler
    services = active_services(zf, start, args.days)
    if services is not None:
        log(f"{start} itibarıyla {args.days} günde aktif servis: {len(services)}")

    # 4) trips
    trips = {}
    for row in open_csv(zf, "trips.txt"):
        if row["route_id"] in routes and (services is None or row["service_id"] in services):
            trips[row["trip_id"]] = row
    log(f"GVB sefer ({'hepsi' if services is None else f'{args.days} gün'}): {len(trips)}")

    # 5) stop_times (büyük dosya, akış halinde)
    st: dict[str, list[tuple[int, str, int | None]]] = defaultdict(list)
    n = 0
    for row in open_csv(zf, "stop_times.txt"):
        n += 1
        if n % 5_000_000 == 0:
            log(f"  stop_times: {n / 1e6:.0f} M satır")
        tid = row["trip_id"]
        if tid in trips:
            dep = parse_time(row.get("departure_time") or row.get("arrival_time") or "")
            st[tid].append((int(row["stop_sequence"]), row["stop_id"], dep))
    log(f"stop_times tarandı: {n} satır, GVB sefer ile: {len(st)}")

    # 6) kalıplar
    pattern_key_to_idx: dict[tuple, int] = {}
    pattern_rows: list[dict] = []
    pattern_offsets: list[list[list[int]]] = []
    trip_to_pattern: dict[str, int] = {}
    for tid, rows in st.items():
        rows.sort()
        trip = trips[tid]
        stop_ids = tuple(r[1] for r in rows)
        key = (trip["route_id"], trip.get("direction_id", "0") or "0", trip.get("shape_id", ""), stop_ids)
        idx = pattern_key_to_idx.get(key)
        if idx is None:
            idx = len(pattern_rows)
            pattern_key_to_idx[key] = idx
            pattern_rows.append({
                "route_id": trip["route_id"],
                "direction": int(trip.get("direction_id") or 0),
                "shape_id": trip.get("shape_id", ""),
                "headsign": trip.get("trip_headsign", "") or "",
                "stops": stop_ids,
            })
            pattern_offsets.append([[] for _ in stop_ids])
        trip_to_pattern[tid] = idx
        t_first = rows[0][2]
        if t_first is not None and len(pattern_offsets[idx][0]) < 60:
            for i, r in enumerate(rows):
                if r[2] is not None:
                    pattern_offsets[idx][i].append(r[2] - t_first)
    log(f"Kalıp: {len(pattern_rows)}")

    # 6b) tarife: her gün için (kalıp, ilk durak kalkış saniyesi) — "henüz yola çıkmamış" seferler ve son sefer uyarısı için
    sched: dict[str, dict[str, list[int]]] = {}
    if args.days > 0:
        sdates = service_dates(zf, start, args.days)
        per_day: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for tid, rows in st.items():
            t_first = rows[0][2]
            if t_first is None:
                continue
            for d in sdates.get(trips[tid]["service_id"], ()):
                per_day[d].append((trip_to_pattern[tid], t_first))
        for d, items in sorted(per_day.items()):
            items.sort(key=lambda x: (x[1], x[0]))
            sched[d] = {"p": [p for p, _ in items], "s": [t for _, t in items]}
        log(f"Tarife günleri: {len(sched)}, toplam sefer-gün: {sum(len(v['p']) for v in sched.values())}")

    # 7) shapes
    needed_shapes = {p["shape_id"] for p in pattern_rows if p["shape_id"]}
    raw_shapes: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in open_csv(zf, "shapes.txt") or []:
        sid = row["shape_id"]
        if sid in needed_shapes:
            raw_shapes[sid].append((int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"])))
    log(f"Şekil: {len(raw_shapes)} / gereken {len(needed_shapes)}")

    # 8) stops
    needed_stops = {s for p in pattern_rows for s in p["stops"]}
    stops_rows = {}
    for row in open_csv(zf, "stops.txt"):
        if row["stop_id"] in needed_stops:
            stops_rows[row["stop_id"]] = row
    stop_ids_sorted = sorted(stops_rows)
    stop_index = {sid: i for i, sid in enumerate(stop_ids_sorted)}

    proj = LocalProj(52.37)
    shape_ids_sorted = sorted(raw_shapes)
    shape_index = {sid: i for i, sid in enumerate(shape_ids_sorted)}
    shapes_out, shape_geom = [], {}
    pts_before = pts_after = 0
    for sid in shape_ids_sorted:
        pts = [(lat, lon) for _, lat, lon in sorted(raw_shapes[sid])]
        simp = simplify(pts, proj, args.tolerance)
        enc = encode_polyline(simp)
        # Uygulama yuvarlanmış koordinatları göreceği için mesafeleri onlardan hesapla.
        dec = decode_polyline(enc)
        xy = [proj.xy(lat, lon) for lat, lon in dec]
        shape_geom[sid] = (xy, cumulative(xy))
        pts_before += len(pts)
        pts_after += len(simp)
        shapes_out.append({"id": sid, "poly": enc})
    log(f"Şekil noktası: {pts_before} -> {pts_after} (tolerans {args.tolerance} m)")

    # 9) route indeksleri ve renkler
    route_ids_sorted = sorted(routes, key=lambda rid: (int(routes[rid].get("route_type") or 3), routes[rid].get("route_short_name", "").zfill(4), rid))
    route_index = {rid: i for i, rid in enumerate(route_ids_sorted)}
    routes_out = []
    tram_i = 0
    for rid in route_ids_sorted:
        r = routes[rid]
        rtype = int(r.get("route_type") or 3)
        short = (r.get("route_short_name") or "").strip()
        color = (r.get("route_color") or "").strip().upper()
        if mode_of(rtype) == "tram" and not color:
            col = default_color(short, rtype, tram_i)
            tram_i += 1
        else:
            col = color or default_color(short, rtype, 0)
        # Metro için GTFS rengi yoksa ya da beyazsa bilinen renkleri kullan.
        if mode_of(rtype) == "metro" and short in METRO_COLORS and (not color or color in ("FFFFFF", "000000")):
            col = METRO_COLORS[short]
        routes_out.append({
            "id": rid,
            "short": short,
            "long": (r.get("route_long_name") or "").strip(),
            "type": rtype,
            "color": col,
            "text": (r.get("route_text_color") or "FFFFFF").strip().upper() or "FFFFFF",
        })

    # 10) kalıp çıktısı
    patterns_out = []
    for p, offs in zip(pattern_rows, pattern_offsets):
        sid = p["shape_id"]
        if sid in shape_geom:
            xy, cum = shape_geom[sid]
            stops_xy = [proj.xy(float(stops_rows[s]["stop_lat"]), float(stops_rows[s]["stop_lon"])) for s in p["stops"]]
            sd = project_stops(xy, cum, stops_xy)
            s_idx = shape_index[sid]
        else:
            sd, s_idx = [], -1
        times = [int(statistics.median(o)) if o else -1 for o in offs]
        patterns_out.append({
            "r": route_index[p["route_id"]],
            "d": p["direction"],
            "s": s_idx,
            "h": p["headsign"],
            "st": [stop_index[s] for s in p["stops"]],
            "t": times,
            "sd": sd,
        })

    stops_out = [{
        "id": sid,
        "name": stops_rows[sid].get("stop_name", ""),
        "lat": round(float(stops_rows[sid]["stop_lat"]), 6),
        "lon": round(float(stops_rows[sid]["stop_lon"]), 6),
    } for sid in stop_ids_sorted]

    end = start + timedelta(days=max(args.days, 1) - 1)
    out = {
        "version": 1,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "OVapi gtfs-nl.zip",
        "agency": args.agency,
        "validFrom": start.strftime("%Y-%m-%d"),
        "validTo": end.strftime("%Y-%m-%d") if args.days > 0 else None,
        "routes": routes_out,
        "stops": stops_out,
        "shapes": shapes_out,
        "patterns": patterns_out,
        "trips": trip_to_pattern,
        "sched": sched,
    }
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # Rapor
    def part_size(key):
        return len(json.dumps(out[key], ensure_ascii=False, separators=(",", ":")).encode())

    size = os.path.getsize(out_path)
    by_mode = defaultdict(int)
    for r in routes_out:
        by_mode[mode_of(r["type"])] += 1
    log(f"Yazıldı: {out_path}")
    print(json.dumps({
        "file": out_path,
        "bytes": size,
        "mb": round(size / 1e6, 2),
        "routes": len(routes_out),
        "routes_by_mode": dict(by_mode),
        "stops": len(stops_out),
        "shapes": len(shapes_out),
        "patterns": len(patterns_out),
        "trips": len(trip_to_pattern),
        "part_bytes": {k: part_size(k) for k in ("routes", "stops", "shapes", "patterns", "trips", "sched")},
        "seconds": round(time.time() - t0, 1),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
