
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


def discover(fetch=None):
    query = """[out:json][timeout:25];
    (nwr["name"]["tourism"~"^(museum|gallery|attraction|viewpoint)$"](54.54,39.55,54.75,39.92);
     nwr["name"]["amenity"~"^(theatre|cinema|cafe|restaurant)$"](54.54,39.55,54.75,39.92);
     nwr["name"]["leisure"~"^(park|garden)$"](54.54,39.55,54.75,39.92););
    out center;"""
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
        if tags.get("access") in {"private", "no"}:
            continue
        coords = element.get("center", element)
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is None or lon is None or not (54.54 <= lat <= 54.75 and 39.55 <= lon <= 39.92):
            continue
        kind, osm_id = element["type"], element["id"]
        yield dict(id=f"osm:{kind}:{osm_id}", name=tags["name"], lat=lat, lon=lon,
                   address=", ".join(filter(None, (tags.get("addr:street"), tags.get("addr:housenumber")))),
                   category_hint=tags.get("tourism") or tags.get("leisure") or tags.get("amenity"),
                   hours=tags.get("opening_hours"), website=tags.get("website") or tags.get("contact:website"),
                   wikidata=tags.get("wikidata"), commons=tags.get("wikimedia_commons"), tags=tags,
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


def audit(places):
    ids = set()
    verified = []
    for p in places:
        if p["id"] in ids:
            raise ValueError(f"Duplicate ID: {p['id']}")
        ids.add(p["id"])
        if not p.get("verified"):
            continue
        if (type(p.get("lat")) not in (float,int) or type(p.get("lon")) not in (float,int)
                or not (54.54 <= p["lat"] <= 54.75 and 39.55 <= p["lon"] <= 39.92)):
            raise ValueError(f"Coordinates outside Ryazan: {p['id']}")
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
    if not verified:
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
    args = parser.parse_args()
    if args.discover:
        if args.discover.resolve() == (ROOT / "catalog.json").resolve():
            parser.error("choose a candidate file outside catalog.json")
        args.discover.write_text(json.dumps(list(discover()), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        selected = audit(json.loads((ROOT / "catalog.json").read_text(encoding="utf-8")))
        if args.online:
            audit_online(selected)
        print(f"{len(selected)} verified places, {sum(len(p['gallery']) for p in selected)} attributed photos")

