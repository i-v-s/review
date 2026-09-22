import asyncio
from urllib.parse import urlsplit

import pytest

from review_service.config import Config
from test_llm import attach, fake_llm


@pytest.mark.parametrize('proxy', [
    'http://host:1080', 'socks5://host', 'socks5://:1080', 'socks5://host:70000',
    'socks5://host:0', 'socks5://host:1080/path', 'socks5://host:1080?x=1',
    'socks5://name:secret@[broken:1080',
])
def test_proxy_validation_does_not_expose_credentials(tmp_path, proxy):
    with pytest.raises(ValueError, match='REVIEW_LLM_PROXY') as error:
        Config(tmp_path / 'repo', state_dir=tmp_path / 'state', llm_proxy=proxy)
    assert 'secret' not in str(error.value)
    assert proxy not in str(error.value)


def test_proxy_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv('REVIEW_LLM_PROXY', 'socks5h://name:secret@[::1]:1080')
    config = Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state')
    assert config.llm_proxy == 'socks5h://name:secret@[::1]:1080'
    assert 'secret' not in repr(config)


@pytest.fixture
async def socks_proxy():
    control = {'auth': None, 'reject': False, 'connections': [], 'credentials': []}
    tasks, writers = set(), set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        upstream = None
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(count)
            method = 2 if control['auth'] else 0
            assert method in methods
            writer.write(bytes([5, method]))
            await writer.drain()
            if method == 2:
                version, length = await reader.readexactly(2)
                username = await reader.readexactly(length)
                length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(length)
                control['credentials'].append((username.decode(), password.decode()))
                ok = (username.decode(), password.decode()) == control['auth']
                writer.write(bytes([1, 0 if ok else 1]))
                await writer.drain()
                if not ok:
                    return
            version, command, _, address_type = await reader.readexactly(4)
            assert version == 5 and command == 1
            if address_type == 3:
                length = (await reader.readexactly(1))[0]
                hostname = (await reader.readexactly(length)).decode()
            else:
                import ipaddress
                hostname = str(ipaddress.ip_address(await reader.readexactly(4 if address_type == 1 else 16)))
            port = int.from_bytes(await reader.readexactly(2), 'big')
            control['connections'].append((hostname, port))
            if control['reject']:
                writer.write(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                return
            remote, upstream = await asyncio.open_connection('127.0.0.1', port)
            writers.add(upstream)
            writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            await writer.drain()
            async def forward(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                destination.close()
            await asyncio.gather(forward(reader, upstream), forward(remote, writer))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            tasks.discard(task)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    control['port'] = server.sockets[0].getsockname()[1]
    try:
        yield control
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize('scheme,auth', [('socks5', False), ('socks5h', True)])
async def test_report_and_chat_use_socks_and_remote_dns(service, repo, fake_llm, socks_proxy, scheme, auth):
    (repo / 'example.py').write_text('changed\n')
    credentials = 'user%40name:pass%3Aword@' if auth else ''
    socks_proxy['auth'] = ('user@name', 'pass:word') if auth else None
    service.config.llm_proxy = f"{scheme}://{credentials}127.0.0.1:{socks_proxy['port']}"
    url, control = fake_llm
    remote_url = f'http://llm.invalid:{urlsplit(url).port}/v1'
    llm = await attach(service, (remote_url, control))
    try:
        result = await service.generate('proxy-report')
        assert await service.store.get('report', result['report_id'])
        answer = ''.join([part async for part in llm.answer('Q', [], [], [], None)])
        assert answer == 'Проверка граничных случаев необходима.'
        assert len(control['requests']) == 2
        assert all(host == 'llm.invalid' for host, port in socks_proxy['connections'])
        assert socks_proxy['connections']
        if auth:
            assert socks_proxy['credentials']
            assert all(credentials == ('user@name', 'pass:word') for credentials in socks_proxy['credentials'])
    finally:
        await llm.close()


async def test_proxy_failure_never_falls_back_to_direct(service, repo, fake_llm, socks_proxy):
    (repo / 'example.py').write_text('changed\n')
    socks_proxy.update(reject=True, auth=('name', 'secret'))
    service.config.llm_proxy = f"socks5://name:secret@127.0.0.1:{socks_proxy['port']}"
    llm = await attach(service, fake_llm)
    try:
        job = await service.start_job('report', service.generate)
        await asyncio.wait_for(service.tasks[job['id']], 5)
        failed = await service.store.get('job', job['id'])
        assert failed['status'] == 'failed'
        assert 'REVIEW_LLM_PROXY' in failed['error']
        assert 'secret' not in failed['error']
        assert not fake_llm[1]['requests']
    finally:
        await llm.close()
