
import io
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from catalog_pipeline import ROOT, Http, RetryLater, connect, duplicate, export, import_rows, process, review
from verify_catalog import photo_allowed


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

