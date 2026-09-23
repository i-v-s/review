import asyncio

import pytest

from review_service.app import SERVICE, create_app
from review_service.config import Config
from test_llm import attach, fake_llm


async def test_models_and_selection_require_authentication(client):
    assert (await client.get('/api/v1/models', headers={'Authorization': ''})).status == 401
    assert (await client.put('/api/v1/model', json={'model': 'other'}, headers={'Authorization': ''})).status == 401
    assert (await client.put('/api/v1/model', json={'model': 'other'}, headers={'Origin': 'https://other.invalid'})).status == 403


@pytest.mark.parametrize('body', [{}, [], {'model': ''}, {'model': '  '}, {'model': 42},
                                 {'model': False}, {'model': []}, {'model': 'x' * 1025},
                                 {'model': 'valid', 'api_key': 'no'}])
async def test_invalid_model_selection_is_rejected(client, service, body):
    previous = service.llm.model
    response = await client.put('/api/v1/model', json=body)
    assert response.status == 400
    assert service.llm.model == previous
    assert await service.store.setting(service.model_setting_key) is None


async def test_model_list_manual_selection_and_reset(client, service, fake_llm):
    llm = await attach(service, fake_llm)
    queue = asyncio.Queue()
    service.subscribers.add(queue)
    try:
        fake_llm[1]['models'] = ['z', 'a', 'z']
        response = await client.get('/api/v1/models')
        assert response.status == 200
        assert await response.json() == {'models': ['a', 'z']}
        response = await client.put('/api/v1/model', json={'model': '  manually-entered  '})
        assert await response.json() == {'model': 'manually-entered'}
        assert (await queue.get())['type'] == 'model_changed'
        state = await (await client.get('/api/v1/state')).json()
        assert state['llm_model'] == 'manually-entered'
        assert state['llm_default_model'] == 'test-model'
        assert state['llm_configured'] and state['llm_available']
        assert 'api_key' not in state and 'base_url' not in state and 'llm_proxy' not in state
        assert service.config.model == 'test-model'
        assert await service.store.setting(service.model_setting_key) == 'manually-entered'
        response = await client.put('/api/v1/model', json={'model': None})
        assert await response.json() == {'model': 'test-model'}
        assert await service.store.setting(service.model_setting_key) is None
    finally:
        service.subscribers.discard(queue)
        await llm.close()


@pytest.mark.parametrize('status', [404, 503])
async def test_catalog_failure_does_not_block_manual_model(client, service, fake_llm, status):
    llm = await attach(service, fake_llm)
    try:
        fake_llm[1].update(models_status=status, error_message='test-key unavailable')
        response = await client.get('/api/v1/models')
        assert response.status == 502
        error = (await response.json())['error']
        assert f'HTTP {status}' in error
        assert 'test-key' not in error
        assert fake_llm[1]['model_requests'] == 1
        assert (await client.put('/api/v1/model', json={'model': 'custom'})).status == 200
        assert service.llm.available
    finally:
        await llm.close()


async def test_catalog_timeout_does_not_change_model(client, service, fake_llm, monkeypatch):
    llm = await attach(service, fake_llm)
    # Avoid a ten-second sleep while exercising the actual timeout handling.
    from review_service import llm as llm_module
    real_timeout = asyncio.timeout
    monkeypatch.setattr(llm_module.asyncio, 'timeout', lambda seconds: real_timeout(0.01 if seconds == 10 else seconds))
    fake_llm[1]['models_delay'] = 0.1
    try:
        response = await client.get('/api/v1/models')
        assert response.status == 504
        assert '10 с' in (await response.json())['error']
        assert service.llm.model == 'test-model'
    finally:
        await llm.close()


