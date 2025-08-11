# ============================================================
# Melbourne Parking Sensors → Clean JSON/CSV (VS Code version)
# v1 Opendatasoft API + hard-coded API key
# ============================================================

import os
import time
import json
import csv  # not strictly needed, but kept for parity
import zipfile
from typing import Dict, Any, List, Tuple
import requests
import pandas as pd

# --------- EDIT if you rotate your key ----------
API_KEY = "8272f3e3cf855ca3006bd9d38e135f06a0714adb8519bf503af526af"
# ------------------------------------------------

BASE_V1 = "https://data.melbourne.vic.gov.au/api/records/1.0/search/"
DATASET_SENSORS = "on-street-parking-bay-sensors"   # main dataset
LIMIT = 1000       # v1 allows up to 1000 rows/page reliably
SLEEP = 0.4        # small polite delay between pages
TIMEOUT = 30


def req(params: Dict[str, Any]) -> requests.Response:
    headers = {}
    if API_KEY:
        # Opendatasoft commonly accepts apikey in header or query:
        headers["Authorization"] = f"Apikey {API_KEY}"
    r = requests.get(BASE_V1, params=params, headers=headers, timeout=TIMEOUT)
    # If provider ignores Authorization, try ?apikey param:
    if r.status_code in (401, 403):
        p2 = dict(params)
        p2["apikey"] = API_KEY
        r = requests.get(BASE_V1, params=p2, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def v1_get_count(dataset_id: str) -> int:
    # rows=0 returns metadata including nhits
    r = req({"dataset": dataset_id, "rows": 0})
    payload = r.json()
    return int(payload.get("nhits", 0))


def v1_fetch_page(dataset_id: str, start: int, rows: int) -> Dict[str, Any]:
    params = {
        "dataset": dataset_id,
        "rows": rows,
        "start": start,
        # optional: specify timezone/lang if you want
        # "timezone": "UTC",
        # "lang": "en",
    }
    r = req(params)
    return r.json()


def v1_extract_fields_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    v1 format:
      { "nhits": N, "records": [ {"fields": {...}}, ... ] }
    We keep only the "fields" object per record.
    """
    out = []
    for rec in payload.get("records", []):
        fields = rec.get("fields", {})
        if isinstance(fields, dict):
            out.append(fields)
    return out


def extract_lat_lon(location: Any):
    """
    Handles shapes like:
      - { "lat": -37.81, "lon": 144.96 }
      - { "latitude": .., "longitude": .. }
      - { "type": "Point", "coordinates": [lon, lat] }
      - [lat, lon]  (seen in some Opendatasoft geo fields)
    Returns (lat, lon) or (None, None)
    """
    if location is None:
        return None, None
    # dict shapes
    if isinstance(location, dict):
        if "lat" in location and "lon" in location:
            return location["lat"], location["lon"]
        if "latitude" in location and "longitude" in location:
            return location["latitude"], location["longitude"]
        coords = location.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
            return lat, lon
        return None, None
    # list/tuple shape [lat, lon]
    if isinstance(location, (list, tuple)) and len(location) >= 2:
        lat, lon = location[0], location[1]
        return lat, lon
    return None, None


def to_bool_status(text: str):
    if not text:
        return None
    t = str(text).strip().lower()
    if t in ("occupied", "true", "yes"):
        return True
    if t in ("unoccupied", "false", "no", "available", "free"):
        return False
    # treat e.g. "present" as unknown
    return None


def transform_row(rec: Dict[str, Any]) -> Dict[str, Any]:
    bay_id = rec.get("kerbsideid") or rec.get("bay_id")
    status_text = rec.get("status_description") or rec.get("status") or ""
    status = to_bool_status(status_text)
    zone_number = rec.get("zone_number")
    updated_at = rec.get("lastupdated") or rec.get("status_timestamp")
    lat, lon = extract_lat_lon(rec.get("location"))
    return {
        "bay_id": bay_id,
        "status": status,           # True = occupied, False = free, None = unknown
        "status_text": status_text,
        "lat": lat,
        "lon": lon,
        "zone_number": zone_number,
        "updated_at": updated_at,
    }


def write_json(obj: Any, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def zip_dir(dir_path: str, zip_path: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files_ in os.walk(dir_path):
            for name in files_:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, start=os.path.dirname(dir_path))
                z.write(full, rel)


def main():
    # 1) Count total
    print("Counting total records …")
    total = v1_get_count(DATASET_SENSORS)
    if total == 0:
        raise RuntimeError("nhits == 0 — dataset not reachable with current key. Double-check API_KEY.")
    print(f"Total nhits: {total:,}")

    # 2) Fetch ALL pages
    os.makedirs("raw", exist_ok=True)
    all_fields: List[Dict[str, Any]] = []

    start = 0
    page = 1
    while start < total:
        payload = v1_fetch_page(DATASET_SENSORS, start=start, rows=LIMIT)
        if page == 1:
            write_json(payload, f"raw/{DATASET_SENSORS}_page_0001.json")
        fields_list = v1_extract_fields_list(payload)
        all_fields.extend(fields_list)
        print(f"  page {page:>3}  start={start:>6}  got={len(fields_list):>4}  total={len(all_fields):>6}")
        if not fields_list:
            break
        start += LIMIT
        page += 1
        time.sleep(SLEEP)

    # Safety: trim if provider added more during paging
    all_fields = all_fields[:total]
    write_json({"records": all_fields}, f"raw/{DATASET_SENSORS}_all_records.json")
    print(f"Saved raw/{DATASET_SENSORS}_all_records.json with {len(all_fields):,} records")

    # 3) Transform -> exact schema
    processed = [transform_row(r) for r in all_fields]

    os.makedirs("processed", exist_ok=True)
    OUT_JSON = "processed/parking_sensors.json"
    OUT_CSV  = "processed/parking_sensors.csv"

    write_json(processed, OUT_JSON)
    df = pd.DataFrame(processed, columns=["bay_id","status","status_text","lat","lon","zone_number","updated_at"])
    df.to_csv(OUT_CSV, index=False)

    print("\nProcessed outputs:")
    print(f"   - {OUT_JSON}  ({len(df):,} rows)")
    print(f"   - {OUT_CSV}")

    print("\nPreview (first 5 rows):")
    try:
        # Safe console preview
        print(df.head(5).to_string(index=False))
    except Exception:
        pass

    # 4) Package
    RAW_ZIP = "raw_export.zip"
    PROC_ZIP = "processed_export.zip"
    zip_dir("raw", RAW_ZIP)
    zip_dir("processed", PROC_ZIP)

    print("\nZipped:")
    print(f"   - {RAW_ZIP}")
    print(f"   - {PROC_ZIP}")

    print("\nDone.")


if __name__ == "__main__":
    main()
