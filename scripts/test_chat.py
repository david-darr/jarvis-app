"""Isolated backend regression suite. Never imports the running app or data.
Run: .venv/Scripts/python.exe scripts/test_chat.py
"""
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock
import asyncio

fixture = tempfile.TemporaryDirectory(prefix="jarvis-chat-test-")
os.environ["JARVIS_DATA_DIR"] = fixture.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import attachments, chat_artifacts, image_gen, middleware
from core.session_manager import session_manager, SessionManager
from core.codex_brain import CodexBrain
from core.brain import Brain
from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage
from claude_agent_sdk.types import StreamEvent
from routes import chat_routes, session_routes
from services import chat_service

app = FastAPI()
app.add_middleware(middleware.SecurityHeadersMiddleware)
app.include_router(chat_routes.router)
app.include_router(session_routes.router)
client = TestClient(app)
ENDPOINTS = {
    "claude": {"id": "claude", "kind": "claude_cli", "model": "endpoint-model"},
    "codex": {"id": "codex", "kind": "codex_cli", "model": "endpoint-model"},
    "local": {"id": "local", "kind": "local", "model": "local-model"},
}

def _efforts(*names):
    return [{"effort": n, "description": ""} for n in names]

# Deterministic stand-in for core/model_catalog.py's real output. "ultra" is
# advertised by one codex model and not the other on purpose: that asymmetry
# is what proves effort validation is per-model rather than per-provider.
FAKE_CATALOG = {
    "codex_cli": [
        {"id": "fake-astra", "display_name": "Fake Astra", "description": "", "alias": None,
         "default_effort": "low", "supported_efforts": _efforts("low", "medium", "high", "ultra"),
         "context_window": 100000, "effective_context_percent": 90, "source": "cli_cache", "estimated": False},
        {"id": "endpoint-model", "display_name": "Endpoint Model", "description": "", "alias": None,
         "default_effort": "medium", "supported_efforts": _efforts("low", "medium", "high"),
         "context_window": 50000, "effective_context_percent": None, "source": "cli_cache", "estimated": False},
    ],
    "claude_cli": [
        {"id": "endpoint-model", "display_name": "Endpoint Model", "description": "", "alias": None,
         "default_effort": None, "supported_efforts": _efforts("low", "high"),
         "context_window": 200000, "effective_context_percent": None, "source": "curated", "estimated": True},
    ],
}


