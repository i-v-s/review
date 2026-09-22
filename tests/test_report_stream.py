import asyncio
import json

import pytest

from review_service.app import SERVICE, create_app
from review_service.models import SourceEvent
from review_service.report_stream import preview
from test_llm import attach, fake_llm


def test_partial_json_never_completes_a_card():
    item = {'section': 'Тест', 'title': 'Кавычки " и скобки } ]', 'explanation': 'Строка\nЮникод 🙂',
            'fragment_ids': ['f'], 'source_ids': ['s'], 'rationale_kind': 'recorded',
            'dependencies': ['f'], 'reviewed': True}
    encoded = json.dumps(item, ensure_ascii=True)
    text = '{"summary":"Начало", "items":[' + encoded + '],"findings":[]}'
    closed = text.index(encoded) + len(encoded)
    for n in range(len(text) + 1):
        result = preview(text[:n], {'s'}, {'f'}, 'batch')
        assert bool(result['items']) == (n >= closed)
        if result['items']:
            assert result['items'][0]['explanation'] == item['explanation']
            assert result['items'][0]['reviewed'] is False


@pytest.mark.parametrize('fence', ['', '```json\n'])
def test_summary_streams_and_fields_can_arrive_in_any_order(fence):
    assert preview(fence + '{"summary":"Прив', set(), set(), 'p')['summary'] == 'Прив'
    text = json.dumps({'findings': [{'title': 'F', 'description': 'D', 'fragment_ids': ['f']}],
                       'items': [], 'summary': 'Итог'}, ensure_ascii=False)
    result = preview(fence + text + ('\n```' if fence else ''), set(), {'f'}, 'p')
    assert result['summary'] == 'Итог'
    assert result['findings'][0]['title'] == 'F'


@pytest.mark.parametrize('extra', [
    {'fragment_ids': ['invented']}, {'source_ids': ['invented']},
    {'dependencies': ['invented']}, {'rationale_kind': 'recorded'}, {'unknown_field': True},
])
def test_invalid_cards_are_never_previewed(extra):
    item = {'section': 'S', 'title': 'T', 'explanation': 'E', **extra}
    result = preview(json.dumps({'summary': 'S', 'items': [item]}), set(), {'f'}, 'p')
    assert result['items'] == []


async def next_preview(queue, predicate):
    async with asyncio.timeout(5):
        while True:
            event = await queue.get()
            if event['type'] == 'report_preview' and predicate(event['draft']):
                return event['draft']


