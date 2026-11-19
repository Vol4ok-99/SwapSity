
"""Resumable SwipeCity catalog workflow; all network enrichment is optional."""
import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from verify_catalog import AGENT, CITY, ROOT, PLACE_TYPES, audit, city_profile, discover, in_city, photo_allowed

STAGES = ("normalize", "duplicates", "enrich", "facts", "photos", "queue")
CATEGORIES = {feature: category for groups in PLACE_TYPES.values()
              for category, features in groups.items() for feature in features}
AREAS = {"park", "garden", "viewpoint", "nature_reserve", "beach"}
LANDMARKS = {"monument", "memorial", "artwork", "ruins", "archaeological_site"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
 id TEXT PRIMARY KEY, raw TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT,
 state TEXT NOT NULL, stage INTEGER NOT NULL DEFAULT 0, retry_at INTEGER DEFAULT 0,
 initial TEXT, decision TEXT, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
 place TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL,
 origin TEXT NOT NULL, method TEXT NOT NULL, checked TEXT NOT NULL,
 UNIQUE(place,field,source,method)
);
CREATE TABLE IF NOT EXISTS events (place TEXT, state TEXT, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY, canonical TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, body BLOB NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS photo_scans (place TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, retry_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ai_suggestions (key TEXT PRIMARY KEY, cost INTEGER, result TEXT, at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
    return db


def transition(db, pid, state, reason):
    db.execute("UPDATE places SET state=?,updated=? WHERE id=?", (state, now(), pid))
    db.execute("INSERT INTO events VALUES(?,?,?,?)", (pid, state, reason, now()))


def evidence(db, pid, field, value, source, origin, method):
    if value in (None, "") or not source:
        return
    db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?)",
               (pid, field, dump(value), source, origin, method, now()))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def import_rows(db, rows, existing=False, city=CITY):
    changed = 0
    with db:
        for raw in rows:
            pid = raw.get("id")
            if raw.get("city") and city_profile(raw["city"]) != city:
                raise ValueError(f"{pid}: запись из другого города")
            if city != CITY:
                raw = {**raw, "city": city}
            if not isinstance(pid,str) or not re.fullmatch(r"[\w:-]{1,120}", pid):
                raise ValueError("Invalid place ID")
            fingerprint = hashlib.sha256(dump({k:v for k,v in raw.items() if k!="collected_on"}).encode()).hexdigest()
            old = db.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
            if existing and old:
                continue
            if old and old["fingerprint"]==fingerprint:
                continue
            if old and (old["state"] == "published" or old["state"] == "ready" and old["decision"]):
                db.execute("UPDATE places SET raw=?,fingerprint=? WHERE id=?",(dump(raw),fingerprint,pid))
                changed+=1
                continue
            state = "published" if existing and raw.get("verified") else "discovered"
            db.execute("""INSERT INTO places(id,raw,fingerprint,state,initial,updated) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,fingerprint=excluded.fingerprint,
                          state='discovered',stage=0,retry_at=0,data=NULL,decision=NULL,updated=excluded.updated""",
                       (pid,dump(raw),fingerprint,state,dump(raw) if existing else None,now()))
            if old:
                db.execute("DELETE FROM facts WHERE place=? AND method!='manual'",(pid,))
            transition(db,pid,state,"Existing public record" if existing else "New or changed source record")
            changed += 1
    return changed


def clean(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def norm(value):
    return " ".join(re.findall(r"\w+",clean(value).casefold().replace("ё","е")))


def address_key(value,city=CITY):
    value=norm(value)
    city_name=norm(city["name"])
    for prefix in ("город "+city_name+" ","г "+city_name+" ",city_name+" "):
        if value.startswith(prefix):
            value=value.removeprefix(prefix)
            break
    aliases={"проспект":"пр","площадь":"пл","переулок":"пер"}
    return " ".join(aliases.get(word,word) for word in value.split() if word not in {"д","дом","ул","улица"})


def phone(value):
    digits = re.sub(r"\D","",str(value or ""))
    return "+7"+digits[1:] if len(digits)==11 and digits[0] in "78" else ("+"+digits if 10<=len(digits)<=15 else None)


def website(value):
    try:
        u=urlsplit(clean(value))
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            return None
        if u.port not in (None,80,443):
            return None
        return urlunsplit((u.scheme,u.netloc.lower(),u.path or "/",u.query,""))
    except ValueError:
        return None


def valid_point(p):
    return in_city(p, p.get("city") or CITY)


def distance(a,b):
    latitude = math.radians((a["lat"] + b["lat"]) / 2)
    return math.hypot((a["lat"]-b["lat"])*111200, (a["lon"]-b["lon"])*111200*math.cos(latitude))


def duplicate(a,b):
    if (a.get("city") or CITY) != (b.get("city") or CITY) or not valid_point(a) or not valid_point(b):
        return False
    city=a.get("city") or CITY
    address=address_key(a.get("address"),city)
    same_address=address and address==address_key(b.get("address"),city)
    close=distance(a,b)<90
    same_q=a.get("wikidata") and a.get("wikidata")==b.get("wikidata")
    same_phone=a.get("phone") and a.get("phone")==b.get("phone")
    similar=SequenceMatcher(None,norm(a["name"]),norm(b["name"])).ratio()>=.85
    return bool((same_q and close) or (similar and close and (same_address or same_phone or distance(a,b)<22)))


class RetryLater(Exception):
    def __init__(self, message, seconds=900):
        super().__init__(message)
        self.seconds=seconds


def map_directory(url):
    parts=urlsplit(url)
    host=parts.hostname or ""
    return (host in {"2gis.ru","2gis.com","yandex.ru","yandex.com","yandex.com.tr"}
            or host.endswith((".2gis.ru",".2gis.com",".yandex.ru",".yandex.com",".yandex.com.tr")))


def public_url(url):
    if not website(url):
        raise ValueError("Invalid source URL")
    try:
        answers=socket.getaddrinfo(urlsplit(url).hostname,None,type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RetryLater("DNS unavailable") from exc
    if not all(ipaddress.ip_address(item[4][0]).is_global for item in answers):
        raise ValueError("Source address is not public")


class PublicRedirect(HTTPRedirectHandler):
    max_redirections=3
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if map_directory(newurl):
            raise ValueError("Картографический справочник доступен для ручной сверки")
        public_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Http:
    def __init__(self, db, enabled=True, opener=None):
        self.db,self.enabled=db,enabled
        self.opener=opener or build_opener(PublicRedirect()).open
        self.last=0
        self.retry_until={}

    def get(self,url,data=None,method=None):
        key=hashlib.sha256((method or "").encode()+b"\0"+url.encode()+b"\0"+(data or b"")).hexdigest()
        cached=self.db.execute("SELECT body FROM cache WHERE key=? AND expires>?",(key,int(time.time()))).fetchone()
        if cached:
            return cached["body"]
        if not self.enabled:
            raise RetryLater("Network disabled")
        host=urlsplit(url).hostname
        remaining=math.ceil(self.retry_until.get(host,0)-time.monotonic())
        if remaining>0:
            raise RetryLater("Source temporarily paused",max(900,remaining))
        public_url(url)
        for attempt in range(3):
            time.sleep(max(0, .65-(time.monotonic()-self.last)))
            self.last=time.monotonic()
            try:
                with self.opener(Request(url,data=data,method=method,headers={"User-Agent":AGENT}),timeout=42 if urlsplit(url).hostname=="overpass-api.de" else 12) as response:
                    body=response.headers.get_content_type().encode() if method=="HEAD" else response.read(12_000_001)
                if len(body)>12_000_000:
                    raise ValueError("Source response too large")
                self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",(key,body,int(time.time())+86400))
                return body
            except HTTPError as exc:
                if exc.code not in (429,500,502,503,504):
                    raise RetryLater(f"Source HTTP {exc.code}") from exc
                retry=exc.headers.get("Retry-After","")
                try:
                    delay=float(retry) if retry.isdigit() else (parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
                except (TypeError,ValueError):
                    delay=0
                if delay>12:
                    self.retry_until[host]=time.monotonic()+delay
                    raise RetryLater(f"Rate limit: retry after {int(delay)}s", max(900, math.ceil(delay))) from exc
                if attempt<2:
                    time.sleep(max(2**attempt,delay))
            except (OSError,URLError):
                if attempt<2:
                    time.sleep(2**attempt)
        self.retry_until[host]=time.monotonic()+60
        raise RetryLater("Source temporarily unavailable")

    def json(self,url,**params):
        response=json.loads(self.get(url+"?"+urlencode(params)))
        if response.get("error"):
            raise RetryLater(f"API: {response['error'].get('code','error')}")
        return response


class StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active=False
        self.parts=[]
        self.results=[]

    def handle_starttag(self,tag,attrs):
        if tag=="script" and dict(attrs).get("type")=="application/ld+json":
            self.active=True
            self.parts=[]

    def handle_data(self,text):
        if self.active:
            self.parts.append(text)

    def handle_endtag(self,tag):
        if tag=="script" and self.active:
            self.active=False
            try:
                self.results.append(json.loads("".join(self.parts)))
            except ValueError:
                pass


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]

    def handle_data(self,text):
        self.parts.append(text)


def plain(value):
    parser=TextHTML()
    parser.feed(value or "")
    return clean(" ".join(parser.parts))


def entities(value):
    if isinstance(value,list):
        for item in value:
            yield from entities(item)
    elif isinstance(value,dict):
        yield value
        if "@graph" in value:
            yield from entities(value["@graph"])


def official_candidates(html):
    parser=StructuredHTML()
    parser.feed(html.decode("utf-8",errors="replace"))
    for item in entities(parser.results):
        addr=item.get("address")
        city=addr.get("addressLocality") if isinstance(addr,dict) else None
        street=addr.get("streetAddress") if isinstance(addr,dict) else addr
        if isinstance(street,str) and isinstance(item.get("name"),str):
            yield item["name"],street,city,item.get("geo")


def normalize(db,row,p,http):
    raw=json.loads(row["raw"])
    tags=raw.get("tags",{})
    hint=raw.get("category_hint")
    point={k:raw.get(k) for k in ("lat","lon")}
    p.update(id=row["id"],city=raw.get("city") or CITY,name=clean(raw.get("name")),category=raw.get("category") or CATEGORIES.get(hint),
             address=clean(raw.get("address")) or None,**point,hours=None,price=None,description=None,
             website=website(raw.get("website")),phone=phone(raw.get("phone") or tags.get("phone")),
             wikidata=raw.get("wikidata"),coordinate_source=raw.get("coordinate_source") or raw.get("source"),
             source=raw.get("source"),areal=hint in AREAS,landmark=hint in LANDMARKS and tags.get("indoor")!="yes",
             findings=[],gallery=[],signals={},pending=[])
    source=p["coordinate_source"]
    for field in ("name","category","address","lat","lon","website","phone"):
        evidence(db,row["id"],field,p.get(field),source,"osm","import")
    evidence(db,row["id"],"hours",raw.get("hours"),source,"osm","unconfirmed")
    if raw.get("closure") or tags.get("disused")=="yes" or any(k.startswith(("disused:","abandoned:","was:","demolished:")) for k in tags):
        p["findings"].append("В источнике есть признак закрытия, переезда или смены назначения")


def find_duplicates(db,row,p,http):
    for item in db.execute("SELECT id,data,initial FROM places WHERE id<>? AND (data IS NOT NULL OR initial IS NOT NULL)",(row["id"],)):
        other=json.loads(item["data"] or item["initial"])
        if other.get("name") and duplicate(p,other):
            p["findings"].append("Возможный дубль: "+item["id"])


def wikidata_fields(db,row,p,http):
    language=(p.get("city") or CITY)["language"]
    q=p.get("wikidata") or json.loads(row["raw"]).get("wikidata")
    if isinstance(q,str) and re.fullmatch(r"Q[1-9][0-9]*",q):
        url="https://www.wikidata.org/wiki/"+q
        entity=http.json("https://www.wikidata.org/w/api.php",action="wbgetentities",ids=q,
                         props="claims|labels|sitelinks",languages=language,sitefilter=language+"wiki",format="json")["entities"][q]
        claims=entity.get("claims",{})
        for prop,field in (("P18","file"),("P373","commons_category"),("P856","wikidata_website"),("P625","wikidata_point")):
            for claim in claims.get(prop,[]):
                if claim.get("rank")=="deprecated":
                    continue
                value=claim.get("mainsnak",{}).get("datavalue",{}).get("value")
                if value:
                    p[field]=value
                    evidence(db,row["id"],field,value,url,"wikidata","claim (independence unknown)")
                    break
        wd_label=entity.get("labels",{}).get(language,{}).get("value")
        p["wiki_title"]=entity.get("sitelinks",{}).get(language+"wiki",{}).get("title")
        if wd_label and SequenceMatcher(None,norm(wd_label),norm(p["name"])).ratio()<.55:
            p["findings"].append("Название в Wikidata отличается: "+wd_label)


def enrich(db,row,p,http):
    city=p.get("city") or CITY
    wikidata_fields(db,row,p,http)
    url=p.get("website") or website(p.get("wikidata_website"))
    if not url or map_directory(url):
        return
    p["website"]=url
    candidates=list(official_candidates(http.get(url)))
    matching=[(name,addr,city,geo) for name,addr,city,geo in candidates
              if SequenceMatcher(None,norm(name),norm(p["name"])).ratio()>=.78]
    host=urlsplit(url).hostname
    independent=not any(host==d or host.endswith("."+d) for d in (
        "openstreetmap.org","wikidata.org","wikipedia.org","wikimedia.org","wikivoyage.org"))
    city_name=norm(city["name"])
    addresses={}
    conflicts=[]
    for name,addr,locality,geo in matching:
        evidence(db,row["id"],"address",addr,url,host,"JSON-LD")
        evidence(db,row["id"],"name",name,url,host,"JSON-LD")
        local=norm(locality) in {city_name,"г "+city_name,"город "+city_name}
        nearby=False
        if isinstance(geo,dict):
            try:
                point={"lat":float(geo["latitude"]),"lon":float(geo["longitude"])}
            except (KeyError,ValueError,TypeError):
                pass
            else:
                evidence(db,row["id"],"coordinates",point,url,host,"JSON-LD")
                nearby=valid_point(p) and in_city(point,city) and distance(p,point)<=100
                if valid_point(p) and distance(p,point)>150:
                    conflicts.append("Координаты на сайте и в OSM не совпадают")
                    continue
        if locality and not local:
            conflicts.append("Город филиала на сайте отличается: "+locality)
            continue
        if p.get("address") and address_key(p["address"],city)!=address_key(addr,city):
            conflicts.append("Адрес на сайте отличается: "+addr)
            continue
        if independent and norm(name)==norm(p["name"]) and re.search(r"\d",addr) and (local or nearby):
            if p.get("address") or nearby:
                addresses[address_key(addr,city)]=addr
    if len(addresses)==1:
        p["address"]=p.get("address") or next(iter(addresses.values()))
        p["signals"]["official_address"]=url
    elif conflicts or len(addresses)>1:
        p["findings"].extend(dict.fromkeys(conflicts or ["Сайт указал несколько возможных адресов"]))
    else:
        p["findings"].append("Сайт не подтвердил название и адрес конкретного филиала в структурированных данных")


def facts(db,row,p,http):
    if not valid_point(p):
        p["findings"].append(f"Координаты отсутствуют или вне границ {(p.get('city') or CITY)['name']}")
    wd=p.get("wikidata_point") or {}
    other={"lat":wd.get("latitude"),"lon":wd.get("longitude")}
    if valid_point(p) and all(type(v) in (int,float) for v in other.values()) and distance(p,other)>200:
        p["findings"].append("Координаты OSM и Wikidata расходятся более чем на 200 м")
    if not p.get("name") or p.get("category") not in CATEGORIES.values():
        p["findings"].append("Не определено название или категория")
    p["signals"]["address"]=("area" if p.get("areal") else "present" if p.get("address") else
                              "coordinates" if p.get("landmark") else "missing")
    p["signals"].update(hours="unknown",price="unknown",identity="unconfirmed")
    if p["signals"]["address"]=="missing":
        p["findings"].append("Нет адреса для точечного объекта")
    if not p["signals"].get("official_address"):
        p["findings"].append("Нужно независимое подтверждение идентичности и адреса")


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def commons_title(value):
    if not isinstance(value,str):
        return ""
    parts=urlsplit(value)
    if parts.scheme:
        if parts.hostname!="commons.wikimedia.org" or not parts.path.startswith("/wiki/"):
            return ""
        value=unquote(parts.path.removeprefix("/wiki/"))
    return value.replace("_"," ").strip()


def photo_queries(raw,p):
    tags=raw.get("tags",{})
    refs=[commons_title(raw.get("commons")),commons_title(tags.get("wikimedia_commons")),
          commons_title(tags.get("image"))]
    files=list(dict.fromkeys(ref for ref in refs if ref.startswith("File:")))
    if p.get("file"):
        files.append("File:"+p["file"])
    if files:
        yield "linked_file", {"titles":"|".join(dict.fromkeys(files))}
    categories=list(dict.fromkeys(ref for ref in refs if ref.startswith("Category:")))
    if p.get("commons_category"):
        categories.append("Category:"+p["commons_category"])
    for category in dict.fromkeys(categories):
        yield "linked_category", dict(generator="categorymembers",gcmtitle=category,gcmtype="file",gcmlimit=12)
    city=p.get("city") or CITY
    language=city["language"]
    wiki=tags.get("wikipedia") or ""
    wiki_language, sep, wiki_title=wiki.partition(":")
    title=p.get("wiki_title") or (wiki_title if sep and wiki_language==language else None)
    if title:
        yield "linked_article", {"article":title,"language":language}
    q=raw.get("wikidata")
    if isinstance(q,str) and re.fullmatch(r"Q[1-9][0-9]*",q):
        yield "depicts", dict(generator="search",gsrnamespace=6,gsrlimit=8,gsrsearch=f"haswbstatement:P180={q} filetype:bitmap")
    name=re.sub(r'[^\w -]+',' ',p["name"]).strip()
    city_name=re.sub(r'[^\w -]+',' ',city["name"]).strip()
    if len(name)>=3 and city_name:
        yield "name_search", dict(generator="search",gsrnamespace=6,gsrlimit=8,
                                 gsrsearch=f'"{name}" "{city_name}" filetype:bitmap')


def media(db,row,p,http,methods=None):
    raw=json.loads(row["raw"])
    proposals={photo["source"]:photo for photo in p["gallery"] if photo.get("confirmed")}
    params=dict(action="query",prop="imageinfo",iiprop="url|mime|extmetadata",iiurlwidth=960,
                iiextmetadatafilter="Artist|Attribution|LicenseUrl|LicenseShortName|Restrictions",format="json",redirects=1)
    article_error=None
    try:
        for method, query in photo_queries(raw,p):
            if methods is not None and method not in methods:
                continue
            if method=="linked_article":
                try:
                    article=http.json(f"https://{query['language']}.wikipedia.org/w/api.php",action="query",
                                      prop="pageimages",titles=query["article"],piprop="name",pilicense="free",
                                      redirects=1,format="json")
                except RetryLater as exc:
                    article_error=exc
                    continue
                files=[page["pageimage"] for page in article.get("query",{}).get("pages",{}).values() if page.get("pageimage")]
                if not files:
                    continue
                query={"titles":"|".join(f"File:{name}" for name in files)}
            result=http.json(COMMONS_API,**params,**query)
            for page in result.get("query",{}).get("pages",{}).values():
                if len(proposals)>=6:
                    break
                info=(page.get("imageinfo") or [{}])[0]
                if info.get("descriptionurl") in proposals:
                    continue
                meta=info.get("extmetadata",{})
                def get(key):
                    return plain(meta.get(key,{}).get("value",""))
                photo=dict(source=info.get("descriptionurl"),url=info.get("thumburl"),author=get("Artist"),
                           license=get("LicenseUrl").replace("http://","https://"),license_label=get("LicenseShortName"),
                           attribution=get("Attribution"),method=method,checked_on=now(),confirmed=False)
                if info.get("mime") in {"image/jpeg","image/png","image/webp"} and photo_allowed(photo) and not get("Restrictions"):
                    proposals[photo["source"]]=photo
                    evidence(db,row["id"],"photo",photo,photo["source"],"commons",method)
                else:
                    evidence(db,row["id"],"rejected_photo",page.get("title"),"https://commons.wikimedia.org/","commons",method)
            if len(proposals)>=3:
                break
        if article_error and len(proposals)<3:
            raise article_error
    finally:
        p["gallery"]=list(proposals.values())
        p["signals"]["photos"]=("review" if any(not x.get("confirmed") for x in proposals.values()) else
                                 "approved" if proposals else "missing")


def photo_stage(db,row,p,http,refresh_wikidata=False):
    retry_at=0
    try:
        source_error=None
        if refresh_wikidata:
            try:
                # Copy only media leads into approved cards; leave their findings untouched.
                lookup={**p,"findings":[]}
                wikidata_fields(db,row,lookup,http)
                p.update({key:lookup[key] for key in ("file","commons_category","wiki_title") if key in lookup})
            except (RetryLater,ValueError,KeyError,OSError) as exc:
                source_error=exc
        media(db,row,p,http)
        if source_error and not p["gallery"]:
            raise source_error
        p["signals"].pop("photo_error",None)
    except (RetryLater,ValueError,OSError) as exc:
        retry_at=int(time.time())+getattr(exc,"seconds",900)
        p["signals"]["photo_error"]=str(exc)
    db.execute("INSERT OR REPLACE INTO photo_scans VALUES(?,?,?)",(row["id"],row["fingerprint"],retry_at))
    return retry_at


def scan_photos(db,limit):
    http=Http(db)
    totals={"processed":0,"with_suggestions":0,"retry":0}
    with db:
        for row in db.execute("SELECT * FROM places WHERE state='published' AND data IS NULL"):
            public=json.loads(row["initial"])
            if public.get("gallery"):
                continue
            data={**public,"city":json.loads(row["raw"]).get("city") or CITY,
                  "areal":bool(public.get("route_to_coordinates")),"findings":[],"gallery":[],
                  "signals":{"identity":"matched","photos":"missing"}}
            db.execute("UPDATE places SET data=? WHERE id=?",(dump(data),row["id"]))
    rows=db.execute("""SELECT p.* FROM places p LEFT JOIN photo_scans s ON s.place=p.id
        WHERE p.data IS NOT NULL AND p.state IN ('needs_review','ready','published','checking')
          AND COALESCE(json_extract(p.data,'$.signals.photos'),'') != 'approved'
          AND (s.place IS NULL OR s.fingerprint != p.fingerprint OR (s.retry_at > 0 AND s.retry_at <= ?))
        ORDER BY CASE WHEN p.state IN ('ready','published') THEN 0 ELSE 1 END,
                 CASE WHEN json_extract(p.raw,'$.commons') IS NOT NULL OR json_extract(p.raw,'$.wikidata') IS NOT NULL
                     OR json_extract(p.raw,'$.tags.wikipedia') IS NOT NULL THEN 0 ELSE 1 END,p.id LIMIT ?""",
        (int(time.time()),limit)).fetchall()
    for row in rows:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current=db.execute("SELECT * FROM places WHERE id=?",(row["id"],)).fetchone()
            if current!=row:
                continue
            p=json.loads(row["data"])
            totals["retry"]+=bool(photo_stage(db,row,p,http,refresh_wikidata=True))
            db.execute("UPDATE places SET data=?,updated=? WHERE id=?",(dump(p),now(),row["id"]))
            totals["processed"]+=1
            totals["with_suggestions"]+=p["signals"]["photos"]=="review"
    return totals


def queue(db,row,p,http):
    accepted=p["signals"].get("official_address") and valid_point(p) and not p["findings"]
    if accepted:
        p["source"]=p["signals"]["official_address"]
        p["signals"]["identity"]="matched"
        return "ready","OSM geometry and exact name/address on linked official site"
    return "needs_review", "; ".join(p["findings"] or ["Проверить источники и идентичность"])


PROCESSORS=(normalize,find_duplicates,enrich,facts,photo_stage,queue)


def process(db, limit=20, enabled=True, stage=None, ids=()):
    http=Http(db,enabled)
    sql="SELECT * FROM places WHERE stage<? AND state IN ('discovered','checking','stale') AND retry_at<=?"
    args=[len(STAGES),int(time.time())]
    if stage:
        sql+=" AND stage=?"
        args.append(STAGES.index(stage))
    if ids:
        sql+=" AND id IN ("+",".join("?" for _ in ids)+")"
        args.extend(ids)
    rows=db.execute(sql+""" ORDER BY stage DESC, CASE WHEN
        COALESCE(json_extract(raw,'$.commons'), json_extract(raw,'$.wikidata'),
                 json_extract(raw,'$.tags.wikipedia'), json_extract(raw,'$.tags.image')) IS NOT NULL THEN 0
        WHEN json_extract(raw,'$.website') IS NOT NULL THEN 1 ELSE 2 END,id LIMIT ?""",(*args,limit)).fetchall()
    processed=0
    for row in rows:
        with db:
            db.execute("BEGIN IMMEDIATE")
            pid=row["id"]
            latest=db.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
            if latest["state"]!=row["state"] or latest["stage"]!=row["stage"] or latest["updated"]!=row["updated"] or latest["fingerprint"]!=row["fingerprint"] or latest["retry_at"]>int(time.time()):
                continue
            row=latest
            processed+=1
            data=json.loads(row["data"] or "{}")
            transition(db,pid,"checking","Processing")
            index=row["stage"]
            end=index+1 if stage else len(STAGES)
            state,reason="checking","Stage completed"
            while index<end:
                try:
                    reason=STAGES[index]
                    result=PROCESSORS[index](db,row,data,http)
                    if index==STAGES.index("queue"):
                        state,reason=result
                    index+=1
                except (RetryLater,ValueError,KeyError,OSError) as exc:
                    if index!=2:
                        raise
                    reason=f"{STAGES[index]}: {exc}"
                    db.execute("UPDATE places SET data=?,stage=?,retry_at=? WHERE id=?",
                               (dump(data),index,int(time.time())+getattr(exc,"seconds",900),pid))
                    transition(db,pid,"checking",reason)
                    break
            else:
                db.execute("UPDATE places SET data=?,stage=?,retry_at=0 WHERE id=?",(dump(data),index,pid))
                transition(db,pid,state,reason)
    return processed


def review(db,pid,action,change):
    row=db.execute("SELECT * FROM places WHERE id=?",(pid,)).fetchone()
    if not row or not row["data"]:
        raise ValueError("Candidate has no processed data")
    p=json.loads(row["data"])
    reason=clean(change.get("reason"))
    if not reason:
        raise ValueError("A review reason is required")
    with db:
        if action=="approve_photos":
            if row["state"] not in ("published","ready"):
                raise ValueError("Сначала подтвердите само место")
            selected=set(change.get("photos",[]))
            photos=[photo for photo in p["gallery"] if photo["source"] in selected and photo_allowed(photo)]
            if not 1<=len(photos)<=3:
                raise ValueError("Отметьте от одной до трёх фотографий")
            p["gallery"]=[{**photo,"confirmed":True,"checked_on":now()} for photo in photos]
            for photo in p["gallery"]:
                evidence(db,pid,"photo",photo,photo["source"],"rightsholder" if photo["license"]=="permission" else "commons","manual")
            p["signals"]["photos"]="approved"
            p["photos_only"]=bool(row["state"]=="published" or p.get("photos_only"))
            state="ready"
        elif action in {"approve","correct"}:
            p.pop("photos_only",None)
            for key in ("name","category","address","lat","lon","hours","price","description","website","source","coordinate_source"):
                if key in change:
                    p[key]=change[key]
            if (not valid_point(p) or not clean(p.get("name")) or p.get("category") not in CATEGORIES.values()
                or not (p.get("areal") or p.get("address") or p.get("landmark")) or not website(p.get("source"))
                or not website(p.get("coordinate_source"))
                or (action=="approve" and urlsplit(p["source"]).hostname==urlsplit(p["coordinate_source"]).hostname)):
                raise ValueError("Confirm the identity, category, coordinate source and address or area")
            if not isinstance(p.get("price"),(int,float,type(None))) or type(p.get("price")) is bool:
                raise ValueError("Price must be a number or null")
            if p.get("price") is not None and (not math.isfinite(p["price"]) or p["price"]<0):
                raise ValueError("Price must be finite and nonnegative")
            if any(p.get(key) is not None and not isinstance(p[key],str) for key in ("hours","description","address")):
                raise ValueError("Text fields must be strings or null")
            manual_photos=[]
            for image in change.get("permission_photos",[]):
                allowed={key:clean(image.get(key)) for key in ("author","attribution","source","url","permission_url")}
                photo={**allowed,"license":"permission","license_label":"С разрешения правообладателя",
                       "license_url":allowed["permission_url"],"method":"manual_permission","checked_on":now(),
                       "confirmed":True}
                if not photo_allowed(photo):
                    raise ValueError("Photo needs author, attribution, image URL, source and rights document URL")
                manual_photos.append(photo)
                evidence(db,pid,"photo",photo,photo["permission_url"],"rightsholder","manual permission")
            selected=set(change.get("photos",[]))
            for photo in p["gallery"]:
                photo["confirmed"]=photo["source"] in selected
                if photo["confirmed"]:
                    photo["confirmed"]=True
                    photo["method"]="manual"
                    photo["checked_on"]=now()
            p["gallery"]=[x for x in p["gallery"] if x["method"]!="manual_permission"]+manual_photos
            p["signals"].update(identity="manual",address="area" if p["areal"] else "present" if p.get("address") else "coordinates",
                                hours="manual" if p.get("hours") else "unknown",
                                price="manual" if p.get("price") is not None else "unknown")
            if action=="approve":
                for field in ("name","category","address","lat","lon","hours","price","description","website"):
                    source=p["coordinate_source"] if field in ("lat","lon") else p["source"]
                    evidence(db,pid,field,p.get(field),source,urlsplit(source).hostname,"manual")
                p["findings"]=[]
            if action=="approve":
                p["signals"]["photos"]="approved" if selected or manual_photos else "missing"
            state="ready" if action=="approve" else "needs_review"
        elif action=="merge":
            target=change.get("target")
            dest=db.execute("SELECT state FROM places WHERE id=?",(target,)).fetchone()
            if target==pid or not dest or dest["state"] not in ("ready","published"):
                raise ValueError("Choose a distinct current canonical record")
            db.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)",(pid,target))
            state="rejected"
        elif action in ("reject","defer"):
            state="rejected" if action=="reject" else "needs_review"
        else:
            raise ValueError("Unknown action")
        db.execute("UPDATE places SET data=?,decision=?,stage=?,retry_at=0 WHERE id=?",
                   (dump(p),dump(change),len(STAGES),pid))
        transition(db,pid,state,reason)


def atomic(path,value):
    path=Path(path)
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,delete=False) as handle:
            tmp=Path(handle.name)
            handle.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
        os.replace(tmp,path)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def export(db,path,apply=False,city=CITY):
    base=load_json(path) if path.exists() else []
    audit(base,city,allow_empty=True)
    output={p["id"]:p for p in base}
    changed=[]
    for row in db.execute("SELECT * FROM places WHERE state='ready'"):
        p=json.loads(row["data"])
        gallery=[x for x in p["gallery"] if x.get("confirmed") and photo_allowed(x)][:3]
        public={key:p.get(key) for key in ("id","name","category","address","lat","lon","hours","price",
                                          "website","description","source","coordinate_source")}
        fallback_address = (f"{city['name']}, общественная территория" if p["areal"] else
                            f"{city['name']}, точка на карте" if p.get("landmark") else None)
        public.update(address=public["address"] or fallback_address,
                      route_to_coordinates=p["areal"] or not public["address"],verified=True,verified_on=date.today().isoformat(),
                      gallery=gallery,photo=gallery[0] if gallery else None)
        if p.get("photos_only"):
            current=output.get(row["id"]) or json.loads(row["initial"])
            public={**current,"gallery":gallery,"photo":gallery[0] if gallery else None}
        output[row["id"]]=public
        changed.append(row["id"])
    withdrawn=[]
    for row in db.execute("SELECT id FROM places WHERE state='rejected'"):
        if row["id"] in output and output[row["id"]].get("verified"):
            output[row["id"]]={**output[row["id"]],"verified":False}
            withdrawn.append(row["id"])
    merged=[]
    for row in db.execute("SELECT * FROM aliases ORDER BY id"):
        if row["canonical"] not in output or not output[row["canonical"]].get("verified"):
            raise ValueError("Canonical record is not publishable")
        if row["id"] in output:
            output[row["id"]]={**output[row["id"]],"verified":False,"canonical_id":row["canonical"]}
        merged.append(dict(row))
    result=list(output.values())
    verified=audit(result,city,allow_empty=not apply)
    report={"existing":len(base),"total":len(result),"verified":len(verified),"changes":changed,"withdrawn":withdrawn,"aliases":merged}
    staging=path.with_name(path.stem+".staging.json")
    atomic(staging,result)
    atomic(path.with_name(path.stem+".report.json"),report)
    if apply:
        atomic(path.with_name(path.stem+".previous.json"),base)
        os.replace(staging,path)
        with db:
            for pid in changed:
                db.execute("UPDATE places SET initial=? WHERE id=?", (dump(output[pid]),pid))
                transition(db,pid,"published","Verified staging applied to local catalog")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=ROOT/"catalog-work.sqlite3")
    parser.add_argument("--catalog",type=Path)
    parser.add_argument("--city-config",type=Path)
    commands=parser.add_subparsers(dest="command",required=True)
    imp=commands.add_parser("import")
    imp.add_argument("--file",type=Path)
    job=commands.add_parser("process")
    job_size=job.add_mutually_exclusive_group()
    job_size.add_argument("--limit",type=int,default=20)
    job_size.add_argument("--all",action="store_true")
    job.add_argument("--stage",choices=STAGES)
    job.add_argument("--offline",action="store_true")
    job.add_argument("--id",action="append",default=[])
    photos=commands.add_parser("photos")
    photo_size=photos.add_mutually_exclusive_group()
    photo_size.add_argument("--limit",type=int,default=20)
    photo_size.add_argument("--all",action="store_true")
    stale=commands.add_parser("stale")
    stale.add_argument("--days",type=int,default=90)
    ui=commands.add_parser("review")
    ui.add_argument("--port",type=int,default=8031)
    ex=commands.add_parser("export")
    ex.add_argument("--apply",action="store_true")
    commands.add_parser("stats")
    auto=commands.add_parser("auto-publish")
    auto.add_argument("--file",type=Path)
    auto.add_argument("--offline",action="store_true")
    ai=commands.add_parser("suggest")
    ai.add_argument("--id",required=True)
    ai.add_argument("--fragment",type=Path,required=True)
    ai.add_argument("--adapter",type=Path,required=True)
    ai.add_argument("--max-records",type=int,required=True)
    ai.add_argument("--budget-micros",type=int,required=True)
    ai.add_argument("--reserve-micros",type=int,required=True)
    args=parser.parse_args()
    try:
        city = city_profile(load_json(args.city_config)) if args.city_config else CITY
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    if city != CITY and (not args.catalog or args.db == ROOT/"catalog-work.sqlite3"
                         or args.catalog.resolve() == (ROOT/"catalog.json").resolve()):
        parser.error("Для нового города укажите отдельные --db и --catalog")
    args.catalog = args.catalog or ROOT/"catalog.json"
    with closing(connect(args.db)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS city_config (id INTEGER PRIMARY KEY, profile TEXT NOT NULL)")
        with db:
            stored = db.execute("SELECT profile FROM city_config WHERE id=1").fetchone()
            if stored and json.loads(stored["profile"]) != city:
                parser.error("У этой базы уже есть настройки другого города")
            if not stored and city != CITY and db.execute("SELECT 1 FROM places LIMIT 1").fetchone():
                parser.error("Для нового города нужна отдельная база")
            db.execute("INSERT OR IGNORE INTO city_config VALUES (1,?)", (dump(city),))
        if args.catalog.exists():
            base = load_json(args.catalog)
            audit(base,city,allow_empty=True)
            import_rows(db,base,existing=True,city=city)
        if args.command=="import":
            with db:
                rows=load_json(args.file) if args.file else list(discover(Http(db).get,city))
                print(dump({"changed":import_rows(db,rows,city=city),"input":len(rows)}))
        elif args.command=="auto-publish":
            from catalog_auto import automatic_publish
            if args.offline and not args.file:
                parser.error("--offline requires --file")
            with db:
                rows=load_json(args.file) if args.file else list(discover(Http(db).get,city))
            report=automatic_publish(db,args.catalog,rows,city,not args.offline)
            print(dump({**report,"skipped":len(report["skipped"])}))
        elif args.command=="process":
            print(dump({"processed":process(db,-1 if args.all else max(1,min(args.limit,250)),not args.offline,args.stage,args.id)}))
        elif args.command=="photos":
            print(dump(scan_photos(db,-1 if args.all else max(1,min(args.limit,250)))))
        elif args.command=="stale":
            with db:
                rows=db.execute("""SELECT id FROM places WHERE state='published'
                    AND julianday(?) - julianday(COALESCE(json_extract(initial,'$.verified_on'),updated)) >= ?""",
                                (now(),args.days)).fetchall()
                for row in rows:
                    db.execute("UPDATE places SET stage=0,retry_at=0 WHERE id=?",(row["id"],))
                    transition(db,row["id"],"stale","Scheduled recheck")
                print(dump({"scheduled":len(rows)}))
        elif args.command=="export":
            print(dump(export(db,args.catalog,args.apply,city)))
        elif args.command=="suggest":
            from catalog_ai import suggest
            print(dump(suggest(db,args)))
        elif args.command=="stats":
            print(dump({row["state"]:row["n"] for row in db.execute("SELECT state, count(*) n FROM places GROUP BY state")}))
            for row in db.execute("""SELECT e.reason,count(*) n FROM places p JOIN events e ON e.rowid=
                (SELECT max(rowid) FROM events WHERE place=p.id) WHERE p.state IN ('needs_review','checking')
                GROUP BY e.reason ORDER BY n DESC LIMIT 12"""):
                print(row["n"],row["reason"])
        else:
            from catalog_review import serve
            serve(args.db,args.port)


if __name__=="__main__":
    main()