def _make_text_pdf(path: Path, pages: int = 1) -> None:
    """Minimal hand-built PDF with a real per-page text content stream (same
    technique as scripts/chat-smoke.cjs's samplePDF()) — a real text layer,
    not a scan."""
    objects, page_nums = [], []
    obj_num = 3
    font_num = obj_num
    objects.append((font_num, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    obj_num += 1
    for i in range(pages):
        stream = f"BT /F1 24 Tf 50 700 Td (This is real, extractable page {i + 1} body text, well over the scanned-page character threshold.) Tj ET"
        content_num = obj_num
        objects.append((content_num, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
        obj_num += 1
        page_num = obj_num
        objects.append((page_num, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>"))
        page_nums.append(page_num)
        obj_num += 1
    all_objects = [(1, "<< /Type /Catalog /Pages 2 0 R >>"),
                   (2, f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_nums)}] /Count {pages} >>")] + objects
    all_objects.sort(key=lambda o: o[0])
    out, offsets = "%PDF-1.4\n", [0]
    for num, body in all_objects:
        offsets.append(len(out.encode("latin-1")))
        out += f"{num} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(all_objects) + 1}\n0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(all_objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    path.write_bytes(out.encode("latin-1"))


def _make_scanned_pdf(path: Path, pages: int = 1) -> None:
    """A PDF with real pages but zero text objects — pypdfium2's own
    PdfDocument, since fabricating that by hand isn't worth it; this is
    exactly what attachments._split_scanned_pdf's char-count heuristic is
    meant to catch."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(400, 500)
    pdf.save(str(path))
    pdf.close()


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.session = session_manager.create_session()
        self.sid = self.session["id"]
        self.root = Path(fixture.name) / self.sid
        self.root.mkdir()
        session_manager.set_workspace(self.sid, str(self.root))
        self.endpoint_patch = patch("core.model_endpoints.get_endpoint", side_effect=lambda key: ENDPOINTS.get(key))
        self.endpoint_patch.start()
        chat_service._busy.clear()
        chat_service._brains.clear()

    def tearDown(self):
        self.endpoint_patch.stop()

    def select(self, endpoint="claude", override=None, effort=None):
        return client.post(f"/api/sessions/{self.sid}/model", json={"model_endpoint_id": endpoint, "model_override": override, "effort": effort})

    def test_variant_persistence_and_no_global_mutation(self):
        self.assertEqual(self.select(override="exact-model-1").status_code, 200)
        self.assertEqual(SessionManager().get_session(self.sid)["model_override"], "exact-model-1")
        self.assertEqual(ENDPOINTS["claude"]["model"], "endpoint-model")
        self.assertIsNone(session_manager.create_session().get("model_override"))
        self.assertEqual(self.select(override="").json()["model_override"], "")
        self.assertIsNone(self.select(override=None).json()["model_override"])

    def test_cli_only_and_validation(self):
        for endpoint, override in [("local", "x"), ("local", ""), (None, "x"), ("missing", None), ("claude", "-m injected"), ("claude", "x" * 161)]:
            self.assertEqual(self.select(endpoint, override).status_code, 400)
        self.assertEqual(self.select("local").status_code, 200)
        self.assertEqual(client.post('/api/sessions/missing/model', json={}).status_code, 404)

    def test_busy_model_and_turn_are_rejected(self):
        chat_service._busy.add(self.sid)
        self.assertEqual(self.select().status_code, 409)
        self.assertEqual(client.post('/api/chat/stream', json={"session_id": self.sid, "message": "Hi"}).status_code, 409)
        self.assertEqual(session_manager.get_session(self.sid)["messages"], [])
        self.assertEqual(client.delete(f'/api/sessions/{self.sid}').status_code, 409)
        self.assertEqual(client.post(f'/api/sessions/{self.sid}/project', json={}).status_code, 409)

    def test_claude_text_deltas_are_not_duplicated(self):
        async def run():
            event = lambda payload: StreamEvent(uuid='test', session_id='sdk-session', event=payload)
            events = [event({'type': 'message_start'}),
                      event({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'not visible'}}),
                      event({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'Hello '}}),
                      event({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'world'}}),
                      AssistantMessage(content=[TextBlock(text='Hello world')], model='test'),
                      event({'type': 'message_start'}),
                      AssistantMessage(content=[TextBlock(text='Fallback block')], model='test'),
                      ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id='sdk-session')]
            class FakeSDK:
                query = AsyncMock()
                async def receive_response(self):
                    for item in events: yield item
            brain = Brain(vault_dir=str(self.root)); brain._client = FakeSDK()
            chunks = [chunk async for chunk in brain.run_turn_stream('test')]
            self.assertEqual(chunks, ['Hello ', 'world', '\n\n', 'Fallback block'])
        asyncio.run(run())

    def test_codex_reports_failure_not_success_text(self):
        async def run():
            class FakeProc:
                returncode = 0
                def __init__(self, data):
                    self.stdout = asyncio.StreamReader(); self.stdout.feed_data(data); self.stdout.feed_eof()
                async def wait(self): return 0
            brain = CodexBrain(vault_dir=str(self.root))
            stderr = asyncio.create_task(asyncio.sleep(0, result=b''))
            with self.assertRaises(RuntimeError):
                _ = [chunk async for chunk in brain._consume_process(FakeProc(b'{"type":"turn.failed","error":{"message":"bad model"}}\n'), stderr, True, 'test')]
        asyncio.run(run())

    def test_cli_dispatch_and_thread_preservation(self):
        async def run():
            for kind, cls in [("claude", "Brain"), ("codex", "CodexBrain")]:
                session_manager.set_model_endpoint(self.sid, kind, "exact-model-2")
                with patch.object(chat_service, cls) as factory:
                    factory.return_value.connect = AsyncMock()
                    factory.return_value.disconnect = AsyncMock()
                    await chat_service._get_brain(self.sid, ENDPOINTS[kind])
                    self.assertEqual(factory.call_args.kwargs["model"], "exact-model-2")
                    await chat_service.close_session_brain(self.sid)
            session_manager.set_codex_thread_id(self.sid, "thread-1")
            session_manager.set_model_endpoint(self.sid, "codex", "exact-model-3")
            brain = CodexBrain(session_id=self.sid, model="exact-model-3")
            args = brain._build_args("codex")
            self.assertIn("resume", args)
            self.assertEqual(args[args.index('-m') + 1], "exact-model-3")
            session_manager.append_message(self.sid, "user", "Keep this history")
            session_manager.set_model_endpoint(self.sid, "claude", "exact-model-4")
            self.assertIsNone(session_manager.get_session(self.sid)["codex_thread_id"])
            self.assertEqual(session_manager.get_session(self.sid)["messages"][0]["content"], "Keep this history")
        asyncio.run(run())

    # -- reasoning effort + context indicator (David's ask 2026-09-15) -----
    # Every test below patches model_catalog.list_models. The real catalog
    # reads the machine's own Codex cache, which would make these results
    # depend on whose laptop they run on and which models that CLI happens
    # to advertise today — exactly the kind of environment coupling this
    # suite's "never imports the running app or data" rule exists to avoid.
    def test_effort_validated_per_model_not_per_provider(self):
        with patch("core.model_catalog.list_models", side_effect=lambda kind: FAKE_CATALOG.get(kind, [])):
            # Advertised by this model — accepted and persisted.
            self.assertEqual(self.select("codex", "fake-astra", "ultra").status_code, 200)
            self.assertEqual(session_manager.get_session(self.sid)["model_effort"], "ultra")
            # The real point of per-model validation: "ultra" is genuinely
            # valid for one model and genuinely invalid for another on the
            # SAME provider, so a provider-wide effort list would wrongly
            # accept this.
            self.assertEqual(self.select("codex", "endpoint-model", "ultra").status_code, 400)
            self.assertEqual(self.select("codex", "endpoint-model", "high").status_code, 200)
            # Unknown value, no endpoint, and a non-CLI endpoint all refuse.
            self.assertEqual(self.select("codex", "fake-astra", "bogus").status_code, 400)
            self.assertEqual(self.select(None, None, "high").status_code, 400)
            self.assertEqual(self.select("local", None, "high").status_code, 400)
            # None stays the default and is not coerced into a value.
            self.assertEqual(self.select("codex", "fake-astra").status_code, 200)
            self.assertIsNone(session_manager.get_session(self.sid)["model_effort"])
            # A custom ID has no advertised list, so the provider-wide union
            # is the ceiling: a real level passes, an invented one does not.
            self.assertEqual(self.select("codex", "some-unlisted-model", "ultra").status_code, 200)
            self.assertEqual(self.select("codex", "some-unlisted-model", "nonsense").status_code, 400)

    def test_effort_reaches_provider_invocation(self):
        async def run():
            with patch("core.model_catalog.list_models", side_effect=lambda kind: FAKE_CATALOG.get(kind, [])):
                self.assertEqual(self.select("codex", "fake-astra", "high").status_code, 200)
            for kind, cls in [("codex", "CodexBrain"), ("claude", "Brain")]:
                with patch.object(chat_service, cls) as factory:
                    factory.return_value.connect = AsyncMock()
                    factory.return_value.disconnect = AsyncMock()
                    await chat_service._get_brain(self.sid, ENDPOINTS[kind])
                    self.assertEqual(factory.call_args.kwargs["effort"], "high")
                    await chat_service.close_session_brain(self.sid)
            # Codex has no --effort flag; the config override is the route,
            # and it must sit ahead of the `resume` subcommand (verified live
            # against codex-cli 0.154.0 — unlike -C/-s, which resume rejects)
            # so a resumed thread picks the new level up too.
            brain = CodexBrain(vault_dir=str(self.root), effort="high")
            fresh = brain._build_args("codex")
            self.assertEqual(fresh[fresh.index("-c") + 1], 'model_reasoning_effort="high"')
            brain.thread_id = "t-1"
            resumed = brain._build_args("codex")
            self.assertLess(resumed.index("-c"), resumed.index("resume"))
            # Unset effort must produce byte-identical argv to before the
            # feature existed — an unused option changes nothing.
            self.assertNotIn("-c", CodexBrain(vault_dir=str(self.root))._build_args("codex"))
        asyncio.run(run())

    def test_context_state_is_occupancy_not_cumulative_spend(self):
        from core import token_usage
        # Codex reports a full input figure with the cached part as a SUBSET;
        # Claude reports only the uncached remainder plus separate cache
        # fields. Getting these backwards is the difference between a
        # correct meter and one that reads near-empty on every cache hit.
        self.assertEqual(token_usage.extract_context_tokens(
            {"total_tokens": 53000, "input_tokens": 50000, "cached_input_tokens": 40000, "output_tokens": 3000}), 50000)
        self.assertEqual(token_usage.extract_context_tokens(
            {"input_tokens": 1200, "output_tokens": 800, "cache_read_input_tokens": 45000, "cache_creation_input_tokens": 3000}), 49200)
        self.assertEqual(token_usage.extract_context_tokens({"prompt_tokens": 7000, "total_tokens": 7200}), 7000)
        for empty in ({}, None, {"output_tokens": 5}):
            self.assertIsNone(token_usage.extract_context_tokens(empty))
        # The lifetime counter must not change meaning now that codex_brain
        # reports extra keys: an explicit total_tokens still wins outright.
        self.assertEqual(token_usage._extract_total_tokens(
            {"total_tokens": 53000, "input_tokens": 50000, "cached_input_tokens": 40000, "output_tokens": 3000}), 53000)

    def test_context_route_reports_honestly(self):
        class FakeBrain:
            model = "fake-astra"
            last_usage = {"total_tokens": 33000, "input_tokens": 30000, "cached_input_tokens": 10000, "output_tokens": 3000}
            async def run_turn_stream(self, text):
                yield "ok"
        with patch("core.model_catalog.list_models", side_effect=lambda kind: FAKE_CATALOG.get(kind, [])):
            self.assertEqual(self.select("codex", "fake-astra").status_code, 200)
            # Nothing measured yet is "unavailable", not a zero or a guess.
            self.assertEqual(client.get(f'/api/sessions/{self.sid}/context').json(), {"available": False})
            with patch.object(chat_service, '_get_brain', AsyncMock(return_value=(FakeBrain(), False))):
                self.assertEqual(client.post('/api/chat/stream', json={"session_id": self.sid, "message": "Hi"}).status_code, 200)
            state = client.get(f'/api/sessions/{self.sid}/context').json()
            self.assertTrue(state["available"])
            self.assertEqual(state["used_tokens"], 30000)
            # 100000 window * 90% effective = 90000 usable; 30000/90000 = 33.3%.
            self.assertEqual(state["capacity_tokens"], 90000)
            self.assertEqual(state["percent"], 33.3)
            self.assertFalse(state["estimated_capacity"])
            # Switching model invalidates the reading — a capacity from the
            # old model would render a wrong percentage until the next turn.
            self.assertEqual(self.select("codex", "endpoint-model").status_code, 200)
            self.assertEqual(client.get(f'/api/sessions/{self.sid}/context').json(), {"available": False})
        self.assertEqual(client.get('/api/sessions/missing/context').status_code, 404)

    def test_context_without_known_capacity_reports_tokens_only(self):
        from core import token_usage
        state = token_usage.build_context_state({"prompt_tokens": 4200}, None, "unlisted")
        self.assertEqual(state["used_tokens"], 4200)
        # A real measurement with an unknown ceiling shows the count and NO
        # percentage, rather than inventing a denominator to divide by.
        self.assertIsNone(state["percent"])
        self.assertIsNone(state["capacity_tokens"])
        self.assertIsNone(token_usage.build_context_state(None, None, "unlisted"))
        # A curated capacity is reported as estimated so the UI can say so.
        estimated = token_usage.build_context_state(
            {"prompt_tokens": 100}, {"effective": 1000, "estimated": True, "source": "curated"}, "m")
        self.assertTrue(estimated["estimated_capacity"])
        # Occupancy can never exceed the stated ceiling once clamped.
        self.assertEqual(token_usage.build_context_state(
            {"prompt_tokens": 5000}, {"effective": 1000, "estimated": False}, "m")["percent"], 100.0)

    # -- Office previews (David's ask 2026-09-15) --------------------------
    def test_office_extraction_round_trip(self):
        from core import office_preview
        import openpyxl, docx
        from pptx import Presentation

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Budget"
        sheet.append(["Item", "Qty"])
        sheet.append(["Widget", 3])
        book.create_sheet("Notes").append(["second sheet"])
        book.save(self.root / "b.xlsx")
        data = office_preview.extract(self.root / "b.xlsx", ".xlsx")
        self.assertEqual([s["name"] for s in data["sheets"]], ["Budget", "Notes"])
        # Rows must be bounded by the sheet's real width, not padded out to
        # the column cap — a two-column sheet rendered as fifty columns of
        # blanks was a real bug caught here.
        self.assertEqual(data["sheets"][0]["rows"], [["Item", "Qty"], ["Widget", "3"]])

        document = docx.Document()
        document.add_heading("Title", level=1)
        document.add_paragraph("Body text.")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "a"
        table.cell(0, 1).text = "b"
        document.add_paragraph("After the table.")
        document.save(self.root / "d.docx")
        blocks = office_preview.extract(self.root / "d.docx", ".docx")["blocks"]
        # Reading order matters: iterating paragraphs and tables separately
        # would move every table to the end of the document.
        self.assertEqual([b["type"] for b in blocks], ["heading", "paragraph", "table", "paragraph"])
        self.assertEqual(blocks[2]["rows"], [["a", "b"]])

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = "Slide one"
        slide.placeholders[1].text = "First point\nSecond point"
        slide.notes_slide.notes_text_frame.text = "Speaker notes"
        from pptx.util import Inches
        grid = slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(1)).table
        grid.cell(0, 0).text, grid.cell(0, 1).text = "h1", "h2"
        grid.cell(1, 0).text, grid.cell(1, 1).text = "v1", "v2"
        deck.save(self.root / "p.pptx")
        parsed = office_preview.extract(self.root / "p.pptx", ".pptx")
        self.assertEqual(parsed["slides"][0]["title"], "Slide one")
        self.assertEqual(parsed["slides"][0]["body"], ["First point", "Second point"])
        self.assertEqual(parsed["slides"][0]["notes"], "Speaker notes")
        # A table on a slide must survive: python-pptx's collections are not
        # sliceable, and the per-shape guard hid that for a whole release.
        self.assertEqual(parsed["slides"][0]["tables"], [[["h1", "h2"], ["v1", "v2"]]])
        # Stated in the payload so the UI can say so rather than implying the
        # preview looks like the real slide.
        self.assertFalse(parsed["layout_fidelity"])

        # Quoted separators and embedded newlines are exactly why this is
        # parsed server-side instead of split on commas in the browser.
        (self.root / "t.csv").write_text('name,note\n"Smith, John","one\ntwo"\n', encoding="utf-8")
        rows = office_preview.extract(self.root / "t.csv", ".csv")["rows"]
        self.assertEqual(rows[1][0], "Smith, John")
        self.assertIn("two", rows[1][1])

    def test_office_preview_refuses_unsafe_or_unreadable_files(self):
        import zipfile
        from core import office_preview

        # Tiny on disk, enormous when expanded. Rejected before any parser
        # is handed the file, not after it has allocated the memory.
        bomb = self.root / "bomb.xlsx"
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", b"0" * (40 * 1024 * 1024))
        self.assertLess(bomb.stat().st_size, 1024 * 1024)
        with self.assertRaises(office_preview.PreviewUnavailable):
            office_preview.extract(bomb, ".xlsx")

        many = self.root / "many.xlsx"
        with zipfile.ZipFile(many, "w") as zf:
            for i in range(office_preview.MAX_ENTRIES + 5):
                zf.writestr(f"part{i}.xml", b"x")
        with self.assertRaises(office_preview.PreviewUnavailable):
            office_preview.extract(many, ".xlsx")

        junk = self.root / "junk.docx"
        junk.write_bytes(b"not an office file at all")
        with self.assertRaises(office_preview.PreviewUnavailable):
            office_preview.extract(junk, ".docx")

        # Macro-enabled formats are not previewable at all, so nothing ever
        # opens them — a clearer boundary than relying on these readers
        # having no way to execute a macro.
        for macro_ext in (".xlsm", ".docm", ".pptm"):
            self.assertNotIn(macro_ext, office_preview.PREVIEWABLE)
            with self.assertRaises(office_preview.PreviewUnavailable):
                office_preview.extract(junk, macro_ext)

    def test_office_route_is_session_scoped_and_reports_failure(self):
        import openpyxl
        book = openpyxl.Workbook()
        book.active.append(["Header"])
        book.save(self.root / "real.xlsx")
        url = self.publish("real.xlsx", (self.root / "real.xlsx").read_bytes())
        params = {"session_id": self.sid, "url": url}
        self.assertEqual(client.get('/api/chat/artifacts', params=params).json()["kind"], "office")
        self.assertEqual(client.get('/api/chat/artifacts/office', params=params).json()["sheets"][0]["rows"], [["Header"]])
        # Raw bytes are only ever a download, never served inline for the
        # browser to do something of its own with.
        inline = client.get('/api/chat/artifacts/content', params=params)
        self.assertIn('attachment', inline.headers['content-disposition'])
        # Same ownership check as every other preview route: a file this
        # session doesn't reference is not readable through it.
        other = session_manager.create_session()["id"]
        self.assertEqual(client.get('/api/chat/artifacts/office', params={"session_id": other, "url": url}).status_code, 404)
        # A file with the right extension but unreadable contents fails with
        # a message meant for the user, not a 500.
        broken = self.publish("broken.xlsx", b"definitely not a workbook")
        res = client.get('/api/chat/artifacts/office', params={"session_id": self.sid, "url": broken})
        self.assertEqual(res.status_code, 422)
        self.assertIn("Office document", res.json()["detail"])
        # A non-Office artifact is refused by this route rather than parsed.
        md = self.publish("notes.md")
        self.assertEqual(client.get('/api/chat/artifacts/office', params={"session_id": self.sid, "url": md}).status_code, 400)

    def test_stream_failure_persists_partial(self):
        class FailingBrain:
            async def run_turn_stream(self, text):
                yield "Partial **answer**"
                raise RuntimeError("private provider error")
        self.select()
        with patch.object(chat_service, '_get_brain', AsyncMock(return_value=(FailingBrain(), False))), patch.object(chat_service, 'close_session_brain', AsyncMock()):
            res = client.post('/api/chat/stream', json={"session_id": self.sid, "message": "Hi"})
        self.assertIn('"error":', res.text)
        self.assertNotIn('"done": true', res.text)
        self.assertNotIn('private provider error', res.text)
        last = session_manager.get_session(self.sid)["messages"][-1]
        self.assertEqual(last["content"], "Partial **answer**")
        self.assertEqual(last["status"], "interrupted")
        self.assertFalse(chat_service.is_busy(self.sid))

    def publish(self, name, content=b"hello"):
        path = self.root / name
        path.write_bytes(content)
        res = client.post('/api/chat/artifacts', json={"session_id": self.sid, "path": str(path)})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["url"]

    def test_file_delivery_and_previews(self):
        # report.docx became 'office' rather than 'download' with the Office
        # preview work (2026-09-15); report.docm stays 'download' because
        # macro-enabled formats are deliberately not offered for preview.
        for name, kind in [('My report.md', 'markdown'), ('index.html', 'html'), ('script.py', 'text'), ('file.pdf', 'pdf'), ('picture.png', 'image'), ('report.docx', 'office'), ('report.docm', 'download')]:
            url = self.publish(name)
            params = {"session_id": self.sid, "url": url}
            res = client.get('/api/chat/artifacts', params=params)
            self.assertEqual(res.json()["kind"], kind)
            self.assertEqual(res.json()["filename"], name)
            self.assertIn(url, SessionManager().get_session(self.sid)["artifact_urls"])
            download = client.get('/api/chat/artifacts/content', params={**params, "download": True})
            self.assertEqual(download.content, b'hello')
            self.assertIn('attachment', download.headers['content-disposition'])
            preview = client.get('/api/chat/artifacts/content', params=params)
            self.assertIn("default-src 'none'", preview.headers['content-security-policy'])
            self.assertEqual(preview.headers['x-content-type-options'], 'nosniff')
            if kind == 'html': self.assertTrue(preview.headers['content-type'].startswith('text/plain'))

    def test_existing_session_link_and_missing_file(self):
        path = self.root / 'legacy.txt'; path.write_text('legacy')
        url = image_gen.register_generated_file(str(path))["url"]
        session_manager.append_message(self.sid, 'assistant', f'[Legacy]({url})')
        self.assertEqual(chat_artifacts.resolve(self.sid, url)[1]['kind'], 'text')
        self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": url + 'missing'}).status_code, 404)

    def test_artifact_security_and_size_limits(self):
        url = self.publish('safe.txt')
        other = session_manager.create_session()['id']
        self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": other, "url": url}).status_code, 404)
        for source in ['../outside.txt', '.env', 'C:\\Windows\\win.ini']:
            self.assertEqual(client.post('/api/chat/artifacts', json={"session_id": self.sid, "path": source}).status_code, 400)
        for invalid in ['/generated-files/%2e%2e%2fsecret', '/generated-files/C%3Asecret', 'https://example.test/secret']:
            session_manager.append_message(self.sid, 'assistant', f'[bad]({invalid})')
            self.assertIn(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": invalid}).status_code, [400, 404])
        url = self.publish('large.txt', b'x' * (chat_artifacts.MAX_PREVIEW_BYTES + 1))
        self.assertEqual(chat_artifacts.resolve(self.sid, url)[1]['kind'], 'download')
        with patch('core.middleware.auth_enabled', return_value=True):
            self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": url}).status_code, 401)

    def test_scanned_pdf_gets_split_text_pdf_does_not(self):
        # Regression for 2026-09-13: a scanned course PDF blew past Claude
        # Code's per-response transport buffer because its pages are
        # already full-resolution images by construction — see
        # attachments.SCANNED_AVG_CHARS_PER_PAGE's docstring.
        text_id = attachments.stage_file('syllabus.pdf', b'placeholder')['id']
        scan_id = attachments.stage_file('scan.pdf', b'placeholder')['id']
        # stage_file above just wrote a placeholder under data/attachments/ —
        # overwrite it in place with a real PDF before resolve_for_turn copies it.
        staged_text = Path(attachments._find_staged_path(text_id))
        staged_scan = Path(attachments._find_staged_path(scan_id))
        _make_text_pdf(staged_text, pages=2)
        _make_scanned_pdf(staged_scan, pages=3)

        names, warnings = attachments.resolve_for_turn([text_id, scan_id], self.sid, str(self.root))
        self.assertEqual(warnings, [])
        text_names = [n for n in names if n.endswith('syllabus.pdf')]
        page_names = [n for n in names if 'scan_page' in n]
        self.assertEqual(len(text_names), 1, names)
        self.assertEqual(len(page_names), 3, names)
        for n in page_names:
            self.assertTrue((self.root / n).is_file())
            self.assertTrue(n.endswith('.jpg'))
        # The scanned original was consumed by the split; the text PDF was not.
        self.assertTrue((self.root / text_names[0]).is_file())
        self.assertFalse(any(n.endswith('scan.pdf') for n in names))

    def test_scanned_pdf_truncates_with_warning_past_page_cap(self):
        big_id = attachments.stage_file('bigscan.pdf', b'placeholder')['id']
        staged = Path(attachments._find_staged_path(big_id))
        _make_scanned_pdf(staged, pages=attachments.MAX_SPLIT_PAGES + 5)

        names, warnings = attachments.resolve_for_turn([big_id], self.sid, str(self.root))
        self.assertEqual(len(names), attachments.MAX_SPLIT_PAGES)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(attachments.MAX_SPLIT_PAGES + 5), warnings[0])

        full_text = chat_service._apply_attachments(self.sid, "here's the reading", [big_id])
        self.assertIn('only the first', full_text)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        fixture.cleanup()
