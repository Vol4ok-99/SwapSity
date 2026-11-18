
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

from verify_catalog import AGENT, ROOT, audit, discover, photo_allowed

STAGES = ("normalize", "duplicates", "enrich", "facts", "photos", "queue")
CATEGORIES = {"museum":"culture","gallery":"culture","theatre":"culture","park":"nature",
              "garden":"nature","viewpoint":"unusual","attraction":"unusual","cinema":"fun",
              "cafe":"food","restaurant":"food","bar":"food","sports_centre":"active","circus":"fun"}
AREAS = {"park", "garden", "viewpoint"}
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


def import_rows(db, rows, existing=False):
    changed = 0
    with db:
        for raw in rows:
            pid = raw.get("id")
            if not isinstance(pid,str) or not re.fullmatch(r"[\w:-]{1,120}", pid):
                raise ValueError("Invalid place ID")
            fingerprint = hashlib.sha256(dump({k:v for k,v in raw.items() if k!="collected_on"}).encode()).hexdigest()
            old = db.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
            if existing and old:
                continue
            if old and old["fingerprint"]==fingerprint:
                continue
            state = "published" if existing and raw.get("verified") else "discovered"
            db.execute("""INSERT INTO places(id,raw,fingerprint,state,initial,updated) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,fingerprint=excluded.fingerprint,
                          state='discovered',stage=0,retry_at=0,data=NULL,updated=excluded.updated""",
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


def address_key(value):
    value=norm(value)
    aliases={"улица":"ул","дом":"д","проспект":"пр","площадь":"пл","переулок":"пер"}
    return " ".join(aliases.get(word,word) for word in value.split() if word not in {"г","город","рязань","д","ул","улица"})


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
    lat,lon=p.get("lat"),p.get("lon")
    return (type(lat) in (int,float) and type(lon) in (int,float)
            and 54.54<=lat<=54.75 and 39.55<=lon<=39.92)


def distance(a,b):
    return math.hypot((a["lat"]-b["lat"])*111200,(a["lon"]-b["lon"])*64300)


def duplicate(a,b):
    if not valid_point(a) or not valid_point(b):
        return False
    same_address=address_key(a.get("address")) and address_key(a.get("address"))==address_key(b.get("address"))
    close=distance(a,b)<90
    same_q=a.get("wikidata") and a.get("wikidata")==b.get("wikidata")
    same_phone=a.get("phone") and a.get("phone")==b.get("phone")
    similar=SequenceMatcher(None,norm(a["name"]),norm(b["name"])).ratio()>=.85
    return bool((same_q and close) or (similar and close and (same_address or same_phone or distance(a,b)<22)))


class RetryLater(Exception):
    def __init__(self, message, seconds=900):
        super().__init__(message)
        self.seconds=seconds


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
        public_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Http:
    def __init__(self, db, enabled=True, opener=None):
        self.db,self.enabled=db,enabled
        self.opener=opener or build_opener(PublicRedirect()).open
        self.last=0

    def get(self,url,data=None,method=None):
        key=hashlib.sha256((method or "").encode()+b"\0"+url.encode()+b"\0"+(data or b"")).hexdigest()
        cached=self.db.execute("SELECT body FROM cache WHERE key=? AND expires>?",(key,int(time.time()))).fetchone()
        if cached:
            return cached["body"]
        if not self.enabled:
            raise RetryLater("Network disabled")
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
                    raise RetryLater(f"Rate limit: retry after {int(delay)}s", max(900, math.ceil(delay))) from exc
                if attempt<2:
                    time.sleep(max(2**attempt,delay))
            except (OSError,URLError):
                if attempt<2:
                    time.sleep(2**attempt)
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
    p.update(id=row["id"],name=clean(raw.get("name")),category=raw.get("category") or CATEGORIES.get(hint),
             address=clean(raw.get("address")) or None,**point,hours=None,price=None,description=None,
             website=website(raw.get("website")),phone=phone(raw.get("phone") or tags.get("phone")),
             wikidata=raw.get("wikidata"),coordinate_source=raw.get("coordinate_source") or raw.get("source"),
             source=raw.get("source"),areal=hint in AREAS,findings=[],gallery=[],signals={},pending=[])
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


def enrich(db,row,p,http):
    q=p.get("wikidata")
    if isinstance(q,str) and re.fullmatch(r"Q[1-9][0-9]*",q):
        url="https://www.wikidata.org/wiki/"+q
        entity=http.json("https://www.wikidata.org/w/api.php",action="wbgetentities",ids=q,
                         props="claims|labels",languages="ru",format="json")["entities"][q]
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
        wd_label=entity.get("labels",{}).get("ru",{}).get("value")
        if wd_label and SequenceMatcher(None,norm(wd_label),norm(p["name"])).ratio()<.55:
            p["findings"].append("Название в Wikidata отличается: "+wd_label)
    url=p.get("website") or website(p.get("wikidata_website"))
    if not url:
        return
    p["website"]=url
    candidates=list(official_candidates(http.get(url)))
    matching=[(name,addr,city,geo) for name,addr,city,geo in candidates
              if SequenceMatcher(None,norm(name),norm(p["name"])).ratio()>=.78]
    for name,addr,city,geo in matching:
        evidence(db,row["id"],"address",addr,url,urlsplit(url).hostname,"JSON-LD")
        evidence(db,row["id"],"name",name,url,urlsplit(url).hostname,"JSON-LD")
        if city and "рязань" not in norm(city):
            p["findings"].append("Город филиала на сайте отличается: "+city)
        elif p.get("address") and address_key(p["address"])!=address_key(addr):
            p["findings"].append("Адрес на сайте отличается: "+addr)
        elif city and p.get("address") and re.search(r"\d",addr) and norm(name)==norm(p["name"]):
            host=urlsplit(url).hostname
            if not any(host==d or host.endswith("."+d) for d in ("openstreetmap.org","wikidata.org","wikipedia.org","wikimedia.org","wikivoyage.org")):
                p["signals"]["official_address"]=url
        if isinstance(geo,dict):
            try:
                point={"lat":float(geo["latitude"]),"lon":float(geo["longitude"])}
            except (KeyError,ValueError,TypeError):
                continue
            evidence(db,row["id"],"coordinates",point,url,urlsplit(url).hostname,"JSON-LD")
            if valid_point(p) and distance(p,point)>150:
                p["findings"].append("Координаты на сайте и в OSM не совпадают")
    if not matching:
        p["findings"].append("Сайт не подтвердил название и адрес конкретного филиала в структурированных данных")


def facts(db,row,p,http):
    if not valid_point(p):
        p["findings"].append("Координаты отсутствуют или вне области Рязани")
    wd=p.get("wikidata_point") or {}
    other={"lat":wd.get("latitude"),"lon":wd.get("longitude")}
    if valid_point(p) and all(type(v) in (int,float) for v in other.values()) and distance(p,other)>200:
        p["findings"].append("Координаты OSM и Wikidata расходятся более чем на 200 м")
    if not p.get("name") or p.get("category") not in CATEGORIES.values():
        p["findings"].append("Не определено название или категория")
    p["signals"]["address"]="area" if p.get("areal") else "present" if p.get("address") else "missing"
    p["signals"].update(hours="unknown",price="unknown",identity="unconfirmed")
    if p["signals"]["address"]=="missing":
        p["findings"].append("Нет адреса для точечного объекта")
    if not p["signals"].get("official_address"):
        p["findings"].append("Нужно независимое подтверждение идентичности и адреса")


def media(db,row,p,http):
    raw=json.loads(row["raw"])
    link=raw.get("commons") or raw.get("tags",{}).get("wikimedia_commons") or ""
    if link.startswith("https://commons.wikimedia.org/wiki/"):
        link=unquote(link.split("/wiki/",1)[1]).replace("_"," ")
    title = link if link.startswith("File:") else "File:"+p["file"] if isinstance(p.get("file"),str) else None
    category=link if link.startswith("Category:") else "Category:"+p["commons_category"] if isinstance(p.get("commons_category"),str) else None
    params=dict(action="query",prop="imageinfo",iiprop="url|extmetadata",iiurlwidth=960,format="json")
    if title:
        params["titles"]=title
    elif category:
        params.update(generator="categorymembers",gcmtitle=category,gcmtype="file",gcmlimit=5)
    else:
        p["signals"]["photos"]="missing"
        return
    result=http.json("https://commons.wikimedia.org/w/api.php",**params)
    proposals=[]
    for page in result.get("query",{}).get("pages",{}).values():
        info=(page.get("imageinfo") or [{}])[0]
        meta=info.get("extmetadata",{})
        get=lambda key: plain(meta.get(key,{}).get("value",""))
        photo=dict(source=info.get("descriptionurl"),url=info.get("thumburl"),author=get("Artist"),
                   license=get("LicenseUrl").replace("http://","https://"),license_label=get("LicenseShortName"),
                   attribution=get("Attribution"),method="linked_file" if title else "linked_category",
                   checked_on=now())
        if photo_allowed(photo) and http.get(photo["url"],method="HEAD").startswith(b"image/"):
            photo["confirmed"]=False
            proposals.append(photo)
            evidence(db,row["id"],"photo",photo,photo["source"],"commons",photo["method"])
        else:
            evidence(db,row["id"],"rejected_photo",page["title"],"https://commons.wikimedia.org/","commons","unsupported license or metadata")
    p["gallery"]=proposals
    p["signals"]["photos"]="review" if proposals else "missing"


def queue(db,row,p,http):
    accepted=p["signals"].get("official_address") and valid_point(p) and not p["findings"]
    if accepted:
        p["source"]=p["signals"]["official_address"]
        p["signals"]["identity"]="matched"
        return "ready","OSM geometry and exact name/address on linked official site"
    return "needs_review", "; ".join(p["findings"] or ["Проверить источники и идентичность"])


PROCESSORS=(normalize,find_duplicates,enrich,facts,media,queue)


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
    rows=db.execute(sql+" ORDER BY stage,id LIMIT ?",(*args,limit)).fetchall()
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
                    if result:
                        state,reason=result
                    index+=1
                except (RetryLater,ValueError,KeyError,OSError) as exc:
                    if index not in (2,4):
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
        if action in {"approve","correct"}:
            for key in ("name","category","address","lat","lon","hours","price","description","website","source","coordinate_source"):
                if key in change:
                    p[key]=change[key]
            if (not valid_point(p) or not clean(p.get("name")) or p.get("category") not in CATEGORIES.values()
                or not (p.get("areal") or p.get("address")) or not website(p.get("source"))
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
            p["signals"].update(identity="manual",address="area" if p["areal"] else "present",
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


def export(db,path,apply=False):
    base=load_json(path)
    audit(base)
    output={p["id"]:p for p in base}
    changed=[]
    for row in db.execute("SELECT * FROM places WHERE state='ready'"):
        p=json.loads(row["data"])
        gallery=[x for x in p["gallery"] if x.get("confirmed") and photo_allowed(x)][:3]
        public={key:p.get(key) for key in ("id","name","category","address","lat","lon","hours","price",
                                          "website","description","source","coordinate_source")}
        public.update(address=public["address"] or ("Рязань, общественная территория" if p["areal"] else None),
                      route_to_coordinates=p["areal"],verified=True,verified_on=date.today().isoformat(),
                      gallery=gallery,photo=gallery[0] if gallery else None)
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
    verified=audit(result)
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
    parser.add_argument("--catalog",type=Path,default=ROOT/"catalog.json")
    commands=parser.add_subparsers(dest="command",required=True)
    imp=commands.add_parser("import")
    imp.add_argument("--file",type=Path)
    job=commands.add_parser("process")
    job.add_argument("--limit",type=int,default=20)
    job.add_argument("--stage",choices=STAGES)
    job.add_argument("--offline",action="store_true")
    job.add_argument("--id",action="append",default=[])
    stale=commands.add_parser("stale")
    stale.add_argument("--days",type=int,default=90)
    ui=commands.add_parser("review")
    ui.add_argument("--port",type=int,default=8031)
    ex=commands.add_parser("export")
    ex.add_argument("--apply",action="store_true")
    commands.add_parser("stats")
    ai=commands.add_parser("suggest")
    ai.add_argument("--id",required=True)
    ai.add_argument("--fragment",type=Path,required=True)
    ai.add_argument("--adapter",type=Path,required=True)
    ai.add_argument("--max-records",type=int,required=True)
    ai.add_argument("--budget-micros",type=int,required=True)
    ai.add_argument("--reserve-micros",type=int,required=True)
    args=parser.parse_args()
    with closing(connect(args.db)) as db:
        import_rows(db,load_json(args.catalog),existing=True)
        if args.command=="import":
            with db:
                rows=load_json(args.file) if args.file else list(discover(Http(db).get))
                print(dump({"changed":import_rows(db,rows),"input":len(rows)}))
        elif args.command=="process":
            print(dump({"processed":process(db,max(1,min(args.limit,250)),not args.offline,args.stage,args.id)}))
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
            print(dump(export(db,args.catalog,args.apply)))
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

