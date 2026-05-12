"""Pre-geocode all listings for the map view. Run once after scraping data."""
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import Storage  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "listings.db"


def geocode_address(address: str) -> tuple[float, float] | None:
    """Call Photon (Komoot), return (lat, lng) or None."""
    from urllib.request import Request
    url = f"https://photon.komoot.io/api/?q={quote(address)}&limit=1"
    req = Request(url, headers={"User-Agent": "Holland2StayMonitor/1.0"})
    try:
        resp = urlopen(req, timeout=8)
        data = json.loads(resp.read().decode())
        feats = data.get("features", [])
        if feats:
            coords = feats[0]["geometry"]["coordinates"]
            return float(coords[1]), float(coords[0])  # [lng, lat] → (lat, lng)
    except Exception as e:
        print(f"  ⚠ Geocode failed: {e}")
    return None


def main() -> None:
    storage = Storage(DB)
    try:
        listings = storage.get_map_listings()
    finally:
        storage.close()

    total = len(listings)
    cached_count = 0
    new_count = 0
    failed = 0

    print(f"Geocoding {total} listings…\n")

    for i, l in enumerate(listings, 1):
        addr = l["address"]

        st = Storage(DB)
        try:
            cached = st.get_cached_coords(addr)
        finally:
            st.close()

        if cached:
            cached_count += 1
            continue

        print(f"  [{i}/{total}] {l['name'][:50]}…", end=" ", flush=True)
        coords = geocode_address(addr)

        if coords:
            lat, lng = coords
            st = Storage(DB)
            try:
                st.cache_coords(addr, lat, lng)
            finally:
                st.close()
            new_count += 1
            print(f"({lat:.4f}, {lng:.4f}) ✓")
        else:
            failed += 1
            print("✗")

        if i < total:
            time.sleep(0.15)

    print(f"\nDone: {cached_count} cached, {new_count} new, {failed} failed")


if __name__ == "__main__":
    main()
