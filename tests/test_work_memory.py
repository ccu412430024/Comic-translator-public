import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from comic_pipeline.gemini import GeminiClient, GeminiError
from comic_pipeline.reader import ReaderProcessor, ReaderService
from comic_pipeline.work_memory import WorkMemory, grounded_delta
from comic_pipeline.work_research import validate_profile
from comic_pipeline.translation import GeminiTranslator
from test_reader import payload, finished
from test_reader_processing import FakeEngine


def block(text="明日会おう。", translation="明天見。"):
    return dict(id="b0001", bbox=[50,30,130,200], bubble_bbox=[10,5,170,235], orientation="vertical",
                source_text=text, translation=translation, preserve_original=False, **{"class":"text_bubble"})


def delta(text="明日会おう。", note="兩人約好明天見面。"):
    return grounded_delta(dict(scene=dict(text=note,certainty="explicit",evidence=[dict(id="b0001",quote=text)])), [block(text)])


def commit(store, work, page, blocks=None, observation=None, context=None, model="gemini-test"):
    selection, segment = store.selection(work["id"])
    blocks = blocks or [block()]
    context = context or store.context(selection, segment, page, blocks)
    value = dict(selection=selection,segment=segment,page=page,context=context,blocks=blocks,
                 delta=observation or delta(),mode="standard",model=model)
    store.commit_page(value)
    return value


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = WorkMemory(":memory:")
        self.addCleanup(self.store.close)
        self.work = self.store.create("測試漫畫", "作者")

    def test_duplicate_page_is_not_learned_twice(self):
        commit(self.store,self.work,"one")
        commit(self.store,self.work,"one")
        self.assertEqual(self.store.get(self.work["id"])["pages"],1)

    def test_later_page_receives_prior_original_dialogue_and_evidence(self):
        commit(self.store,self.work,"one")
        selection,segment=self.store.selection(self.work["id"])
        context=self.store.context(selection,segment,"two",[block()])
        self.assertEqual(context["dynamic"]["recent_pages"][0]["dialogue"][0]["source_text"],"明日会おう。")
        self.assertEqual(context["dynamic"]["grounded_history"][0]["evidence"][0]["quote"],"明日会おう。")

    def test_revisit_uses_original_pre_page_snapshot_without_future_events(self):
        commit(self.store,self.work,"one")
        second=commit(self.store,self.work,"two",observation=delta(note="後一頁才發生的事件"))
        commit(self.store,self.work,"three",observation=delta(note="更後面的秘密"))
        selection,segment=self.store.selection(self.work["id"])
        self.assertEqual(self.store.context(selection,segment,"two",[block()])["dynamic"],second["context"]["dynamic"])
        first=self.store.context(selection,segment,"one",[block()])
        self.assertEqual(first["dynamic"]["recent_pages"],[])
        self.assertNotIn("秘密",json.dumps(first,ensure_ascii=False))

    def test_other_works_and_new_segments_do_not_receive_old_dialogue(self):
        commit(self.store,self.work,"one")
        other=self.store.create("另一部作品")
        for target in [other,self.store.segment(self.work["id"],"第 2 卷")]:
            selection,segment=self.store.selection(target["id"])
            self.assertEqual(self.store.context(selection,segment,"new",[block()])["dynamic"]["recent_pages"],[])

    def test_segment_from_other_work_is_rejected(self):
        other=self.store.create("另一部作品")
        with self.assertRaises(ValueError):
            self.store.selection(self.work["id"],other["current_segment"])

    def test_delete_prevents_inflight_commit_from_restoring_work(self):
        value=commit(self.store,self.work,"one")
        self.store.delete(self.work["id"])
        self.assertFalse(self.store.commit_page(value))
        self.assertEqual(self.store.list(),[])

    def test_unverified_evidence_and_terms_are_dropped(self):
        values=dict(scene=dict(text="死亡",certainty="explicit",evidence=[dict(id="missing",quote="明日")]),
            facts=[dict(text="秘密",certainty="explicit",evidence=[dict(id="b0001",quote="根本不存在的原文")])],
            terms=[dict(ja="アリス",zh_tw="愛莉絲",evidence=[dict(id="b0001",quote="明日")])])
        clean=grounded_delta(values,[block()])
        self.assertIsNone(clean["scene"])
        self.assertEqual(clean["facts"],[])
        self.assertEqual(clean["terms"],[])
        malformed=grounded_delta(dict(scene={},facts=None,terms="bad",open_threads=[None]),[block()])
        self.assertEqual(malformed["facts"],[])

    def test_uncertain_observations_do_not_become_grounded_facts(self):
        observation=delta(note="也許是兄妹")
        observation["scene"]["certainty"]="uncertain"
        commit(self.store,self.work,"one",observation=observation)
        selection,segment=self.store.selection(self.work["id"])
        self.assertEqual(self.store.context(selection,segment,"two",[block()])["dynamic"]["grounded_history"],[])

    def test_restart_retains_translations_and_does_not_export_keys_or_images(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"memory.sqlite3"
            first=WorkMemory(path)
            work=first.create("持久化漫畫")
            commit(first,work,"one")
            first.close()
            second=WorkMemory(path)
            try:
                selection,segment=second.selection(work["id"])
                self.assertEqual(second.saved_translation(selection,segment,"one","standard","gemini-test")[0]["translation"],"明天見。")
                self.assertIsNone(second.saved_translation(selection,segment,"one","standard","other-model"))
                exported=json.dumps(second.export(work["id"]))
                self.assertNotIn("api_key",exported)
                self.assertNotIn("image",exported)
            finally:
                second.close()

    def test_refresh_cannot_finish_after_work_is_deleted(self):
        token=self.store.start_research(self.work["id"])
        self.assertIsNone(self.store.start_research(self.work["id"]))
        self.store.delete(self.work["id"])
        self.assertFalse(self.store.finish_research(self.work["id"],token,{}, {}, [],"model"))

    def test_profile_refresh_invalidates_old_translation_without_altering_prior_snapshot(self):
        old=commit(self.store,self.work,"one")
        token=self.store.start_research(self.work["id"])
        self.store.finish_research(self.work["id"],token,dict(title_ja="更新作品名"),{},[],"model")
        selection,segment=self.store.selection(self.work["id"])
        self.assertIsNone(self.store.saved_translation(selection,segment,"one","standard","gemini-test"))
        context=self.store.context(selection,segment,"one",[block()])
        self.assertEqual(context["dynamic"],old["context"]["dynamic"])
        self.assertEqual(context["fixed"]["title_ja"],"更新作品名")

    def test_opening_another_connection_does_not_interrupt_live_research(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"memory.sqlite3"
            first=WorkMemory(path);second=None
            try:
                work=first.create("漫畫");first.start_research(work["id"])
                second=WorkMemory(path)
                self.assertEqual(second.get(work["id"])["status"],"building")
                second.recover_interrupted_research()
                self.assertEqual(first.get(work["id"])["status"],"interrupted")
            finally:
                if second:second.close()
                first.close()


class ResearchTests(unittest.TestCase):
    def test_profile_only_keeps_known_source_references(self):
        value=dict(matched=True,title_ja="漫画",identity_source_ids=["s1"],synopsis=dict(text="設定",source_ids=["invented"]),
                   setting=[],glossary=[dict(ja="名",zh_tw="名字",source_ids=["invented"])])
        result=validate_profile(value,[dict(id="s1",url="https://example.org/",title="source")])
        self.assertEqual(result["glossary"],[])
        self.assertEqual(result["synopsis"]["text"],"")
        value["matched"]=False
        with self.assertRaises(ValueError):validate_profile(value,[])

    @patch("comic_pipeline.gemini.sdk_request")
    def test_search_enables_grounding_and_retains_source_metadata(self,request):
        request.return_value.status_code=200
        request.return_value.json.return_value=dict(candidates=[dict(finishReason="STOP",content=dict(parts=[dict(text="研究資料")]),
            groundingMetadata=dict(groundingChunks=[dict(web=dict(uri="https://example.org/wiki",title="Wiki"))],webSearchQueries=["漫画 人物"]))])
        result,_=GeminiClient(key="private-test-key").search("search")
        self.assertEqual(request.call_args.kwargs["json"]["tools"],[dict(google_search={})])
        self.assertEqual(result["sources"][0]["id"],"s1")
        self.assertNotIn("private-test-key",json.dumps(result))
        request.return_value.json.return_value["candidates"][0]["groundingMetadata"]={}
        with self.assertRaises(GeminiError):GeminiClient(key="private-test-key").search("search")

    def test_translation_includes_both_context_layers_and_validates_update(self):
        client=Mock(model="gemini-test")
        client.generate_json.return_value=(dict(translations=[dict(id="b0001",text="明天見。")],memory_update=dict(
            scene=dict(text="約好明天見面",certainty="explicit",evidence=[dict(id="b0001",quote="明日会おう。")]))),{})
        translator=GeminiTranslator(client)
        translator.set_memory_context(dict(fixed=dict(title_ja="作品名",user_instructions="保留敬稱"),dynamic=dict(recent_pages=[])))
        self.assertEqual(translator.translate([block()])["b0001"],"明天見。")
        prompt,_,schema=client.generate_json.call_args.args
        self.assertIn("FIXED WORK DOSSIER",prompt)
        self.assertIn("PRIOR READING MEMORY",prompt)
        self.assertIn("USER TRANSLATION PREFERENCES",prompt)
        self.assertIn("保留敬稱",prompt)
        self.assertNotIn("user_instructions",prompt.split("FIXED WORK DOSSIER:")[1].split("PRIOR READING MEMORY")[0])
        self.assertEqual(translator.memory_context["fixed"]["user_instructions"],"保留敬稱")
        self.assertIn("memory_update",schema["required"])
        self.assertEqual(translator.memory_delta["scene"]["evidence"][0]["id"],"b0001")


class MemoryIntegrationTests(unittest.TestCase):
    def test_success_commits_once_and_next_page_receives_memory(self):
        contexts=[]
        class Translator:
            requests=[]
            memory_delta=delta()
            def set_memory_context(self,value):contexts.append(value)
            def translate(self,blocks):return {b["id"]:"明天見。" for b in blocks}
        processor=ReaderProcessor(engine=FakeEngine(),translator_factory=lambda *_:Translator())
        service=ReaderService(processor)
        self.addCleanup(service.close)
        service.configure(dict(key="private-test-key"))
        work=service.memory.create("測試漫畫")
        def run(color,**extra):
            from comic_pipeline.reader import png_bytes
            import base64
            data=dict(payload(),image=base64.b64encode(png_bytes(Image.new("RGB",(180,240),color))).decode(),
                      translator="gemini",work_id=work["id"],**extra)
            job=service.submit(data)
            return finished(service,job["id"]),data
        first,data=run("white")
        self.assertEqual(first["state"],"done",first)
        self.assertTrue(first["metrics"]["memory"]["saved"])
        self.assertEqual(finished(service,service.submit(data)["id"])["state"],"done")
        self.assertEqual(service.memory.get(work["id"])["pages"],1)
        second,_=run("#f8f8f8")
        self.assertEqual(second["state"],"done",second)
        self.assertEqual(len(contexts[-1]["dynamic"]["recent_pages"]),1)
        third,_=run("white",force_translation=True)
        self.assertEqual(third["state"],"done",third)
        self.assertEqual(contexts[-1]["dynamic"]["recent_pages"],[])
        self.assertEqual(service.memory.get(work["id"])["pages"],2)
        latest=service.submit(data)
        self.assertNotEqual(latest["id"],first["id"])
        self.assertEqual(finished(service,latest["id"])["state"],"done")
        self.assertEqual(len(contexts),3)

    def test_cancelled_page_does_not_enter_durable_memory(self):
        entered,release=threading.Event(),threading.Event()
        store=WorkMemory(":memory:")
        work=store.create("測試漫畫")
        class Processor:
            def process(self,image,mode,provider,settings,progress):
                entered.set();release.wait(3)
                selection,segment=store.selection(work["id"])
                return dict(image=b"unused",metrics=dict(memory={}),_memory_commit=dict(selection=selection,segment=segment,page="one",
                    context=store.context(selection,segment,"one",[block()]),blocks=[block()],delta=delta(),mode=mode,model="test"))
        service=ReaderService(Processor(),memory=store)
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        job=service.submit(payload())
        self.assertTrue(entered.wait(1));service.cancel(job["id"]);release.set()
        self.assertEqual(finished(service,job["id"])["state"],"canceled")
        self.assertEqual(store.get(work["id"])["pages"],0)


if __name__=="__main__":unittest.main()
