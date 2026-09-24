import asyncio

import pytest

from review_service.app import SERVICE, create_app
from review_service.config import Config
from review_service.models import SourceEvent, uid


async def test_auth_and_path_validation(client):
    response = await client.get('/api/v1/state', headers={'Authorization': ''})
    assert response.status == 401
    response = await client.post('/api/v1/untracked', json={'paths': ['../outside']})
    assert response.status == 400
    response = await client.get('/api/v1/state', headers={'Origin': 'https://untrusted.invalid'})
    assert response.status == 403


async def test_api_preserves_snapshot_and_preview(client, service, repo):
    (repo / 'example.py').write_text('new\n')
    await service.refresh()
    s = (await (await client.get('/api/v1/state')).json())['snapshot']
    body = dict(snapshot_id=s['id'], expected_version=s['version'], path='example.py',
                action='discard', whole_file=True, key=uid())
    preview = await (await client.post('/api/v1/operations/preview', json=body)).json()
    assert preview['content'] == 'one\ntwo\nthree\nfour\n'
    assert (repo / 'example.py').read_text() == 'new\n'
    response = await client.post('/api/v1/operations', json=body)
    assert response.status == 200
    op = await response.json()
    assert (repo / 'example.py').read_text() == preview['content']
    response = await client.get(f"/api/v1/snapshots/{s['id']}/file", params={'path': 'example.py'})
    assert (await response.json())['content'] == 'new\n'
    response = await client.post(f"/api/v1/operations/{op['id']}/undo", json={'key': uid()})
    assert response.status == 200
    assert (repo / 'example.py').read_text() == 'new\n'


async def test_edit_work_file_is_versioned_idempotent_and_undoable(client, service, repo):
    (repo / 'example.py').write_text('changed\n')
    snapshot = await service.refresh()
    body = dict(snapshot_id=snapshot.id, expected_version=snapshot.version, path='example.py',
                content='edited without final newline', key=uid())
    response = await client.post('/api/v1/operations/edit', json=body)
    assert response.status == 200
    op = await response.json()
    assert op['action'] == 'edit' and op['changes_work'] and op['status'] == 'completed'
    assert (repo / 'example.py').read_text() == body['content']
    assert (await client.post('/api/v1/operations/edit', json=body)).status == 200
    assert (await client.post('/api/v1/operations/edit', json={**body, 'key': uid()})).status == 409
    assert (await client.post('/api/v1/operations/edit', json={**body, 'content': 'other'})).status == 409
    undo = await client.post(f"/api/v1/operations/{op['id']}/undo", json={'key': uid()})
    assert undo.status == 200
    assert (repo / 'example.py').read_text() == 'changed\n'


async def test_report_operations_advance_working_snapshot_and_keep_original(client, service, repo):
    (repo / 'example.py').write_text('changed\n')
    original = await service.refresh()
    from review_service.git import diff_fragments
    fragment = diff_fragments(original)[0]
    report = dict(id='report-with-journal', snapshot_id=original.id, snapshot_version=original.version,
                  items=[dict(id='item-one', title='Change', fragment_ids=[fragment['id']])], findings=[])
    await service.store.put('report', report['id'], report)
    scope = dict(report_id=report['id'], target_kind='item', target_id='item-one')
    stage = await client.post('/api/v1/operations', json=dict(
        snapshot_id=original.id, expected_version=original.version, path='example.py',
        action='stage', whole_file=True, key=uid(), **scope))
    assert stage.status == 200
    staged_op = await stage.json()
    reported = await (await client.get('/api/v1/reports/' + report['id'])).json()
    assert reported['snapshot_id'] == original.id
    assert reported['working_snapshot_id'] == staged_op['after_id']
    assert [entry['id'] for entry in reported['journal']] == [staged_op['id']]
    assert reported['journal'][0]['target_id'] == 'item-one'

    edit = await client.post('/api/v1/operations/edit', json=dict(
        snapshot_id=staged_op['after_id'], expected_version=staged_op['after_version'],
        path='example.py', content='edited\n', key=uid(), **scope))
    assert edit.status == 200
    edit_op = await edit.json()
    reported = await (await client.get('/api/v1/reports/' + report['id'])).json()
    assert reported['working_snapshot_id'] == edit_op['after_id']
    assert len(reported['journal']) == 2
    undo = await client.post('/api/v1/operations/' + edit_op['id'] + '/undo', json={'key': uid()})
    assert undo.status == 200
    undo_op = await undo.json()
    reported = await (await client.get('/api/v1/reports/' + report['id'])).json()
    assert reported['working_snapshot_id'] == undo_op['after_id']
    assert reported['journal'][-1]['undo_of'] == edit_op['id']
    assert (repo / 'example.py').read_text() == 'changed\n'
    await service.store.put('report_live', report['id'],
                            {'snapshot_id': original.id, 'snapshot_version': original.version})
    await service.rebuild_report_live()
    restored = await (await client.get('/api/v1/reports/' + report['id'])).json()
    assert restored['working_snapshot_id'] == undo_op['after_id']


