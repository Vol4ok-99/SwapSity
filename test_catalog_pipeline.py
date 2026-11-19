
import io
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from catalog_pipeline import ROOT, Http, RetryLater, connect, duplicate, export, import_rows, process, review, scan_photos
from verify_catalog import audit, discover, photo_allowed


def candidate(**changes):
    return dict(id="osm:node:1111", name="Кафе Пилот", category_hint="cafe", address="Ленина, 6",
                lat=54.63, lon=39.74, source="https://www.openstreetmap.org/node/1111", **changes)


def site(address):
    record={"@type":"CafeOrCoffeeShop","name":"Кафе Пилот",
            "address":{"streetAddress":address,"addressLocality":"Рязань"}}
    return ('<script type="application/ld+json">'+json.dumps(record)+'</script>').encode()


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db=connect(Path(self.temp.name)/"test.sqlite3")
        self.addCleanup(self.db.close)

    def state(self):
        return self.db.execute("SELECT * FROM places WHERE id='osm:node:1111'").fetchone()

    def test_second_city_discovery_and_export(self):
        city={"name":"Тула","bounds":[54.1,37.5,54.3,37.8],"language":"ru"}
        element={"type":"node","id":1111,"lat":54.2,"lon":37.6,
                 "tags":{"name":"Памятник","historic":"monument"}}
        with patch("verify_catalog.request") as network:
            rows=list(discover(lambda *_: json.dumps({"elements":[element,{**element,"lon":39}]}),city))
            network.assert_not_called()
        self.assertEqual(len(rows),1)
        import_rows(self.db,rows,city=city)
        process(self.db,enabled=False)
        self.assertEqual(self.state()["state"],"needs_review")
        review(self.db,rows[0]["id"],"approve",{"source":"https://example.org/","reason":"Verified landmark"})
        catalog=Path(self.temp.name)/"new-city.json"
        report=export(self.db,catalog,city=city)
        self.assertEqual(report["verified"],1)
        published=audit(json.loads(catalog.with_suffix(".staging.json").read_bytes()),city)[0]
        self.assertTrue(published["route_to_coordinates"])
        self.assertEqual(published["address"],"Тула, точка на карте")

    def test_photo_fallback_updates_only_gallery_in_seeded_record(self):
        public={**candidate(),"category":"food","kind":"Авторское описание","verified":True,
                "verified_on":"2025-01-01","source":"https://example.org/",
                "coordinate_source":"https://www.openstreetmap.org/node/1111","gallery":[],"photo":None}
        import_rows(self.db,[public],existing=True)
        source="https://commons.wikimedia.org/wiki/File:Pilot.jpg"
        photo={"descriptionurl":source,"thumburl":"https://upload.wikimedia.org/test.jpg","mime":"image/jpeg",
               "extmetadata":{"Artist":{"value":"Example"},"LicenseUrl":{"value":"https://creativecommons.org/licenses/by/4.0/"}}}
        pages={"1":{"imageinfo":[photo]},"2":{"imageinfo":[{**photo,"descriptionurl":source+"2",
                "extmetadata":{**photo["extmetadata"],"Restrictions":{"value":"Non-commercial"}}}]}}
        with patch.object(Http,"json",return_value={"query":{"pages":pages}}) as http:
            self.assertEqual(scan_photos(self.db,20)["with_suggestions"],1)
            self.assertIn('"Кафе Пилот" "Рязань"',http.call_args.kwargs["gsrsearch"])
            self.assertEqual(scan_photos(self.db,20)["processed"],0)
        self.assertEqual(len(json.loads(self.state()["data"])["gallery"]),1)
        catalog=Path(self.temp.name)/"catalog.json"
        catalog.write_text(json.dumps([public]),encoding="utf-8")
        self.assertEqual(export(self.db,catalog)["changes"],[])
        review(self.db,public["id"],"approve_photos",{"reason":"Same place","photos":[source],"name":"Ignored"})
        export(self.db,catalog,True)
        published=json.loads(catalog.read_bytes())[0]
        self.assertEqual(published.pop("photo")["source"],source)
        self.assertEqual(len(published.pop("gallery")),1)
        self.assertEqual(published,{k:v for k,v in public.items() if k not in {"photo","gallery"}})

    def test_official_address_can_fill_missing_osm_address(self):
        raw={**candidate(website="https://example.org/"),"address":None}
        html=site("Ленина, 6")
        geo=b', "geo": {"latitude": 54.63, "longitude": 39.74}}'
        html=html.replace(b'}}</script>',b'}'+geo+b'</script>')
        import_rows(self.db,[raw])
        with patch.object(Http,"get",return_value=html),patch.object(Http,"json",return_value={}):
            process(self.db)
        self.assertEqual(self.state()["state"],"ready")
        self.assertEqual(json.loads(self.state()["data"])["address"],"Ленина, 6")

    def test_auto_publish_needs_no_manual_decisions(self):
        from catalog_auto import automatic_publish
        rows=[candidate(hours="09:00-18:00", tags={"description":"Местное кафе"}),
              {**candidate(), "id":"osm:node:1112", "source":"https://www.openstreetmap.org/node/1112"},
              {**candidate(), "id":"osm:node:1113", "source":"https://www.openstreetmap.org/node/1113",
               "tags":{"disused:amenity":"cafe"}}]
        catalog=Path(self.temp.name)/"auto.json"
        first=automatic_publish(self.db,catalog,rows,online=False)
        self.assertEqual(first["added"],1)
        self.assertEqual(len(first["skipped"]),2)
        [place]=audit(json.loads(catalog.read_bytes()))
        self.assertEqual(place["hours"],"09:00-18:00")
        self.assertEqual(place["description"],"Местное кафе")
        self.assertEqual(place["source_type"],"osm_import")
        self.assertEqual(automatic_publish(self.db,catalog,rows,online=False)["added"],0)
        with self.assertRaises(ValueError):
            automatic_publish(self.db,catalog,[],online=False)
        self.assertEqual(len(audit(json.loads(catalog.read_bytes()))),1)

    def test_duplicates_and_branches(self):
        first=candidate()
        same={**first,"address":"г. Рязань, ул. Ленина, д. 6","lon":39.7401}
        branch={**first,"address":"Ленина, 8","lon":39.742}
        unrelated={**first,"name":"Студия балета"}
        self.assertTrue(duplicate(first,same))
        self.assertFalse(duplicate(first,branch))
        self.assertFalse(duplicate(first,unrelated))

    def test_official_address_conflict(self):
        import_rows(self.db,[candidate(website="https://example.org/")])
        with patch.object(Http,"get",return_value=site("Ленина, 14")):
            self.assertEqual(process(self.db),1)
        self.assertEqual(self.state()["state"],"needs_review")
        self.assertIn("Адрес на сайте отличается",self.state()["data"])

    def test_ready_without_photo_and_reimport(self):
        raw=candidate(website="https://example.org/")
        import_rows(self.db,[raw])
        with patch.object(Http,"get",return_value=site("ул. Ленина, д. 6")):
            process(self.db)
        self.assertEqual(self.state()["state"],"ready")
        self.assertEqual(import_rows(self.db,[{**raw,"collected_on":"2026-11-18"}]),0)
        self.assertEqual(process(self.db),0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM places").fetchone()[0],1)
        self.assertEqual(json.loads(self.state()["data"])["gallery"],[])
        catalog=Path(self.temp.name)/"catalog.json"
        catalog.write_bytes((ROOT/"catalog.json").read_bytes())
        old=catalog.read_bytes()
        report=export(self.db,catalog)
        self.assertEqual(report["changes"],[raw["id"]])
        self.assertEqual(catalog.read_bytes(),old)
        export(self.db,catalog,True)
        self.assertEqual((catalog.parent/"catalog.previous.json").read_bytes().decode().strip(),
                         old.decode().strip())
        published=next(p for p in json.loads(catalog.read_bytes()) if p["id"]==raw["id"])
        self.assertIsNone(published["photo"])
        self.assertIsNone(published["price"])
        self.assertEqual(self.state()["state"],"published")

    def test_unknown_license(self):
        photo={"url":"https://upload.wikimedia.org/test.jpg","author":"X",
               "source":"https://commons.wikimedia.org/wiki/File:Test.jpg",
               "license":"https://creativecommons.org/licenses/by-nc/4.0/"}
        self.assertFalse(photo_allowed(photo))
        self.assertTrue(photo_allowed({**photo,"license":"https://creativecommons.org/licenses/by/4.0/"}))

    def test_temporary_failure_waits_at_stage(self):
        import_rows(self.db,[candidate(website="https://example.org/")])
        with patch.object(Http,"get",side_effect=RetryLater("429",1900)):
            self.assertEqual(process(self.db),1)
        self.assertEqual(self.state()["state"],"checking")
        self.assertEqual(self.state()["stage"],2)
        self.assertGreater(self.state()["retry_at"],0)
        self.assertEqual(process(self.db),0)

    def test_retry_after_and_cache(self):
        class Response(io.BytesIO):
            pass
        def throttle(req,timeout):
            raise HTTPError(req.full_url,429,"Too many",{"Retry-After":"1800"},None)
        with patch("catalog_pipeline.public_url"):
            with self.assertRaises(RetryLater) as retry:
                Http(self.db,opener=throttle).get("https://example.org/")
            self.assertEqual(retry.exception.seconds,1800)
            with self.db:
                Http(self.db,opener=lambda req,timeout:Response(b"ok")).get("https://example.org/")
            self.assertEqual(Http(self.db,enabled=False).get("https://example.org/"),b"ok")

    def test_decision_persists_until_source_change(self):
        raw=candidate()
        import_rows(self.db,[raw])
        process(self.db)
        review(self.db,raw["id"],"reject",{"reason":"Закрытие подтверждено"})
        self.assertEqual(import_rows(self.db,[raw]),0)
        self.assertEqual(process(self.db),0)
        self.assertEqual(self.state()["state"],"rejected")
        import_rows(self.db,[{**raw,"address":"Ленина, 8"}])
        self.assertEqual(self.state()["state"],"discovered")

    def test_failed_export_preserves_previous(self):
        import_rows(self.db,[candidate()])
        process(self.db)
        review(self.db,"osm:node:1111","approve",{"source":"https://example.org/","reason":"Сверено"})
        bad=json.loads(self.state()["data"])
        bad["lat"]=0
        with self.db:
            self.db.execute("UPDATE places SET data=?",(json.dumps(bad),))
        path=Path(self.temp.name)/"catalog.json"
        path.write_bytes((ROOT/"catalog.json").read_bytes())
        backup=path.with_name("catalog.previous.json")
        backup.write_text("previous version",encoding="utf-8")
        old=path.read_bytes()
        with self.assertRaises(ValueError):
            export(self.db,path,True)
        self.assertEqual(path.read_bytes(),old)
        self.assertEqual(backup.read_text(encoding="utf-8"),"previous version")


if __name__=="__main__":
    unittest.main()

