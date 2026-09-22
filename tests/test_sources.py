import json
import sys

import aiohttp
from aiohttp import web

from review_service.sources import CodexAdapter, OpenCodeAdapter, normalize


def test_unknown_events_retained():
    messages = [{'id': 'x', 'type': 'futureType', 'payload': {'answer': 42}}]
    events = normalize('codex', 'session', messages)
    assert events[0].source_id == 'x'
    assert '42' in events[0].text
    assert events[0].raw == messages[0]


async def test_opencode_http_contract(repo, aiohttp_server):
    async def sessions(request):
        assert request.query['directory'] == str(repo)
        return web.json_response([{'id': 's', 'title': 'Session', 'directory': str(repo)}])
    async def info(request):
        return web.json_response({'id': 's', 'directory': str(repo)})
    async def messages(request):
        return web.json_response([{'info': {'id': 'm', 'role': 'assistant'}, 'parts': [{'text': 'A decision'}]}])
    app = web.Application()
    app.add_routes([web.get('/session', sessions), web.get('/session/s', info), web.get('/session/s/message', messages)])
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as http:
        adapter = OpenCodeAdapter(http, str(server.make_url('')).rstrip('/'), repo)
        assert (await adapter.sessions())[0]['id'] == 's'
        assert (await adapter.events('s'))[0].text == 'A decision'


async def test_codex_stdio_contract(repo, tmp_path):
    script = tmp_path / 'fake-codex'
    script.write_text(f'#!{sys.executable}\n' + '''import sys, json
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'userAgent': 'fake'}
    elif method == 'thread/list':
        result = {'data': [{'id': 't1', 'preview': 'Task'}]}
    elif method == 'thread/read':
        assert request['params']['includeTurns'] is True
        result = {'thread': {'turns': [{'items': [{'id': 'm1', 'type': 'agentMessage', 'text': 'Use a lock'}]}]}}
    else:
        raise RuntimeError(method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
    script.chmod(0o755)
    adapter = CodexAdapter(str(script), repo)
    try:
        assert (await adapter.sessions())[0]['id'] == 't1'
        assert (await adapter.events('t1'))[0].text == 'Use a lock'
    finally:
        await adapter.close()
