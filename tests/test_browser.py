import asyncio
import json
import os

import pytest
from playwright.async_api import async_playwright, expect

from review_service.models import SourceEvent
from review_service.git import diff_fragments
from test_llm import attach, fake_llm


@pytest.mark.browser
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
async def test_complete_review_in_browser(client, service, repo, fake_llm, viewport):
    (repo / 'example.py').write_text('ONE\ntwo\nTHREE\nfour\n')
    await service.refresh()
    await service.store.event(SourceEvent(id='decision:test', provider='decision', session_id='test',
                                          source_id='test', role='decision', text='Use explicit boundary checks.'))
    llm = await attach(service, fake_llm)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page(viewport=viewport, is_mobile=viewport['width'] < 700,
                                      has_touch=viewport['width'] < 700)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            await page.get_by_label('Токен доступа').fill('test-secret')
            await page.get_by_role('button', name='Открыть workspace').click()
            await expect(page.locator('#workspace')).to_be_visible()
            await page.locator('#generate').click()
            await expect(page.get_by_role('heading', name='Сохраняем инвариант')).to_be_visible(timeout=15000)
            await page.get_by_role('button', name='Источник 1').click()
            await expect(page.locator('#dialog')).to_contain_text('Use explicit boundary checks.')
            await page.locator('#dialog-close').click()
            await page.get_by_label('Решение просмотрено').check()
            await page.get_by_role('button', name='Спросить ↗', exact=True).click()
            await page.get_by_label('Вопрос об изменениях').fill('Какие проверки нужны?')
            await page.get_by_role('button', name='Спросить ↑').click()
            await expect(page.locator('#chat-messages')).to_contain_text('Проверка граничных случаев необходима.', timeout=15000)
            await page.locator('#chat-close').click()
            await page.screenshot(path=f"test-results/review-{viewport['width']}.png", full_page=True)
            await page.locator('[data-view="changes"]').click()
            first = page.locator('.diff').first
            await first.locator('input[data-line="d:0"]').check()
            await first.locator('input[data-line="a:0"]').check()
            await first.get_by_role('button', name='Stage строк', exact=True).click()
            await expect(page.get_by_role('heading', name='Подготовлено к коммиту')).to_be_visible()
            unstaged = page.locator('.diff').filter(has=page.locator('.tag', has_text='unstaged')).first
            await unstaged.locator('input[data-line="d:2"]').check()
            await unstaged.locator('input[data-line="a:2"]').check()
            await unstaged.get_by_role('button', name='Отменить строки', exact=True).click()
            await expect(page.locator('#dialog')).to_be_visible()
            await page.locator('#dialog').get_by_role('button', name='Отменить изменения', exact=True).click()
            await expect(page.locator('#dialog')).not_to_be_visible()
            assert (repo / 'example.py').read_text() == 'ONE\ntwo\nthree\nfour\n'
            await page.locator('[data-view="history"]').click()
            await page.get_by_role('button', name='Восстановить ↶').first.click()
            await expect(page.locator('#toast')).to_contain_text('Предыдущее состояние восстановлено')
            assert (repo / 'example.py').read_text() == 'ONE\ntwo\nTHREE\nfour\n'
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            await browser.close()
            await llm.close()


@pytest.mark.browser
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
async def test_live_draft_reconnect_scroll_and_completion(client, service, repo, fake_llm, viewport):
    (repo / 'example.py').write_text(''.join(f'changed line {n}\n' for n in range(120)))
    snapshot = await service.refresh()
    fragment = diff_fragments(snapshot)[0]
    await service.store.event(SourceEvent(id='decision:stream', provider='decision', session_id='s',
                                        source_id='m', role='decision', text='Frozen streaming evidence'))
    item = {'section': 'Проверка', 'title': 'Первый пункт', 'explanation': 'Объяснение изменения.',
            'source_ids': ['decision:stream'], 'fragment_ids': [fragment['id']]}
    control = fake_llm[1]
    gates = {0: asyncio.Event(), 1: asyncio.Event()}
    control.update(piece_gates=gates, before_finish=asyncio.Event(), pieces=[
        '{"summary":"Постепенный',
        ' отчёт", "items":[' + json.dumps(item, ensure_ascii=False),
        ',' + json.dumps({**item, 'title': 'Второй пункт'}, ensure_ascii=False) + '], "findings":[]}',
    ])
    llm = await attach(service, fake_llm)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page(viewport=viewport, is_mobile=viewport['width'] < 700)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            await page.get_by_label('Токен доступа').fill('test-secret')
            await page.get_by_role('button', name='Открыть workspace').click()
            await page.locator('#generate').click()
            await expect(page.locator('.summary-text')).to_have_text('Постепенный')
            await expect(page.locator('.review-card')).to_have_count(0)
            assert not await service.store.list('report')
            gates[0].set()
            await expect(page.get_by_role('heading', name='Первый пункт')).to_be_visible()
            await expect(page.get_by_label('Решение просмотрено')).to_be_disabled()
            await expect(page.get_by_role('button', name='Спросить ↗', exact=True)).to_be_disabled()
            await expect(page.get_by_role('button', name='Stage файла', exact=True)).to_be_disabled()
            await page.get_by_role('button', name='Источник 1').click()
            await expect(page.locator('#dialog')).to_contain_text('Frozen streaming evidence')
            await page.locator('#dialog-close').click()
            await page.reload()
            await expect(page.get_by_role('heading', name='Первый пункт')).to_be_visible()
            # Force an SSE disconnect; connect() must resync the saved draft.
            for queue in service.subscribers:
                queue.put_nowait({'type': 'shutdown'})
            await expect(page.locator('#connection')).to_have_text('Переподключение…')
            await expect(page.locator('#connection')).to_have_text('Подключено', timeout=10000)
            await page.evaluate('window.scrollTo(0, 450)')
            position = await page.evaluate('window.scrollY')
            assert position > 0
            await page.locator('.review-card').first.evaluate('(node) => window.firstPreviewCard = node')
            gates[1].set()
            await expect(page.get_by_role('heading', name='Второй пункт')).to_be_attached()
            assert await page.evaluate('window.scrollY') == position
            assert await page.locator('.review-card').first.evaluate('(node) => node === window.firstPreviewCard')
            assert not await service.store.list('report')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=f"test-results/draft-{viewport['width']}.png", full_page=True)
            control['before_finish'].set()
            await expect(page.locator('.draft-status')).to_have_count(0)
            await expect(page.get_by_label('Решение просмотрено').first).to_be_enabled()
            await expect(page.get_by_role('button', name='Спросить ↗', exact=True).first).to_be_enabled()
            assert errors == []
        finally:
            for gate in gates.values():
                gate.set()
            control['before_finish'].set()
            await browser.close()
            await llm.close()


