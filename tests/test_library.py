import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from comic_pipeline.library import Conflict, WorkLibrary
from comic_pipeline.reader import ReaderService
from comic_pipeline.work_memory import WorkMemory
import test_reader
from test_reader import FakeProcessor, finished, payload
from test_work_memory import block, commit, delta


BOOK = "bookwalker:00000000-0000-0000-0000-000000000001"


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.store = WorkMemory(":memory:")
        self.addCleanup(self.store.close)
        self.library = WorkLibrary(self.store)
        self.work = self.store.create("測試作品", "作者")

    def value(self, **kwargs):
        return dict(version=self.store.get(self.work["id"])["data_version"], **kwargs)

    def test_reader_title_reuses_exact_work_and_never_enables_memory(self):
        first = self.library.register(dict(ref=BOOK, title="測試作品"))
        self.assertEqual(first["catalogue_id"], self.work["id"])
        second = self.library.register(dict(ref=BOOK, title="閱讀器顯示的別名"))
        self.assertEqual(first["catalogue_id"], second["catalogue_id"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.library.detail(self.work["id"])["books"][0]["title"], "閱讀器顯示的別名")
        self.assertEqual(self.store.get(self.work["id"])["pages"], 0)
        self.assertIsNone(self.store.get(self.work["id"])["profile"])
        for ref in ("https://reader.example/?signed=private", "bookwalker:not-a-book"):
            with self.assertRaises(ValueError):
                self.library.register(dict(ref=ref, title="測試"))

    def test_missing_title_gets_renamed_when_real_title_arrives(self):
        result = self.library.register(dict(ref=BOOK))
        actual = self.library.register(dict(ref=BOOK, title="真正的閱讀器書名"))
        self.assertEqual(result["catalogue_id"], actual["catalogue_id"])
        self.assertEqual(actual["title"], "真正的閱讀器書名")

    def test_ambiguous_names_do_not_merge_and_explicit_binding_wins(self):
        self.store.create("測試作品", "另一位作者")
        result = self.library.register(dict(ref=BOOK, title="測試作品"))
        self.assertNotEqual(result["catalogue_id"], self.work["id"])
        bound = self.library.register(dict(ref=BOOK, title="別名", work_id=self.work["id"]))
        self.assertEqual(bound["catalogue_id"], self.work["id"])

    def test_same_title_is_separate_between_platforms_but_reused_within_platform(self):
        bw = self.library.register(dict(ref=BOOK, title="測試作品"))
        pixiv = self.library.register(dict(ref="pixiv:work:5424", title="測試作品"))
        same_bw = self.library.register(dict(ref=BOOK[:-1]+"2", title="測試作品"))
        same_pixiv = self.library.register(dict(ref="pixiv:work:5425", title="測試作品"))
        self.assertNotEqual(bw["catalogue_id"], pixiv["catalogue_id"])
        self.assertEqual(bw["catalogue_id"], same_bw["catalogue_id"])
        self.assertEqual(pixiv["catalogue_id"], same_pixiv["catalogue_id"])
        rows = {w["id"]: w for w in self.library.list()}
        self.assertEqual(rows[bw["catalogue_id"]]["platforms"], ["bookwalker"])
        self.assertEqual(rows[pixiv["catalogue_id"]]["platforms"], ["pixiv"])
        self.assertEqual(self.library.detail(pixiv["catalogue_id"])["books"][0]["platform"], "pixiv")

    def test_explicit_cross_platform_binding_rolls_back_without_changing_any_data(self):
        bw = self.library.register(dict(ref=BOOK, title="測試作品"))
        pixiv = self.library.register(dict(ref="pixiv:work:5424", title="測試作品"))
        before = [self.library.export(w["id"]) for w in self.store.list()]
        for ref, identifier in (("pixiv:work:5424", bw["catalogue_id"]), (BOOK, pixiv["catalogue_id"])):
            with self.assertRaisesRegex(ValueError, "平台"):
                self.library.register(dict(ref=ref, title="測試作品", work_id=identifier))
        self.assertEqual(before, [self.library.export(w["id"]) for w in self.store.list()])

    def test_stale_browser_binding_does_not_override_a_different_platform(self):
        bw = self.library.register(dict(ref=BOOK, title="測試作品"))
        pixiv = self.library.register(dict(ref="pixiv:work:5424", title="測試作品", work_id=bw["catalogue_id"], prefer_existing=True))
        self.assertNotEqual(pixiv["catalogue_id"], bw["catalogue_id"])
        again = self.library.register(dict(ref="pixiv:work:5424", title="測試作品", work_id=bw["catalogue_id"], prefer_existing=True))
        self.assertEqual(again["catalogue_id"], pixiv["catalogue_id"])
        self.assertEqual(len(self.store.list()), 2)

    def test_legacy_physical_pages_define_platform_even_without_catalogue_binding(self):
        commit(self.store, self.work, "pixiv:episode:50313:page:0")
        self.assertEqual(self.library.detail(self.work["id"])["platforms"], ["pixiv"])
        bw = self.library.register(dict(ref=BOOK, title="測試作品"))
        self.assertNotEqual(bw["catalogue_id"], self.work["id"])
        self.assertEqual(self.store.get(self.work["id"])["pages"], 1)

    def test_unassigned_work_is_claimed_by_its_first_platform(self):
        self.assertEqual(self.library.detail(self.work["id"])["platforms"], [])
        result = self.library.register(dict(ref="pixiv:work:5424", title="測試作品"))
        self.assertEqual(result["catalogue_id"], self.work["id"])
        with self.assertRaisesRegex(ValueError, "平台"):
            self.library.register(dict(ref=BOOK, title="測試作品", work_id=self.work["id"]))

    def test_legacy_title_reference_is_a_bookwalker_origin(self):
        result = self.library.register(dict(ref="title:測試作品", title="測試作品"))
        self.assertEqual(self.library.detail(result["catalogue_id"])["platforms"], ["bookwalker"])
        other = self.library.register(dict(ref="pixiv:work:5424", title="測試作品"))
        self.assertNotEqual(other["catalogue_id"], result["catalogue_id"])

    def test_same_title_platforms_have_independent_profiles_memories_and_translation_cleanup(self):
        self.library.register(dict(ref=BOOK, title="測試作品"))
        other = self.library.register(dict(ref="pixiv:work:5424", title="測試作品"))
        pixiv = self.store.get(other["catalogue_id"])
        for work, name in ((self.work, "BookWalker"), (pixiv, "Pixiv")):
            self.library.update(work["id"], dict(version=self.store.get(work["id"])["data_version"], profile=dict(user_instructions=name)))
            commit(self.store, work, "same-render-key", blocks=[block(translation=name)])
        saved = self.library.export(pixiv["id"])
        self.library.clear(self.work["id"], self.value())
        self.assertEqual(self.library.export(pixiv["id"]), saved)
        self.assertEqual(self.library.detail(pixiv["id"])["stats"]["translations"], 1)
        self.assertEqual(self.library.detail(pixiv["id"])["profile"]["user_instructions"], "Pixiv")

    def test_profile_versions_restore_and_inflight_commit_is_rejected(self):
        stale = commit(self.store, self.work, "before")
        epoch = self.work["epoch"]
        one = self.library.update(self.work["id"], self.value(profile=dict(user_instructions="保留敬稱", translation_style=["自然口語"])))
        self.assertNotEqual(one["epoch"], epoch)
        self.assertEqual(one["revision"], 1)
        self.assertFalse(self.store.commit_page(dict(stale, page="inflight")))
        self.library.update(self.work["id"], self.value(profile=dict(user_instructions="更自然")))
        restored = self.library.restore_profile(self.work["id"], self.value(revision=1))
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["profile"]["user_instructions"], "保留敬稱")
        self.assertEqual(len(restored["profile_versions"]), 3)

    def test_optimistic_version_rejects_concurrent_reading_or_editor(self):
        value = self.value(title="不能蓋掉新資料")
        commit(self.store, self.work, "new")
        with self.assertRaises(Conflict):
            self.library.update(self.work["id"], value)
        self.assertEqual(self.store.get(self.work["id"])["title"], "測試作品")
        for value in ({}, dict(version=True), dict(version="1")):
            with self.assertRaises(Conflict):
                self.library.delete(self.work["id"], value)

    def test_page_edit_rebuilds_later_context_without_future_facts(self):
        commit(self.store, self.work, "one")
        commit(self.store, self.work, "two", observation=delta(note="第二頁才發生的事"))
        commit(self.store, self.work, "three", observation=delta(note="第三頁的秘密"))
        segment = self.work["current_segment"]
        edited = block(translation="明天再見。")
        self.library.edit_page(self.work["id"], self.value(segment_id=segment, page_key="one", blocks=[edited], delta=delta(note="修正後的第一頁記憶")))
        first = self.library.page(self.work["id"], segment, "one")
        second = self.library.page(self.work["id"], segment, "two")
        third = self.library.page(self.work["id"], segment, "three")
        self.assertEqual(first["context"]["recent_pages"], [])
        self.assertEqual(second["context"]["recent_pages"][0]["dialogue"][0]["translation"], "明天再見。")
        self.assertIn("修正後的第一頁記憶", json.dumps(second["context"], ensure_ascii=False))
        self.assertNotIn("第二頁才發生", json.dumps(second["context"], ensure_ascii=False))
        self.assertNotIn("第三頁的秘密", json.dumps(third["context"], ensure_ascii=False))
        self.assertEqual(self.library.detail(self.work["id"])["stats"]["translations"], 0)

    def test_invalid_evidence_rolls_back_every_edit(self):
        commit(self.store, self.work, "one")
        before = self.library.export(self.work["id"])
        with self.assertRaises(ValueError):
            self.library.edit_page(self.work["id"], self.value(segment_id=self.work["current_segment"], page_key="one", blocks=[block(text="違う原文。")], delta=delta()))
        self.assertEqual(before, self.library.export(self.work["id"]))
        for profile in ([], dict(glossary=["bad"]), dict(translation_style=None), dict(synopsis=3)):
            with self.assertRaises(ValueError):
                self.library.update(self.work["id"], self.value(title="不應更名", profile=profile))
            self.assertEqual(before, self.library.export(self.work["id"]))

    def test_delete_page_and_clear_segment_keep_other_segment_and_profile(self):
        self.library.update(self.work["id"], self.value(profile=dict(user_instructions="保留人名")))
        commit(self.store, self.work, "one")
        commit(self.store, self.work, "two")
        old_segment = self.work["current_segment"]
        self.library.edit_page(self.work["id"], self.value(segment_id=old_segment, page_key="one"), delete=True)
        self.assertEqual(self.library.page(self.work["id"], old_segment, "two")["context"]["recent_pages"], [])
        self.store.segment(self.work["id"], "第二卷")
        commit(self.store, self.work, "another")
        self.library.clear(self.work["id"], self.value(segment_id=old_segment))
        current = self.library.detail(self.work["id"])
        self.assertEqual(current["pages"], 1)
        self.assertEqual(current["profile"]["user_instructions"], "保留人名")

    def test_translation_clear_preserves_memory_and_export_cascades(self):
        self.library.register(dict(ref=BOOK, title="測試作品"))
        commit(self.store, self.work, "one")
        backup = self.library.export(self.work["id"])
        self.assertEqual(len(backup["translations"]), 1)
        self.assertEqual(len(backup["work"]["books"]), 1)
        self.library.clear(self.work["id"], self.value(), translations=True)
        self.assertEqual(self.library.detail(self.work["id"])["pages"], 1)
        self.assertEqual(self.library.detail(self.work["id"])["stats"]["translations"], 0)
        self.library.delete(self.work["id"], self.value())
        for table in ("reader_books", "profiles", "segments", "pages", "translations"):
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_pagination_and_foreign_segment_bounds(self):
        for n in range(4):
            commit(self.store, self.work, str(n))
        # Equal timestamps still produce a consistent order across batches.
        self.store.db.execute("UPDATE pages SET created=123 WHERE work_id=?", (self.work["id"],))
        first = self.library.pages(self.work["id"], limit=2)
        self.assertEqual([p["seq"] for p in first["pages"]], [4, 3])
        result = self.library.pages(self.work["id"], offset=2, limit=2)
        self.assertEqual([p["seq"] for p in result["pages"]], [2, 1])
        self.assertEqual(result["total"], 4)
        other = self.store.create("其他作品")
        with self.assertRaises(ValueError):
            self.library.pages(self.work["id"], other["current_segment"])

    def test_old_database_migrates_without_losing_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"memory.sqlite3"
            old = WorkMemory(path)
            work = old.create("原有作品")
            commit(old, work, "saved")
            old.close()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("ALTER TABLE works DROP COLUMN data_version")
                db.execute("DROP TABLE reader_books")
            migrated = WorkMemory(path)
            try:
                self.assertEqual(migrated.get(work["id"])["pages"], 1)
                self.assertEqual(WorkLibrary(migrated).detail(work["id"])["stats"]["translations"], 1)
                self.assertTrue(path.with_name("memory-before-library.sqlite3").is_file())
            finally:
                migrated.close()

    def test_nested_transactions_are_atomic(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.create("不應留下的作品")
                raise RuntimeError("rollback")
        self.assertEqual(len(self.store.list()), 1)


class LibraryRuntimeTests(unittest.TestCase):
    def test_research_fills_the_existing_empty_catalogue_without_duplicates(self):
        class Researcher:
            def build(self, work):
                return dict(title_ja=work["title"]),dict(sources=[]),[]
        service = ReaderService(FakeProcessor(), researcher_factory=lambda _: Researcher())
        self.addCleanup(service.close)
        service.configure(dict(key="test-only-key"))
        catalogue = service.library.register(dict(ref=BOOK, title="同一部作品"))
        work = service.create_work(dict(ref=BOOK, title="同一部作品", author="作者", volume="第 1 卷"))
        self.assertEqual(work["id"], catalogue["catalogue_id"])
        service.research_pool.submit(lambda: None).result(2)
        self.assertEqual(len(service.memory.list()), 1)
        self.assertEqual(service.memory.get(work["id"])["status"], "ready")

    def test_research_creation_respects_platform_scope_and_unscoped_local_creation(self):
        class Researcher:
            def build(self, work):
                return dict(title_ja=work["title"]), dict(sources=[]), []
        service = ReaderService(FakeProcessor(), researcher_factory=lambda _: Researcher())
        self.addCleanup(service.close)
        service.configure(dict(key="test-only-key"))
        bw = service.library.register(dict(ref=BOOK, title="同名作品"))
        pixiv = service.create_work(dict(ref="pixiv:work:5424", title="同名作品"))
        local = service.create_work(dict(title="同名作品"))
        service.research_pool.submit(lambda: None).result(2)
        self.assertEqual(len(service.memory.list()), 3)
        self.assertNotEqual(bw["catalogue_id"], pixiv["id"])
        self.assertEqual(service.library.detail(pixiv["id"])["platforms"], ["pixiv"])
        self.assertEqual(service.library.detail(local["id"])["platforms"], [])
        self.assertEqual(service.memory.get(bw["catalogue_id"])["status"], "empty")

    def test_edit_cancels_inflight_job_and_old_catalogue_cannot_restart(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeProcessor):
            def process(self, *args):
                entered.set(); release.wait(3)
                return super().process(*args)
        service = ReaderService(Slow())
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        binding = service.library.register(dict(ref=BOOK, title="閱讀中作品"))
        job = service.submit(dict(payload(), **binding))
        self.assertTrue(entered.wait(1))
        work = service.memory.get(binding["catalogue_id"])
        service.manage_work(work["id"], "update", dict(version=work["data_version"], title="更名"))
        self.assertIsNone(service.result_image(job["id"]))
        self.assertEqual(service.library_runtime(work["id"])["jobs"], [])
        with self.assertRaises(ValueError):
            service.submit(dict(payload(), **binding))
        release.set()
        service.pool.submit(lambda: None).result(2)
        self.assertIsNone(service.get(job["id"]))
        self.assertFalse(service.cache)

    def test_per_work_clear_does_not_remove_another_works_images(self):
        service = ReaderService(FakeProcessor())
        self.addCleanup(service.close)
        one = service.library.register(dict(ref=BOOK, title="一"))
        two = service.library.register(dict(ref=BOOK[:-1]+"2", title="二"))
        first = finished(service, service.submit(dict(payload(), **one))["id"])
        second = finished(service, service.submit(dict(payload(), **two))["id"])
        self.assertNotEqual(first["id"], second["id"])
        work = service.memory.get(one["catalogue_id"])
        service.manage_work(work["id"], "clear-runtime", dict(version=work["data_version"]))
        self.assertIsNone(service.result_image(first["id"]))
        self.assertTrue(service.result_image(second["id"]))


class LibraryHTTPTests(unittest.TestCase):
    setUp = test_reader.ReaderHTTPTests.setUp
    tearDown = test_reader.ReaderHTTPTests.tearDown
    request = test_reader.ReaderHTTPTests.request
    def test_manager_entry_assets_registration_and_complete_export(self):
        for path in ("/library", "/library.css", "/library.js", "/library-platforms.js"):
            status, headers, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/library")[1]["X-Frame-Options"], "DENY")
        status, _, body = self.request("POST", "/api/library/books", dict(ref=BOOK, title="書名"), self.origin)
        self.assertEqual(status, 200)
        identifier = json.loads(body)["catalogue_id"]
        _, _, body = self.request("GET", "/api/library/"+identifier)
        work = json.loads(body)
        status, _, _ = self.request("POST", f"/api/library/{identifier}/update", dict(version=work["data_version"], profile=dict(user_instructions="自然口語")), self.origin)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", f"/api/library/{identifier}/delete", dict(version=work["data_version"]), self.origin)[0], 409)
        status, _, body = self.request("GET", "/api/library/export")
        backup = json.loads(body)
        self.assertEqual(backup["format"], "comic-library-backup-v1")
        self.assertEqual(backup["works"][0]["work"]["profile"]["user_instructions"], "自然口語")
        self.assertEqual(backup["works"][0]["work"]["books"][0]["title"], "書名")
        self.assertEqual(self.request("GET", "/api/library/"+identifier+"/pages?limit=999")[0], 404)

    def test_work_picker_api_exposes_platforms_and_does_not_merge_same_titles(self):
        identifiers = []
        for ref in (BOOK, "pixiv:work:5424"):
            status, _, body = self.request("POST", "/api/library/books", dict(ref=ref, title="同名作品"), self.origin)
            self.assertEqual(status, 200)
            identifiers.append(json.loads(body)["catalogue_id"])
        self.assertNotEqual(*identifiers)
        _, _, body = self.request("GET", "/api/works")
        rows = {w["id"]: w for w in json.loads(body)["works"]}
        for identifier, platform in zip(identifiers, ("bookwalker", "pixiv")):
            self.assertEqual(rows[identifier]["platforms"], [platform])
            _, _, body = self.request("GET", "/api/works/"+identifier)
            self.assertEqual(json.loads(body)["platforms"], [platform])

    def test_manager_data_and_mutations_keep_same_origin_boundaries(self):
        status, _, body = self.request("POST", "/api/library/create", dict(title="無需金鑰"), self.origin)
        work = json.loads(body)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/api/library/books", dict(ref=BOOK, title="x"), "https://example.com")[0], 403)
        for path in ("/api/library", "/api/library/export", "/api/library/"+work["id"]):
            self.assertEqual(self.request("GET", path, origin="https://example.com")[0], 403)
        self.assertEqual(self.request("POST", f"/api/library/{work['id']}/delete", dict(version=work["data_version"]), "https://example.com")[0], 403)
        self.assertEqual(self.request("GET", "/api/library/"+work["id"])[0], 200)