async def test_model_can_be_selected_without_environment_default(repo, tmp_path, fake_llm, aiohttp_client):
    config = Config(repo, state_dir=tmp_path / 'model-state', token='test-secret',
                    api_key='test-key', base_url=fake_llm[0], model='', poll_interval=3600)
    client = await aiohttp_client(create_app(config), headers={'Authorization': 'Bearer test-secret'})
    state = await (await client.get('/api/v1/state')).json()
    assert state['llm_configured'] and not state['llm_available']
    assert (await client.get('/api/v1/models')).status == 200
    assert (await client.post('/api/v1/reports')).status == 503
    assert (await client.put('/api/v1/model', json={'model': 'other-model'})).status == 200
    (repo / 'example.py').write_text('changed\n')
    response = await client.post('/api/v1/reports')
    assert response.status == 202
    job = await response.json()
    await asyncio.wait_for(client.app[SERVICE].tasks[job['id']], 5)
    assert fake_llm[1]['requests'][0]['model'] == 'other-model'
    assert (await client.put('/api/v1/model', json={'model': None})).status == 200
    assert not (await (await client.get('/api/v1/state')).json())['llm_available']


async def test_selection_survives_restart_and_is_scoped_to_provider(repo, tmp_path, fake_llm, aiohttp_client):
    def config(base_url):
        return Config(repo, state_dir=tmp_path / 'persistent-model', token='test-secret',
                      api_key='test-key', model='default-model', base_url=base_url, poll_interval=3600)
    async def start(base_url):
        return await aiohttp_client(create_app(config(base_url)), headers={'Authorization': 'Bearer test-secret'})
    first = await start(fake_llm[0])
    await first.put('/api/v1/model', json={'model': 'saved-model'})
    await first.close()
    second = await start(fake_llm[0] + '/')
    assert (await (await second.get('/api/v1/state')).json())['llm_model'] == 'saved-model'
    await second.close()
    third = await start(fake_llm[0] + '/other')
    assert (await (await third.get('/api/v1/state')).json())['llm_model'] == 'default-model'
    await third.close()


async def test_jobs_freeze_model_at_acceptance_and_record_it(client, service, repo, fake_llm, monkeypatch):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    gate = asyncio.Event()
    original = service.generate
    async def delayed(id, *, model=None):
        await gate.wait()
        return await original(id, model=model)
    monkeypatch.setattr(service, 'generate', delayed)
    try:
        first = await (await client.post('/api/v1/reports')).json()
        task = service.tasks[first['id']]
        assert first['model'] == 'test-model'
        await client.put('/api/v1/model', json={'model': 'other-model'})
        gate.set()
        await asyncio.wait_for(task, 5)
        first_job = await service.store.get('job', first['id'])
        report = await service.store.get('report', first_job['result']['report_id'])
        draft = await service.store.get('report_draft', first['id'])
        assert first_job['model'] == report['model'] == draft['model'] == 'test-model'
        second = await (await client.post('/api/v1/reports')).json()
        await asyncio.wait_for(service.tasks[second['id']], 5)
        chat = await (await client.post('/api/v1/questions', json={
            'question': 'Q', 'snapshot_id': service.current.id})).json()
        await asyncio.wait_for(service.tasks[chat['id']], 5)
        assert [r['model'] for r in fake_llm[1]['requests']] == ['test-model', 'other-model', 'other-model']
        messages = await service.store.list('message')
        assert messages[-1]['model'] == 'other-model'
        assert (await service.store.get('job', second['id']))['model'] == 'other-model'
    finally:
        gate.set()
        await llm.close()


async def test_all_batches_and_repairs_use_frozen_model_and_batch_references(service, fake_llm):
    service.config.max_context_chars = 220
    fake_llm[1].update(invalid=True, repair=True)
    llm = await attach(service, fake_llm)
    async def progress(_):
        await service.select_model('new-model')
    try:
        report, _ = await llm.report([{'id': f'f{n}', 'text': 'x' * 50} for n in range(3)], [], progress)
        requests = fake_llm[1]['requests']
        assert len(requests) == 6
        assert {r['model'] for r in requests} == {'test-model'}
        assert len(report.items) == 3
        for index, request in enumerate(requests):
            schema = request['response_format']['json_schema']['schema']
            assert schema['$defs']['ReviewItem']['properties']['fragment_ids']['items']['enum'] == [f'f{index // 2}']
        assert llm.model == 'new-model'
    finally:
        await llm.close()
