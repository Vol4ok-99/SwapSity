"""Build a public city catalog from a current OSM snapshot and sourced media."""
from collections import Counter
from datetime import date
import json
import re

from catalog_pipeline import (
    CITY, CATEGORIES, Http, RetryLater, atomic, clean, distance, duplicate,
    load_json, map_directory, media, norm, normalize, website, wikidata_fields,
)
from verify_catalog import audit, in_city, photo_allowed

CLOSED_PREFIXES = ("disused:", "abandoned:", "demolished:", "removed:", "was:")
CONFLICTS = ("Адрес на сайте отличается", "Город филиала на сайте отличается",
             "Координаты на сайте и в OSM не совпадают")


def check(raw, place, row, city):
    tags = raw.get("tags", {})
    if row and (row["state"] == "rejected" or
                row["state"] not in {"published", "ready"} and row["decision"]):
        return "решение предыдущей проверки"
    if raw.get("closure") or tags.get("access") in {"private", "no", "customers_no"}:
        return "закрыто или недоступно"
    if (any(key.startswith(CLOSED_PREFIXES) for key in tags)
            or any(tags.get(key) == "yes" for key in ("disused", "abandoned", "demolished", "construction"))
            or tags.get("opening_hours") in {"closed", "off"}):
        return "закрыто или недоступно"
    if not place["name"] or place["category"] not in CATEGORIES.values() or not in_city(place, city):
        return "нет названия, категории или координат"
    if not re.fullmatch(r"osm:(node|way|relation):[0-9]+", place["id"]):
        return "нет идентификатора OSM"
    kind, number = place["id"].split(":")[1:]
    if place["source"] != f"https://www.openstreetmap.org/{kind}/{number}":
        return "источник не соответствует ID"
    previous = json.loads(row["data"]) if row and row["data"] else {}
    if any(item.startswith(CONFLICTS) for item in previous.get("findings", [])):
        return "конфликт источников"
    return None


def osm_facts(raw, place, city):
    tags = raw.get("tags", {})
    name = city["name"]
    language = city["language"]
    address = place["address"]
    site = website(raw.get("website"))
    place.update(
        address=address or f"{name}, точка на карте",
        route_to_coordinates=not address or place["areal"],
        hours=clean(raw.get("hours") or tags.get("opening_hours")) or None,
        description=clean(tags.get("description:"+language) or tags.get("description")) or None,
        website=site if site and not map_directory(site) else None,
        source_type="osm_import",
        verified=True,
        verified_on=raw.get("collected_on") or date.today().isoformat(),
        osm_license="https://opendatacommons.org/licenses/odbl/1-0/",
    )


def direct_media(db, row, raw, place, http, unique_q):
    tags = raw.get("tags", {})
    q = raw.get("wikidata")
    direct = {**raw, "tags":{key:tags[key] for key in ("image", "wikimedia_commons") if key in tags}}
    if not unique_q:
        direct.pop("wikidata", None)
    error = None
    try:
        if unique_q:
            wikidata_fields(db,row,place,http)
            point=place.get("wikidata_point") or {}
            other={"lat":point.get("latitude"),"lon":point.get("longitude")}
            if (not in_city(other,place["city"]) or distance(place,other)>120
                    or place["findings"]):
                place.pop("file",None)
                direct.pop("wikidata",None)
        media(db,{**row,"raw":json.dumps(direct)},place,http,methods={"linked_file","depicts"})
    except (RetryLater, ValueError, KeyError, OSError) as exc:
        error = str(exc)
    photos = [photo for photo in place["gallery"] if photo_allowed(photo)
              and photo["method"] in {"linked_file","depicts"}]
    return [{**photo,"confirmed":True} for photo in photos[:3]],error


def automatic_publish(db, path, rows, city=CITY, online=True):
    if not rows:
        raise ValueError("Снимок мест пуст. Каталог сохранён без изменений.")
    base = load_json(path) if path.exists() else []
    verified = audit(base,city,allow_empty=True)
    ready = {row["id"]: row for row in db.execute("SELECT * FROM places")}
    results = {place["id"]:place for place in base}
    accepted = {place["id"]:place for place in verified if place.get("source_type")!="osm_import"}
    q_counts = Counter(row.get("wikidata") for row in rows if row.get("wikidata"))
    report = {"candidates":len(rows),"added":0,"updated":0,"skipped":{},"photo_errors":0}
    http = Http(db)
    ordered = sorted(rows,key=lambda raw: (not bool(raw.get("address")), not bool(raw.get("wikidata")), raw["id"]))
    for raw in ordered:
        pid=raw["id"]
        row=ready.get(pid)
        place={}
        with db:
            normalize(db,{"id":pid,"raw":json.dumps({**raw,"city":city})},place,http)
        reason=check(raw,place,row,city)
        original=accepted.get(pid)
        old=results.get(pid)
        if not original and not reason:
            for other_id, other in accepted.items():
                if other_id==pid:
                    continue
                other={**other,"city":city}
                if duplicate(place,other) or (norm(place["name"])==norm(other["name"])
                                              and distance(place,other)<40):
                    reason="дубль ближайшего объекта"
                    break
        if reason:
            report["skipped"][pid]=reason
            continue
        if not original:
            osm_facts(raw,place,city)
        previous_media = original
        if not previous_media and old and old.get("verified") and norm(old["name"])==norm(place["name"]):
            if distance(place,old)<=50:
                previous_media=old
        gallery=list((previous_media or {}).get("gallery",[]))
        if online and len(gallery)<3 and (raw.get("commons") or raw.get("wikidata")
                                        or raw.get("tags",{}).get("image")
                                        or raw.get("tags",{}).get("wikimedia_commons")):
            seed={"id":pid,"raw":json.dumps(raw)}
            with db:
                photos,error=direct_media(db,seed,raw,place,http,q_counts[raw.get("wikidata")]==1)
            report["photo_errors"]+=bool(error)
            seen={photo["source"] for photo in gallery}
            gallery=(gallery+[photo for photo in photos if photo["source"] not in seen])[:3]
        public = original or {key:place.get(key) for key in (
            "id","name","lat","lon","category","address","route_to_coordinates","description",
            "hours","website","phone","price","source","coordinate_source","source_type",
            "verified","verified_on","osm_license")}
        public={**public,"gallery":gallery,"photo":gallery[0] if gallery else None}
        accepted[pid]=public
        results[pid]=public
        report["added"]+=not bool(old and old.get("verified"))
        report["updated"]+=bool(old and old.get("verified") and old!=public)
    for place in verified:
        pid=place["id"]
        if place.get("source_type")=="osm_import" and pid not in accepted:
            results[pid]={**place,"verified":False}
    final=list(results.values())
    selected=audit(final,city)
    report["visible"]=len(selected)
    report["with_photo"]=sum(bool(place["gallery"]) for place in selected)
    if path.exists() and final!=base:
        atomic(path.with_name(path.stem+".previous.json"),base)
    atomic(path,final)
    atomic(path.with_name(path.stem+".auto-report.json"),report)
    return report
