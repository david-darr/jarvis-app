"""Unit D: Forge-only stream activity and the reused admin run history API.

No Git, model process, network, or application data is used.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
environment = tempfile.TemporaryDirectory(prefix='kairos-forge-transcript-')
os.environ['JARVIS_DATA_DIR'] = environment.name

import httpx
from fastapi import FastAPI
from core import file_checkpoints, middleware, runs, session_manager_store as store
from core.session_manager import session_manager
from routes import chat_routes, run_routes
from services import chat_service


def tearDownModule():
    store.close()
    environment.cleanup()


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = session_manager.create_session('Transcript fixture')
        self.id = self.session['id']
        self.context = runs.RunContext('chat', session_id=self.id)
        self.addCleanup(store.delete_all_sessions)

    async def stream(self, forge=False, events=None):
        if forge:
            session_manager.set_forge(self.id, {'mode': 'build', 'project_id': 'fixture'})
        sequence = events or [runs.text('Before'), runs.tool_started('read', 'Read', {'file_path': 'src/garden.js'}),
                              runs.tool_finished('read', True, '5 lines read'),
                              runs.tool_started('cmd', 'shell', {'command': 'node --check src/garden.js'}),
                              runs.tool_finished('cmd', True, 'Syntax check passed'), runs.text('After'), runs.result(True)]

        class Brain:
            async def events(self, prompt):
                for event in sequence:
                    yield event

        tally = runs.Tally()
        runs.enter(self.context)
        try:
            items = [item async for item in chat_service._stream_with_permission_prompts(self.id, Brain(), 'fixture', tally)]
        finally:
            runs.leave(self.context)
            runs.CURRENT.set(None)
        return items, tally

    async def test_forge_live_steps_have_targets_outcomes_and_durations(self):
        items, tally = await self.stream(True)
        steps = [item['tool_step'] for item in items if isinstance(item, dict)]
        self.assertEqual([s['phase'] for s in steps], ['started', 'finished', 'started', 'finished'])
        self.assertEqual([s['summary'] for s in steps], ['src/garden.js'] * 2 + ['node --check src/garden.js'] * 2)
        self.assertEqual(steps[-1]['output'], 'Syntax check passed')
        self.assertTrue(steps[-1]['ok'])
        self.assertGreaterEqual(steps[-1]['seconds'], 0)
        self.assertEqual(items[1]['run_id'], self.context.run_id)
        self.assertEqual([s['at'] for s in steps], sorted(s['at'] for s in steps))
        self.assertEqual(len(tally.timeline), 4)

    async def test_non_forge_stream_is_exactly_the_old_text_projection(self):
        items, _ = await self.stream()
        self.assertEqual(items, ['Before', 'After'])

    async def test_clipping_never_projects_unkept_input_or_output(self):
        argument = {'command': 'HEAD' + 'x' * 4000 + 'TAIL'}
        output = 'OUT' + 'y' * 4000 + 'END'
        items, tally = await self.stream(True, [runs.tool_started('x', 'shell', argument),
                                               runs.tool_finished('x', False, output), runs.result(True)])
        self.assertEqual(items[0]['tool_step']['summary'], tally.timeline[0]['detail'])
        self.assertEqual(items[1]['tool_step']['output'], tally.timeline[1]['detail'])
        self.assertLessEqual(len(items[1]['tool_step']['output']), runs.TIMELINE_CLIP + 3)
        self.assertNotIn('TAIL', json.dumps(items))
        self.assertNotIn('END', json.dumps(items))
        self.assertFalse(items[1]['tool_step']['ok'])

    async def test_timeline_limit_also_bounds_live_activity(self):
        events = [event for i in range(runs.TIMELINE_LIMIT) for event in
                  (runs.tool_started(str(i), 'Read', {'path': 'fixture'}), runs.tool_finished(str(i), True, 'ok'))]
        items, tally = await self.stream(True, events + [runs.result(True)])
        self.assertEqual(len(items), runs.TIMELINE_LIMIT)
        self.assertEqual(tally.dropped, runs.TIMELINE_LIMIT)

    async def test_clipped_edit_arguments_still_have_a_path_summary(self):
        items, tally = await self.stream(True, [runs.tool_started('edit', 'Edit', {
            'file_path': 'src/garden.js', 'old_string': 'x' * 4000}), runs.tool_finished('edit', True, 'Edited')])
        with self.assertRaises(ValueError):
            json.loads(tally.timeline[0]['detail'])
        self.assertEqual(items[0]['tool_step']['summary'], 'src/garden.js')
        self.assertEqual(items[1]['tool_step']['summary'], 'src/garden.js')

    async def test_existing_run_routes_return_turns_and_steps_in_order(self):
        _, tally = await self.stream(True)
        runs.record(self.context, tally, 'finished')
        older = runs.RunContext('chat', session_id=self.id, started_at=self.context.started_at - 30)
        runs.record(older, runs.Tally(), 'stopped')
        app = FastAPI(); app.include_router(run_routes.router)
        app.dependency_overrides[middleware.require_admin] = lambda: 'admin'
        with patch.object(run_routes.helpers, 'batch_rows_for_run', return_value=[]):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                turns = (await client.get('/api/runs', params={'session_id': self.id})).json()
                self.assertEqual([r['id'] for r in turns], [self.context.run_id, older.run_id])
                response = await client.get('/api/runs/' + self.context.run_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['steps'], tally.timeline)
                self.assertEqual([s['kind'] for s in response.json()['steps']], ['tool_started', 'tool_finished'] * 2)

    async def test_history_admin_only(self):
        app = FastAPI(); app.include_router(run_routes.router)
        with patch.object(middleware, 'auth_enabled', return_value=False), \
             patch.object(middleware.auth_manager, 'is_admin', return_value=False):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                for path in ('/api/runs?session_id=' + self.id, '/api/runs/fixture'):
                    self.assertEqual((await client.get(path)).status_code, 403)

    async def test_http_serialization_keeps_non_forge_bytes_unchanged(self):
        app = FastAPI(); app.include_router(chat_routes.router)
        app.dependency_overrides[middleware.require_user] = lambda: 'admin'

        async def stream_message(*args):
            items, _ = await self.stream(bool((session_manager.get_session(self.id) or {}).get('forge')))
            for item in items:
                yield item

        with patch.object(chat_service, 'stream_message', stream_message), \
             patch.object(chat_service, 'validate_image_attachments', return_value=[]), \
             patch.object(chat_routes.chat_references, 'resolve', return_value=''):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                ordinary = await client.post('/api/chat/stream', json={'session_id': self.id, 'message': 'fixture'})
                self.assertEqual(ordinary.content, b'data: {"chunk": "Before"}\n\ndata: {"chunk": "After"}\n\ndata: {"done": true}\n\n')
                session_manager.set_forge(self.id, {'mode': 'build'})
                forge = await client.post('/api/chat/stream', json={'session_id': self.id, 'message': 'fixture'})
                payloads = [json.loads(line[6:]) for line in forge.text.splitlines() if line.startswith('data: ')]
                self.assertEqual(sum('tool_step' in p for p in payloads), 4)


class CheckpointTotalsTests(unittest.TestCase):
    def test_turn_totals_are_not_clipped_with_the_preview(self):
        event = {'id': 'fixture', 'roots': [{'path': str(ROOT), 'changes': [
            {'path': 'fixture.txt', 'before': 'before', 'after': 'after'}]}]}
        bodies = {'before': b'old\n' * 2105, 'after': b'new\n' * 2107}
        with patch.object(file_checkpoints, '_load_event', return_value=event), \
             patch.object(file_checkpoints, '_repo', return_value=ROOT), \
             patch.object(file_checkpoints, '_current_blob_oid', return_value='after'), \
             patch.object(file_checkpoints, '_read_object', side_effect=lambda repo, oid: ('blob', bodies[oid])):
            change = file_checkpoints.get_event('fixture')['roots'][0]['changes'][0]
        self.assertEqual((change['added'], change['removed']), (2107, 2105))
        self.assertLessEqual(len(change['diff'].splitlines()), 2000)


if __name__ == '__main__':
    unittest.main()
