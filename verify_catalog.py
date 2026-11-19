
"""Audit published places and optionally collect unapproved leads from Overpass."""
import argparse
from datetime import date
import json
from pathlib import Path
from urllib.parse import urlencode, unquote, urlsplit
from urllib.request import Request, urlopen
import re

ROOT = Path(__file__).resolve().parent
AGENT = "SwipeCityCatalog/1.0 (+https://github.com/Vol4ok-99/SwapSity)"
OVERPASS = "https://overpass-api.de/api/interpreter"


def request(url, data=None, method=None):
    return urlopen(Request(url, data=data, method=method, headers={"User-Agent": AGENT}), timeout=42)


CITY = {"name": "Рязань", "bounds": [54.54, 39.55, 54.75, 39.92], "language": "ru"}
PLACE_TYPES = {
    "tourism": {"culture": ("museum", "gallery"), "unusual": ("attraction", "viewpoint", "zoo", "aquarium", "artwork")},
    "historic": {"culture": ("castle", "manor", "memorial", "monument", "archaeological_site", "ruins", "fort", "building")},
    "amenity": {"culture": ("theatre", "arts_centre", "library", "community_centre"),
                "fun": ("cinema", "circus", "planetarium"),
                "food": ("cafe", "restaurant", "bar", "pub", "food_court", "ice_cream")},
    "leisure": {"nature": ("park", "garden", "nature_reserve"),
                "fun": ("water_park", "amusement_arcade", "escape_game"),
                "active": ("sports_centre", "fitness_centre", "swimming_pool", "ice_rink", "stadium", "bowling_alley", "trampoline_park")},
    "shop": {"food": ("bakery", "confectionery")},
    "natural": {"nature": ("beach",)},
}


def city_profile(config):
    if not isinstance(config,dict):
        raise ValueError("Настройки города должны быть объектом JSON")
    name, bounds = config.get("name"), config.get("bounds")
    language = config.get("language", "ru")
    if not isinstance(name, str) or not name.strip() or not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,12}", language):
        raise ValueError("Укажите название города и код языка Википедии")
    if (not isinstance(bounds, list) or len(bounds) != 4 or
            any(type(n) not in (int, float) or not -180 <= n <= 180 for n in bounds)):
        raise ValueError("Границы должны быть списком [юг, запад, север, восток]")
    south, west, north, east = bounds
    if not (-90 <= south < north <= 90 and west < east):
        raise ValueError("Некорректные границы города")
    return {"name": name.strip(), "bounds": bounds, "language": language}


def in_city(point, city=CITY):
    south, west, north, east = city["bounds"]
    lat, lon = point.get("lat"), point.get("lon")
    return type(lat) in (int, float) and type(lon) in (int, float) and south <= lat <= north and west <= lon <= east


def discover(fetch=None, city=CITY):
    city = city_profile(city)
    bbox = ",".join(map(str, city["bounds"]))
    filters = []
    for key, groups in PLACE_TYPES.items():
        values = "|".join(value for group in groups.values() for value in group)
        filters.append(f'nwr["name"]["{key}"~"^({values})$"];')
    query = f'[out:json][timeout:30][bbox:{bbox}];({"".join(filters)});out center qt;'
    payload = urlencode({"data": query}).encode()
    if fetch:
        result = json.loads(fetch(OVERPASS, payload))
    else:
        with request(OVERPASS, payload) as response:
            result = json.load(response)
    if result.get("remark"):
        raise ValueError(result["remark"])
    for element in result["elements"]:
        tags = element.get("tags", {})
        name = tags.get("name:" + city["language"]) or tags.get("name")
        if not name or tags.get("access") in {"private", "no"}:
            continue
        coords = element.get("center", element)
        if not in_city(coords, city):
            continue
        category, hint = next(((group, tags[key]) for key, groups in PLACE_TYPES.items()
                               for group, values in groups.items() if tags.get(key) in values), (None, None))
        if not category:
            continue
        kind, osm_id = element["type"], element["id"]
        street = tags.get("addr:street") or tags.get("addr:place")
        number = tags.get("addr:housenumber")
        yield dict(id=f"osm:{kind}:{osm_id}", name=name, lat=coords["lat"], lon=coords["lon"],
                   address=tags.get("addr:full") or (f"{street}, {number}" if street and number else None),
                   category=category, category_hint=hint, hours=tags.get("opening_hours"),
                   website=tags.get("website") or tags.get("contact:website"), wikidata=tags.get("wikidata"),
                   commons=tags.get("wikimedia_commons"), tags=tags, city=city,
                   source=f"https://www.openstreetmap.org/{kind}/{osm_id}", collected_on=date.today().isoformat(),
                   verified=False)


def https(url):
    return bool(url and urlsplit(url).scheme == "https" and urlsplit(url).hostname)