async def test_head_to_work_comparison_maps_both_layers(client, service, repo):
    (repo / 'example.py').write_text('ONE\ntwo\nthree\nfour\n')
    from conftest import git_command
    git_command(repo, 'add', 'example.py')
    (repo / 'example.py').write_text('ONE\nTWO\nthree\nfour\n')
    snapshot = await service.refresh()
    response = await client.get(f'/api/v1/snapshots/{snapshot.id}/comparison',
                                params={'path': 'example.py'})
    assert response.status == 200
    mapping = await response.json()
    assert 'a:0' in mapping['unstage']['b']['1']
    assert 'a:1' in mapping['stage']['b']['2']
    assert 'd:0' in mapping['unstage']['a']['1']
    assert 'd:1' in mapping['stage']['a']['2']


async def test_sync_captures_clean_file_for_current_editor(client, service, repo):
    state = await (await client.post('/api/v1/sync', json={'paths': ['example.py']})).json()
    file = await (await client.get(f"/api/v1/snapshots/{state['snapshot']['id']}/file",
                                   params={'path': 'example.py', 'side': 'work'})).json()
    assert file['content'] == 'one\ntwo\nthree\nfour\n'
    assert (await client.post('/api/v1/sync', json={'paths': ['../outside']})).status == 400


async def test_concurrent_operations_conflict(client, service, repo):
    (repo / 'example.py').write_text('new\n')
    snapshot = await service.refresh()
    base = dict(snapshot_id=snapshot.id, expected_version=snapshot.version, path='example.py', whole_file=True)
    responses = await asyncio.gather(
        client.post('/api/v1/operations', json={**base, 'action': 'stage', 'key': uid()}),
        client.post('/api/v1/operations', json={**base, 'action': 'discard', 'key': uid()}),
    )
    assert sorted(r.status for r in responses) == [200, 409]


async def test_no_llm_keeps_git_available(client):
    assert (await client.post('/api/v1/reports')).status == 503
    assert (await client.get('/api/v1/state')).status == 200
    response = await client.post('/api/v1/decisions', json={'text': 'Use a single writer.'})
    assert response.status == 201
    events = (await (await client.get('/api/v1/sources')).json())['events']
    assert events[0]['text'] == 'Use a single writer.'


async def test_import_deduplicates(client):
    export = {'info': {'id': 's1', 'title': 'Work'}, 'messages': [
        {'info': {'id': 'm1', 'role': 'assistant'}, 'parts': [{'type': 'text', 'text': 'Reason.'}]}
    ]}
    for _ in range(2):
        assert (await client.post('/api/v1/import', json=export)).status == 200
    sources = (await (await client.get('/api/v1/sources')).json())['events']
    assert len(sources) == 1
    assert sources[0]['text'] == 'Reason.'


async def test_undo_after_restart(repo, tmp_path, aiohttp_client):
    from test_git import operate
    config = Config(repo, state_dir=tmp_path / 'persist', token='secret', poll_interval=3600)
    first = await aiohttp_client(create_app(config))
    (repo / 'example.py').write_text('recover me\n')
    op = await operate(first.app[SERVICE], 'discard', whole=True)
    await first.close()
    second = await aiohttp_client(create_app(config))
    await second.app[SERVICE].undo(op['id'], uid())
    assert (repo / 'example.py').read_text() == 'recover me\n'


async def test_prepared_operation_recovers_after_restart(repo, tmp_path, aiohttp_client):
    from test_git import operate
    config = Config(repo, state_dir=tmp_path / 'recover', token='secret', poll_interval=3600)
    first = await aiohttp_client(create_app(config))
    (repo / 'example.py').write_text('recover after crash\n')
    service = first.app[SERVICE]
    op = await operate(service, 'discard', whole=True)
    op['status'] = 'prepared'
    op.pop('after_id')
    op.pop('after_version')
    await service.store.put('operation', op['id'], op)
    await first.close()
    second = await aiohttp_client(create_app(config))
    recovered = await second.app[SERVICE].store.get('operation', op['id'])
    assert recovered['status'] == 'completed'
    await second.app[SERVICE].undo(op['id'], uid())
    assert (repo / 'example.py').read_text() == 'recover after crash\n'