async def test_preview_before_finish_and_frozen_evidence(client, service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    event = SourceEvent(id='d1', provider='decision', session_id='s', source_id='m', role='decision', text='Original')
    await service.store.event(event)
    control = fake_llm[1]
    control['before_finish'] = asyncio.Event()
    llm = await attach(service, fake_llm)
    queue = asyncio.Queue(maxsize=128)
    service.subscribers.add(queue)
    try:
        job = await service.start_job('report', service.generate)
        draft = await next_preview(queue, lambda d: bool(d['items']))
        assert draft['status'] == 'running'
        assert not await service.store.list('report')
        assert (await service.store.get('job', job['id']))['status'] == 'running'
        fetched = await (await client.get('/api/v1/report-drafts/' + job['id'])).json()
        assert fetched['summary'] == draft['summary']
        event.text = 'Changed'
        await service.store.event(event)
        sources = await (await client.get('/api/v1/sources', params={'draft_id': job['id']})).json()
        assert sources['events'][0]['text'] == 'Original'
        state = await (await client.get('/api/v1/state')).json()
        assert state['active_report_draft_id'] == job['id']
        assert state['report_drafts'][0]['id'] == job['id']
        control['before_finish'].set()
        await asyncio.wait_for(service.tasks[job['id']], 5)
        finished = await service.store.get('report_draft', job['id'])
        assert finished['status'] == 'completed'
        assert finished['revision'] > draft['revision']
        assert await service.store.get('report', finished['report_id'])
        assert not (await service.state())['report_drafts']
    finally:
        control['before_finish'].set()
        service.subscribers.discard(queue)
        await llm.close()


@pytest.mark.parametrize('failure', ['cancel', 'timeout', 'length', 'disconnect'])
async def test_failures_retain_partial_and_previous_reports(service, repo, fake_llm, failure):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    control = fake_llm[1]
    queue = asyncio.Queue(maxsize=128)
    service.subscribers.add(queue)
    try:
        previous = await service.generate('previous')
        if failure in ('cancel', 'timeout'):
            control['before_finish'] = asyncio.Event()
        if failure == 'timeout':
            service.config.llm_timeout_seconds = 0.8
        if failure in ('length', 'disconnect'):
            control['finish_reason'] = 'length' if failure == 'length' else None
        job = await service.start_job('report', service.generate)
        task = service.tasks[job['id']]
        await next_preview(queue, lambda d: d['id'] == job['id'] and bool(d['items']))
        if failure == 'cancel':
            task.cancel()
        await asyncio.wait_for(task, 5)
        draft = await service.store.get('report_draft', job['id'])
        assert draft['status'] == ('cancelled' if failure == 'cancel' else 'failed')
        assert draft['items'] and draft['summary']
        assert [r['id'] for r in await service.store.list('report')] == [previous['report_id']]
        assert len(control['requests']) == 2  # No automatic repeat of an interrupted stream.
    finally:
        if control['before_finish']:
            control['before_finish'].set()
        service.subscribers.discard(queue)
        await llm.close()


async def test_repair_replaces_current_attempt_without_duplicates(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    fake_llm[1].update(invalid=True, repair=True)
    llm = await attach(service, fake_llm)
    queue = asyncio.Queue(maxsize=128)
    service.subscribers.add(queue)
    try:
        result = await service.generate('repair')
        previews = []
        while not queue.empty():
            event = queue.get_nowait()
            if event['type'] == 'report_preview':
                previews.append(event['draft'])
        assert all(not d['items'] for d in previews if d.get('attempt') == 1)
        assert any(d.get('attempt') == 2 and d['items'] for d in previews)
        report = await service.store.get('report', result['report_id'])
        assert len([i for i in report['items'] if i['section'] != 'Без объяснения']) == 1
        assert [d['revision'] for d in previews] == sorted({d['revision'] for d in previews})
    finally:
        service.subscribers.discard(queue)
        await llm.close()


async def test_multiple_batches_preserve_completed_parts(service, fake_llm):
    service.config.max_context_chars = 220
    fragments = [{'id': f'f{n}', 'text': 'x' * 50} for n in range(3)]
    updates = []
    async def progress(_):
        pass
    async def on_preview(data):
        updates.append(data)
    llm = await attach(service, fake_llm)
    try:
        report, _ = await llm.report(fragments, [], progress, on_preview=on_preview)
        assert len(fake_llm[1]['requests']) == 3
        assert len(report.items) == 3
        assert {f for item in report.items for f in item.fragment_ids} == {'f0', 'f1', 'f2'}
        assert all(len(d['items']) >= d['batch_number'] - 1 for d in updates)
    finally:
        await llm.close()


async def test_restart_marks_orphan_draft_interrupted(client, service, repo, fake_llm, aiohttp_client):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    try:
        await service.generate('orphan')
        draft = await service.store.get('report_draft', 'orphan')
        draft.update(status='running', report_id='not-published')
        await service.store.put('report_draft', 'orphan', draft)
        await service.store.put('job', 'orphan', {'id': 'orphan', 'kind': 'report', 'status': 'running'})
    finally:
        await llm.close()
    config = service.config
    config.api_key = ''
    await client.close()
    restarted = await aiohttp_client(create_app(config), headers={'Authorization': 'Bearer test-secret'})
    restored = await (await restarted.get('/api/v1/report-drafts/orphan')).json()
    assert restored['status'] == 'interrupted'
    assert restored['items'] == draft['items']
    assert (await restarted.app[SERVICE].store.get('job', 'orphan'))['status'] == 'interrupted'


async def test_draft_routes_require_authentication(client):
    for path in ('/api/v1/report-drafts/missing', '/api/v1/sources?draft_id=missing'):
        assert (await client.get(path, headers={'Authorization': ''})).status == 401
        assert (await client.get(path)).status == 404


async def test_graceful_restart_retains_live_preview(client, service, repo, fake_llm, aiohttp_client):
    (repo / 'example.py').write_text('changed\n')
    control = fake_llm[1]
    control['before_finish'] = asyncio.Event()
    llm = await attach(service, fake_llm)
    queue = asyncio.Queue(maxsize=128)
    service.subscribers.add(queue)
    try:
        job = await service.start_job('report', service.generate)
        draft = await next_preview(queue, lambda d: bool(d['items']))
        config = service.config
        await client.close()
        await llm.close()
        config.api_key = ''
        restarted = await aiohttp_client(create_app(config), headers={'Authorization': 'Bearer test-secret'})
        restored = await (await restarted.get('/api/v1/report-drafts/' + job['id'])).json()
        assert restored['status'] == 'interrupted'
        assert restored['items'] == draft['items']
        assert (await restarted.app[SERVICE].store.get('job', job['id']))['status'] == 'interrupted'
    finally:
        control['before_finish'].set()
        service.subscribers.discard(queue)
        await llm.close()


async def test_cancel_after_publication_finishes_draft(service, repo, fake_llm, monkeypatch):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    original = service.store.put

    async def cancel_after_commit(kind, id, body):
        await original(kind, id, body)
        if kind == 'report':
            raise asyncio.CancelledError()

    monkeypatch.setattr(service.store, 'put', cancel_after_commit)
    try:
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 5)
        draft = await service.store.get('report_draft', job['id'])
        assert draft['status'] == 'completed'
        assert (await service.store.get('job', job['id']))['status'] == 'completed'
        assert await service.store.get('report', draft['report_id'])
    finally:
        await llm.close()