@pytest.mark.browser
async def test_cancelled_draft_stays_selectable_with_previous_report(client, service, repo, fake_llm):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    previous = await service.generate('previous')
    control = fake_llm[1]
    control['before_finish'] = asyncio.Event()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page()
        try:
            await page.goto(str(client.make_url('/')))
            await page.get_by_label('Токен доступа').fill('test-secret')
            await page.get_by_role('button', name='Открыть workspace').click()
            await page.locator('#generate').click()
            await expect(page.locator('.draft-status')).to_contain_text('Генерируется')
            await expect(page.get_by_role('heading', name='Сохраняем инвариант')).to_be_visible()
            job_id = service.active_report
            await page.get_by_label('Версия отчёта').select_option('report:' + previous['report_id'])
            await expect(page.locator('.draft-status')).to_have_count(0)
            await page.get_by_role('button', name='Отменить', exact=True).click()
            await expect(page.locator('#job-status')).not_to_be_visible()
            await expect(page.get_by_label('Версия отчёта')).to_have_value('report:' + previous['report_id'])
            await page.get_by_label('Версия отчёта').select_option('draft:' + job_id)
            await expect(page.locator('.draft-status')).to_contain_text('Не завершён')
            await expect(page.locator('.draft-status')).to_contain_text('Генерация отменена')
            await page.reload()
            await expect(page.locator('.draft-status')).to_contain_text('Не завершён')
            await expect(page.get_by_role('heading', name='Сохраняем инвариант')).to_be_visible()
        finally:
            control['before_finish'].set()
            await browser.close()
            await llm.close()


@pytest.mark.browser
async def test_report_start_and_reload_do_not_fetch_an_unsaved_draft(client, service, repo, fake_llm, monkeypatch):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    ready = asyncio.Event()
    original_generate = service._generate

    async def delayed_generate(job_id):
        await ready.wait()
        return await original_generate(job_id)

    monkeypatch.setattr(service, '_generate', delayed_generate)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page()
        failed_responses = []
        console_errors = []
        page.on('response', lambda response: failed_responses.append(response.url) if response.status == 404 else None)
        page.on('console', lambda message: console_errors.append((message.text, message.location)) if message.type == 'error' else None)
        try:
            await page.goto(str(client.make_url('/')))
            await page.get_by_label('Токен доступа').fill('test-secret')
            await page.get_by_role('button', name='Открыть workspace').click()
            await page.locator('#generate').click()
            await expect(page.locator('#job-status')).to_be_visible()
            await page.reload()
            await expect(page.locator('#connection')).to_have_text('Подключено')
            await expect(page.locator('#job-status')).to_be_visible()
            assert not await service.store.list('report_draft')
            assert failed_responses == [], console_errors
            assert console_errors == []
            ready.set()
            await expect(page.get_by_role('heading', name='Сохраняем инвариант')).to_be_visible(timeout=15000)
        finally:
            ready.set()
            if service.active_report in service.tasks:
                await asyncio.wait_for(service.tasks[service.active_report], 5)
            await browser.close()
            await llm.close()


@pytest.mark.browser
@pytest.mark.parametrize('status', ['failed', 'completed'])
async def test_reload_of_job_without_a_draft_uses_saved_report(client, service, repo, fake_llm, status):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    try:
        saved = await service.generate('saved')
    finally:
        await llm.close()
    job = {'id': 'without-draft', 'kind': 'report', 'status': status}
    if status == 'completed':
        job['result'] = saved
    await service.store.put('job', job['id'], job)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page()
        await page.add_init_script("sessionStorage.setItem('review-token', 'test-secret'); sessionStorage.setItem('review-report-selection', 'draft:without-draft');")
        failed_responses = []
        console_errors = []
        page.on('response', lambda response: failed_responses.append(response.url) if response.status == 404 else None)
        page.on('console', lambda message: console_errors.append((message.text, message.location)) if message.type == 'error' else None)
        try:
            await page.goto(str(client.make_url('/')))
            await expect(page.get_by_role('heading', name='Сохраняем инвариант')).to_be_visible()
            await expect(page.get_by_label('Версия отчёта')).to_have_value('report:' + saved['report_id'])
            assert failed_responses == [], console_errors
            assert console_errors == []
        finally:
            await browser.close()
