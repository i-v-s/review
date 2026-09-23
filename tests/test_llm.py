import asyncio
import json

import pytest
from aiohttp import web

from review_service.config import Config
from review_service.git import diff_fragments
from review_service.llm import LLM
from review_service.models import ReviewError, SourceEvent


def completion(content):
    return {'id': 'local', 'object': 'chat.completion', 'created': 1, 'model': 'test-model',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}


@pytest.fixture
async def fake_llm(aiohttp_server):
    control = {'invalid': False, 'invalid_json': False, 'status': 200, 'delay': 0,
               'chunk_delay': 0, 'requests': [], 'finish_reason': 'stop', 'before_finish': None,
               'paused': asyncio.Event(), 'pieces': None, 'repair': False, 'piece_gates': {},
               'error_message': 'Simulated provider failure', 'report_patch': {},
               'models': ['test-model', 'other-model'], 'models_status': 200,
               'models_delay': 0, 'model_requests': 0}
    async def handler(request):
        body = await request.json()
        control['requests'].append(body)
        await asyncio.sleep(control['delay'])
        if control['status'] != 200:
            return web.json_response({'error': {'message': control['error_message']}}, status=control['status'])
        is_report = body['messages'][0]['content'].startswith('You prepare')
        if is_report:
            payload = json.loads(body['messages'][1]['content'])
            fragment = payload['fragments'][0]
            source_ids = [payload['sources'][0]['source_id']] if payload['sources'] else []
            invalid = control['invalid'] and not (control['repair'] and len(body['messages']) > 2)
            report = {'summary': 'Добавлена обработка граничных случаев.', 'items': [
                {'section': 'Корректность', 'title': 'Сохраняем инвариант', 'explanation': 'Разбираем изменение поведения.',
                 'rationale_kind': 'recorded' if source_ids else 'reconstructed', 'argument': 'Нужна проверка.',
                 'limitations': [], 'dependencies': [],
                 'source_ids': source_ids, 'fragment_ids': ['invented' if invalid else fragment['id']]}
            ], 'findings': []}
            report.update(control['report_patch'])
            content = 'not JSON' if control['invalid_json'] else json.dumps(report, ensure_ascii=False)
            pieces = control['pieces'] or [content[n:n + 31] for n in range(0, len(content), 31)]
        else:
            content = 'Проверка граничных случаев необходима.'
            pieces = ['Проверка ', 'граничных случаев необходима.']
        if not body.get('stream'):
            return web.json_response(completion(content))
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        try:
            for index, text in enumerate(pieces):
                event = {'id': 'local', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model',
                         'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]}
                await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
                if is_report and index in control['piece_gates']:
                    await control['piece_gates'][index].wait()
                await asyncio.sleep(control['chunk_delay'])
            if is_report and control['before_finish']:
                control['paused'].set()
                await control['before_finish'].wait()
            if control['finish_reason']:
                event['choices'] = [{'index': 0, 'delta': {}, 'finish_reason': control['finish_reason']}]
                await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
            await response.write(b'data: [DONE]\n\n')
        except ConnectionResetError:
            pass
        return response
    app = web.Application()
    app.router.add_post('/v1/chat/completions', handler)
    async def models(request):
        control['model_requests'] += 1
        await asyncio.sleep(control['models_delay'])
        if control['models_status'] != 200:
            return web.json_response({'error': {'message': control['error_message']}}, status=control['models_status'])
        return web.json_response({'object': 'list', 'data': [
            {'id': id, 'object': 'model', 'created': 1, 'owned_by': 'test'} for id in control['models']]})
    app.router.add_get('/v1/models', models)
    server = await aiohttp_server(app)
    return str(server.make_url('/v1')), control


async def attach(service, fake_llm):
    url, control = fake_llm
    service.config.api_key = 'test-key'
    service.config.model = 'test-model'
    service.config.base_url = url
    service.llm = LLM(service.config)
    await service.llm.open()
    return service.llm


async def test_report_validates_refs_and_covers_all_fragments(service, repo, fake_llm):
    (repo / 'example.py').write_text(''.join(f'{n}\n' for n in range(120)))
    llm = await attach(service, fake_llm)
    try:
        await service.store.event(SourceEvent(id='d1', provider='decision', session_id='s', source_id='m', role='decision', text='Original reason'))
        result = await service.generate('job')
        report = await service.store.get('report', result['report_id'])
        assert {fid for i in report['items'] for fid in i['fragment_ids']} == {f['id'] for f in diff_fragments(service.current)}
        assert any(i['section'] == 'Без объяснения' for i in report['items'])
        await service.store.event(SourceEvent(id='d1', provider='decision', session_id='s', source_id='m', role='decision', text='New reason'))
        assert (await service.store.get('report_evidence', report['id']))['events'][0]['text'] == 'Original reason'
    finally:
        await llm.close()


async def test_invented_references_rejected(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    fake_llm[1]['invalid'] = True
    llm = await attach(service, fake_llm)
    try:
        with pytest.raises(ReviewError, match='Неизвестная ссылка: items.0.fragment_ids'):
            await service.generate('job')
        assert not await service.store.list('report')
        assert len(fake_llm[1]['requests']) == 2
    finally:
        await llm.close()


async def test_streaming_answer(service, repo, fake_llm):
    llm = await attach(service, fake_llm)
    try:
        answer = ''.join([part async for part in llm.answer('Что проверить?', [], [], [], None)])
        assert answer == 'Проверка граничных случаев необходима.'
    finally:
        await llm.close()


async def test_cancel_job_is_persisted(service):
    async def slow(id):
        await asyncio.sleep(20)
    job = await service.start_job('chat', slow)
    await asyncio.sleep(0.01)
    task = service.tasks[job['id']]
    task.cancel()
    await task
    assert (await service.store.get('job', job['id']))['status'] == 'cancelled'


async def test_invalid_json_does_not_publish(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    fake_llm[1]['invalid_json'] = True
    llm = await attach(service, fake_llm)
    try:
        with pytest.raises(ReviewError):
            await service.generate('job')
        assert not await service.store.list('report')
    finally:
        await llm.close()


async def test_provider_failure_becomes_failed_job(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    fake_llm[1]['status'] = 503
    llm = await attach(service, fake_llm)
    try:
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 10)
        assert (await service.store.get('job', job['id']))['status'] == 'failed'
        assert not await service.store.list('report')
    finally:
        await llm.close()


async def test_timeout_is_actionable_and_preserves_previous_report(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    service.config.llm_timeout_seconds = 0.02
    service.config.llm_max_retries = 0
    llm = await attach(service, fake_llm)
    try:
        first = await service.generate('initial')
        existing_ids = {report['id'] for report in await service.store.list('report')}
        assert first['report_id'] in existing_ids

        fake_llm[1]['delay'] = 0.1
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 2)
        failed = await service.store.get('job', job['id'])

        assert failed['status'] == 'failed'
        assert failed['error'].startswith('LLM не ответила за 0.02 с.')
        assert 'REVIEW_LLM_TIMEOUT_SECONDS' in failed['error']
        assert 'APITimeoutError' not in failed['error']
        assert {report['id'] for report in await service.store.list('report')} == existing_ids
        assert len(fake_llm[1]['requests']) == 2
    finally:
        await llm.close()


def test_llm_limits_are_loaded_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('REVIEW_LLM_TIMEOUT_SECONDS', '321.5')
    monkeypatch.setenv('REVIEW_LLM_MAX_RETRIES', '2')
    monkeypatch.setenv('REVIEW_MAX_CONTEXT_CHARS', '50000')
    monkeypatch.setenv('REVIEW_MAX_OUTPUT_TOKENS', '4096')

    config = Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state')

    assert config.llm_timeout_seconds == 321.5
    assert config.llm_max_retries == 2
    assert config.max_context_chars == 50000
    assert config.max_output_tokens == 4096


@pytest.mark.parametrize('status,hint', [(400, 'размер контекста'), (401, 'OPENAI_API_KEY'),
                                      (403, 'права'), (404, 'REVIEW_MODEL'),
                                      (429, 'квота'), (503, 'стороне провайдера')])
async def test_provider_http_error_includes_cause_and_preserves_report(service, repo, fake_llm, status, hint):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    try:
        previous = await service.generate('previous')
        fake_llm[1].update(status=status, error_message='no router for requested model')
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 5)
        failed = await service.store.get('job', job['id'])
        assert failed['status'] == 'failed'
        assert f'HTTP {status}' in failed['error']
        assert hint in failed['error']
        assert 'test-model' in failed['error']
        assert 'no router for requested model' in failed['error']
        assert 'Проверьте доступность провайдера' not in failed['error']
        draft = await service.store.get('report_draft', job['id'])
        assert draft['error'] == failed['error']
        assert [r['id'] for r in await service.store.list('report')] == [previous['report_id']]
    finally:
        await llm.close()


async def test_chat_http_error_includes_status_and_redacts_credentials(service, fake_llm):
    llm = await attach(service, fake_llm)
    try:
        # Configure proxy metadata after opening the local client to exercise error
        # redaction without sending this test's request through an actual proxy.
        service.config.llm_proxy = 'socks5://proxy-user:proxy%3Apassword@localhost:1080'
        fake_llm[1].update(status=401, error_message=(
            'Invalid key test-key; browser test-secret; '
            'socks5://proxy-user:proxy%3Apassword@localhost:1080; proxy:password'))
        with pytest.raises(ReviewError) as raised:
            _ = [part async for part in llm.answer('Q', [], [], [], None)]
        error = str(raised.value)
        assert 'HTTP 401' in error and 'OPENAI_API_KEY' in error
        assert '[скрыто]' in error
        for secret in ('test-key', 'test-secret', 'proxy-user', 'proxy%3Apassword', 'proxy:password'):
            assert secret not in error
        assert 'Проверьте адрес, доступность и авторизацию прокси' not in error
    finally:
        await llm.close()


@pytest.mark.parametrize('enabled', [True, False])
async def test_report_format_is_sent_on_initial_and_repair_but_not_chat(service, fake_llm, enabled):
    service.config.llm_structured_output = enabled
    fake_llm[1].update(invalid=True, repair=True)
    llm = await attach(service, fake_llm)
    async def progress(_):
        pass
    try:
        await llm.report([{'id': 'f1'}], [], progress)
        requests = fake_llm[1]['requests']
        assert len(requests) == 2
        for request in requests:
            assert ('response_format' in request) == enabled
            if enabled:
                wrapper = request['response_format']
                assert wrapper['type'] == 'json_schema'
                assert wrapper['json_schema']['strict'] is True
                schema = wrapper['json_schema']['schema']
                assert schema['$defs']['ReviewItem']['properties']['fragment_ids']['items']['enum'] == ['f1']
                assert json.loads(request['messages'][0]['content'].split('findings:\n')[1]) == schema
        _ = [part async for part in llm.answer('Q', [], [], [], None)]
        assert 'response_format' not in requests[-1]
    finally:
        await llm.close()


@pytest.mark.parametrize('failure,detail', [
    ('json', 'Некорректный JSON'), ('structure', 'Нарушена структура отчёта: items.0.title'),
    ('source_ids', 'Неизвестная ссылка: items.0.source_ids'),
    ('dependencies', 'Неизвестная ссылка: items.0.dependencies'),
    ('recorded', 'У записанного решения отсутствует источник: items.0.source_ids'),
])
async def test_validation_diagnostics_preserve_previous_report(service, repo, fake_llm, failure, detail):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    try:
        previous = await service.generate('previous')
        item = {'section': 'S', 'title': 'T', 'explanation': 'E'}
        if failure == 'json':
            fake_llm[1]['invalid_json'] = True
        elif failure == 'structure':
            item.pop('title')
        elif failure == 'recorded':
            item['rationale_kind'] = 'recorded'
        else:
            item[failure] = ['invented-secret-text']
        fake_llm[1]['report_patch'] = {'items': [item]}
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 5)
        failed = await service.store.get('job', job['id'])
        draft = await service.store.get('report_draft', job['id'])
        assert failed['status'] == draft['status'] == 'failed'
        assert detail in failed['error'] == draft['error']
        assert 'порция 1/1' in failed['error'] and 'test-model' in failed['error']
        assert 'invented-secret-text' not in failed['error']
        assert detail in fake_llm[1]['requests'][-1]['messages'][-1]['content']
        assert [r['id'] for r in await service.store.list('report')] == [previous['report_id']]
        assert len(fake_llm[1]['requests']) == 3
    finally:
        await llm.close()


async def test_schema_rejection_has_opt_out_hint_without_retry(service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    fake_llm[1].update(status=400, error_message='response_format unsupported')
    try:
        with pytest.raises(ReviewError, match='REVIEW_LLM_STRUCTURED_OUTPUT=0'):
            await service.generate('job')
        assert len(fake_llm[1]['requests']) == 1
    finally:
        await llm.close()


@pytest.mark.parametrize('value,expected', [(None, True), ('1', True), ('0', False)])
def test_structured_output_configuration(monkeypatch, tmp_path, value, expected):
    if value is None:
        monkeypatch.delenv('REVIEW_LLM_STRUCTURED_OUTPUT', raising=False)
    else:
        monkeypatch.setenv('REVIEW_LLM_STRUCTURED_OUTPUT', value)
    assert Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state').llm_structured_output is expected
    monkeypatch.setenv('REVIEW_LLM_STRUCTURED_OUTPUT', 'typo')
    with pytest.raises(ValueError, match='REVIEW_LLM_STRUCTURED_OUTPUT'):
        Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state')