def photo_allowed(photo):
    if photo.get("license") == "permission":
        return bool(photo.get("author") and photo.get("attribution") and https(photo.get("source"))
                    and https(photo.get("url")) and https(photo.get("permission_url")) and https(photo.get("license_url"))
                    and photo.get("checked_on") and photo.get("method")=="manual_permission")
    license_ok = re.fullmatch(r"https://creativecommons\.org/(?:licenses/(?:by|by-sa)/[1-4]\.0(?:/[a-z-]+)?|publicdomain/zero/1\.0)/?", photo.get("license") or "")
    return bool(photo.get("author") and https(photo.get("url")) and https(photo.get("source"))
                and urlsplit(photo["source"]).hostname == "commons.wikimedia.org"
                and urlsplit(photo["url"]).hostname in {"upload.wikimedia.org", "thumb.wikimedia.org"}
                and license_ok)


def audit(places, city=CITY, allow_empty=False):
    ids = set()
    verified = []
    for p in places:
        if p["id"] in ids:
            raise ValueError(f"Duplicate ID: {p['id']}")
        ids.add(p["id"])
        if not p.get("verified"):
            continue
        if not in_city(p, city):
            raise ValueError(f"Coordinates outside {city['name']}: {p['id']}")
        if not all(p.get(key) for key in ("name", "category", "address", "verified_on")):
            raise ValueError(f"Missing verification fields: {p['id']}")
        date.fromisoformat(p["verified_on"])
        if p["category"] not in {"culture","nature","food","fun","active","unusual"}:
            raise ValueError(f"Invalid category: {p['id']}")
        price=p.get("price")
        if price is not None and (type(price) not in (int,float) or not 0 <= price < float("inf")):
            raise ValueError(f"Invalid price: {p['id']}")
        if (not https(p.get("source")) or not https(p.get("coordinate_source"))
                or p.get("website") and not https(p["website"])):
            raise ValueError(f"Missing source: {p['id']}")
        gallery = p.get("gallery", [])
        if p.get("photo") != (gallery[0] if gallery else None):
            raise ValueError(f"Cover differs from approved gallery: {p['id']}")
        if len(gallery) > 3 or len({x["source"] for x in gallery}) != len(gallery):
            raise ValueError(f"Missing or repeated photos: {p['id']}")
        for photo in gallery:
            if not photo_allowed(photo):
                raise ValueError(f"Missing image attribution or supported license: {p['id']}")
        verified.append(p)
    if not verified and not allow_empty:
        raise ValueError("No verified places")
    return verified


def audit_online(verified):
    titles = [unquote(urlsplit(x["source"]).path.removeprefix("/wiki/")).replace("_", " ")
              for p in verified for x in p["gallery"] if x["license"]!="permission"]
    current_licenses = {}
    for offset in range(0, len(titles), 20):
        api = "https://commons.wikimedia.org/w/api.php?" + urlencode(
            dict(action="query", prop="imageinfo", iiprop="extmetadata",
                 titles="|".join(titles[offset:offset + 20]), format="json"))
        with request(api) as response:
            images = json.load(response)["query"]["pages"]
        for page in images.values():
            if not page.get("imageinfo"):
                raise ValueError(f"Missing Commons file: {page['title']}")
            metadata = page["imageinfo"][0]["extmetadata"]
            current_licenses[page["title"]] = metadata.get("LicenseUrl", {}).get("value", "")
    for place in verified:
        with request(place["source"]) as response:
            if response.status != 200:
                raise ValueError(f"Unavailable source: {place['id']}")
        for photo in place["gallery"]:
            if photo["license"] == "permission":
                with request(photo["permission_url"]) as response:
                    if response.status != 200:
                        raise ValueError(f"Unavailable rights document: {photo['source']}")
            else:
                title = unquote(urlsplit(photo["source"]).path.removeprefix("/wiki/")).replace("_", " ")
                actual = current_licenses.get(title, "").replace("http://", "https://").rstrip("/")
                if actual != photo["license"].rstrip("/"):
                    raise ValueError(f"License changed for {title}")
            with request(photo["url"], method="HEAD") as response:
                if response.status != 200 or not response.headers.get_content_type().startswith("image/"):
                    raise ValueError(f"Unavailable photo: {photo['source']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="check source and Commons URLs")
    parser.add_argument("--discover", type=Path, help="write new unapproved OSM leads to a separate file")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog.json")
    parser.add_argument("--city-config", type=Path)
    args = parser.parse_args()
    city = city_profile(json.loads(args.city_config.read_text(encoding="utf-8-sig"))) if args.city_config else CITY
    if args.discover:
        if args.discover.resolve() == (ROOT / "catalog.json").resolve():
            parser.error("choose a candidate file outside catalog.json")
        args.discover.write_text(json.dumps(list(discover(city=city)), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        selected = audit(json.loads(args.catalog.read_text(encoding="utf-8-sig")), city)
        if args.online:
            audit_online(selected)
        print(f"{len(selected)} verified places, {sum(len(p['gallery']) for p in selected)} attributed photos")

