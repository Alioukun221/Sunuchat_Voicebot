"""Structures de santé proches d'un point, via OpenStreetMap (gratuit, sans clé d'API).

- Nominatim : localité (« Pikine », « Podor ») → coordonnées ;
- Overpass : postes de santé, centres de santé, cliniques et hôpitaux autour d'un point.

Les données OSM du Sénégal sont bonnes en ville et plus lacunaires en brousse : on élargit
le rayon de recherche tant qu'on ne trouve rien.
"""

import logging
import math

import httpx

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Nominatim et Overpass demandent un User-Agent qui identifie l'application.
HEADERS = {"User-Agent": "sunuchat/0.1 (assistant sante WhatsApp, Senegal)"}
RADII_M = (3000, 10000, 30000)

KINDS = {
    "hospital": "hôpital",
    "clinic": "centre de santé",
    "centre": "centre de santé",
    "health_post": "poste de santé",
    "doctors": "cabinet médical",
    "doctor": "cabinet médical",
}


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance à vol d'oiseau (formule de haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def distance_text(km: float) -> str:
    """« 600 m » sous un kilomètre (plus naturel à l'oral que « 0,6 km »), sinon « 2,3 km »."""
    if km < 1:
        return f"{max(50, round(km * 1000, -1)):.0f} m"
    return f"{km:.1f} km".replace(".", ",").replace(",0 km", " km")


def maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


async def geocode(place: str) -> tuple[float, float] | None:
    """Coordonnées d'une localité du Sénégal, ou None."""
    params = {"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "sn"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            r = await client.get(NOMINATIM_URL, params=params)
        r.raise_for_status()
        results = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Géocodage impossible (%s) : %s", place, e)
        return None
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


async def region_of(place: str) -> str | None:
    """Région administrative d'une localité du Sénégal (« Kédougou » pour « Saraya »), ou
    None. Sert à n'envoyer une alerte régionale qu'aux personnes concernées."""
    params = {
        "q": place,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "sn",
        "addressdetails": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            r = await client.get(NOMINATIM_URL, params=params)
        r.raise_for_status()
        results = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Région introuvable (%s) : %s", place, e)
        return None
    if not results:
        return None
    address = results[0].get("address", {})
    region = address.get("state") or address.get("region")
    if not region:
        return None
    return region.removeprefix("Région de ").removeprefix("Région du ").strip()


def _query(lat: float, lon: float, radius: int) -> str:
    around = f"(around:{radius},{lat},{lon})"
    return (
        "[out:json][timeout:25];("
        f'nwr{around}["amenity"~"^(hospital|clinic|doctors)$"];'
        f'nwr{around}["healthcare"~"^(hospital|clinic|centre|health_post|doctor)$"];'
        ");out center tags;"
    )


def parse_facilities(data: dict, lat: float, lon: float, limit: int = 3) -> list[dict]:
    """Réponse Overpass → structures nommées, de la plus proche à la plus lointaine."""
    found: dict[str, dict] = {}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name:fr") or tags.get("name")
        center = el.get("center", el)
        if not name or "lat" not in center:
            continue
        la, lo = float(center["lat"]), float(center["lon"])
        kind = tags.get("healthcare") or tags.get("amenity") or ""
        address = ", ".join(
            tags[k] for k in ("addr:street", "addr:suburb", "addr:city") if tags.get(k)
        )
        km = distance_km(lat, lon, la, lo)
        item = {
            "nom": name,
            "type": KINDS.get(kind, "structure de santé"),
            "distance_km": round(km, 1),
            "distance": distance_text(km),
            "adresse": address,
            "telephone": tags.get("phone") or tags.get("contact:phone") or "",
            "lat": la,
            "lon": lo,
            "carte": maps_link(la, lo),
        }
        key = name.lower()
        if key not in found or item["distance_km"] < found[key]["distance_km"]:
            found[key] = item  # même structure en point et en bâtiment : on garde la plus proche
    return sorted(found.values(), key=lambda x: x["distance_km"])[:limit]


async def nearby_health_facilities(lat: float, lon: float, limit: int = 3) -> list[dict]:
    """Structures de santé les plus proches, en élargissant le rayon si besoin."""
    async with httpx.AsyncClient(timeout=40, headers=HEADERS) as client:
        for radius in RADII_M:
            try:
                r = await client.post(OVERPASS_URL, data={"data": _query(lat, lon, radius)})
                r.raise_for_status()
                facilities = parse_facilities(r.json(), lat, lon, limit)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Recherche OpenStreetMap impossible : %s", e)
                return []
            if facilities:
                return facilities
    return []
