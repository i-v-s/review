import asyncio
import json
import os
import re

import pytest
from playwright.async_api import async_playwright, expect

from review_service.models import SourceEvent
from review_service.git import diff_fragments
from conftest import git_command
from test_llm import attach, fake_llm


async def expand_report_section(page, name='Корректность'):
    toggle = page.locator('.tree-section-row').filter(has_text=name).get_by_role('button')
    await expect(toggle).to_be_visible()
    if await toggle.get_attribute('aria-expanded') == 'false':
        await toggle.click()


@pytest.mark.browser
@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 800}, {'width': 390, 'height': 844}])
async def test_shared_sidebar_tree_filters_counts_and_width(client, service, repo, fake_llm, viewport):
    (repo / 'tests').mkdir()
    (repo / 'docs').mkdir()
    (repo / 'tests/test_case.py').write_text('assert True\n')
    (repo / 'docs/guide.md').write_text('original\n')
    git_command(repo, 'add', 'tests', 'docs')
    git_command(repo, 'commit', '-qm', 'Add files')
    (repo / 'example.py').write_text(''.join(f'changed {n}\n' for n in range(80)))
    (repo / 'tests/test_case.py').write_text('assert False\n')
    (repo / 'docs/guide.md').write_text('updated\n')
    (repo / 'staged.py').write_text('staged\n')
    git_command(repo, 'add', 'staged.py')
    await service.refresh()
    llm = await attach(service, fake_llm)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page(viewport=viewport, is_mobile=viewport['width'] < 700)
        await page.add_init_script("sessionStorage.setItem('review-token', 'test-secret');")
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            await expect(page.locator('#sidebar-workspace')).to_be_visible()
            await page.locator('#generate').click()
            await expect(page.get_by_label('Версия отчёта')).to_have_value(re.compile('^report:'), timeout=15000)
            await expect(page.locator('#sidebar-report')).to_be_visible()
            await expect(page.locator('.tree-section-row').first.get_by_role('button')).to_have_attribute('aria-expanded', 'false')
            await page.get_by_role('button', name='Развернуть: Описание').click()
            summary_files = page.locator('#report-tree > .tree-group .tree-entry.level-1')
            code = summary_files.filter(has_text='example.py')
            await expect(code).to_be_visible()
            await expect(code).to_contain_text('+80')
            await expect(code).to_contain_text('−4')
            await expect(page.locator('#report-tree .tree-entry.level-1').filter(has_text='staged.py')).to_have_count(0)
            await expect(page.locator('#report-tree .tree-entry.level-1').filter(has_text='tests/test_case.py')).to_have_count(0)
            await expect(page.locator('#report-tree .tree-entry.level-1').filter(has_text='docs/guide.md')).to_have_count(0)
            await page.locator('[data-file-filter="tests"]').check()
            await expect(summary_files.filter(has_text='tests/test_case.py')).to_be_visible()
            await page.locator('[data-file-filter="docs"]').check()
            await expect(summary_files.filter(has_text='docs/guide.md')).to_be_visible()
            await expand_report_section(page)
            if viewport['width'] > 800:
                splitter = page.get_by_role('separator', name='Ширина левой панели')
                await splitter.focus()
                await page.keyboard.press('ArrowRight')
                after_key = (await page.locator('#sidebar').bounding_box())['width']
                assert after_key > 250
                split_box = await splitter.bounding_box()
                await page.mouse.move(split_box['x'] + split_box['width'] / 2, split_box['y'] + split_box['height'] / 2)
                await page.mouse.down()
                await page.mouse.move(split_box['x'] + split_box['width'] / 2 + 40, split_box['y'] + split_box['height'] / 2)
                await page.mouse.up()
                assert (await page.locator('#sidebar').bounding_box())['width'] > after_key
            await page.reload()
            await expect(page.locator('#sidebar-report')).to_be_visible()
            await expect(page.get_by_role('button', name='Свернуть: Описание')).to_have_attribute('aria-expanded', 'true')
            await expect(page.get_by_role('button', name='Свернуть: Корректность')).to_have_attribute('aria-expanded', 'true')
            await expect(page.locator('[data-file-filter="tests"]')).not_to_be_checked()
            await expect(page.locator('[data-file-filter="docs"]')).not_to_be_checked()
            await expect(code).to_be_visible()
            if viewport['width'] > 800:
                assert (await page.locator('#sidebar').bounding_box())['width'] > 250
            else:
                await expect(page.locator('#sidebar-splitter')).to_be_hidden()
            await page.locator('#sidebar-back').click()
            await expect(page.locator('#sidebar-workspace')).to_be_visible()
            await page.locator('[data-view="overview"]').click()
            await expect(page.locator('#sidebar-report')).to_be_visible()
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            await browser.close()
            await llm.close()


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
            await expand_report_section(page)
            await expect(page.get_by_role('button', name='Сохраняем инвариант', exact=True)).to_be_visible(timeout=15000)
            await page.get_by_role('button', name='Сохраняем инвариант', exact=True).click()
            await expect(page.get_by_role('heading', name='Сохраняем инвариант', exact=True)).to_be_visible()
            await page.get_by_role('button', name='Источник 1').click()
            await expect(page.locator('#dialog')).to_contain_text('Use explicit boundary checks.')
            await page.locator('#dialog-close').click()
            await page.get_by_label('Просмотрено').check()
            await expect(page.locator('.overview-metrics')).to_contain_text('Review 1/1')
            await page.get_by_role('button', name='Спросить об этом').click()
            await page.get_by_label('Вопрос об изменениях').fill('Какие проверки нужны?')
            await page.get_by_role('button', name='Спросить ↑').click()
            await expect(page.locator('#chat-messages')).to_contain_text('Проверка граничных случаев необходима.', timeout=15000)
            await page.screenshot(path=f"test-results/review-{viewport['width']}.png", full_page=True)
            await page.locator('#sidebar-back').click()
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
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}])
async def test_overview_stage_without_edit_and_item_journal(client, service, repo, viewport):
    (repo / 'example.py').write_text('ONE\ntwo\nthree\nfour\n')
    git_command(repo, 'add', 'example.py')
    (repo / 'example.py').write_text('ONE\nTWO\nthree\nfour\n')
    snapshot = await service.refresh()
    fragment = next(f for f in diff_fragments(snapshot) if f['layer'] == 'unstaged')
    report = {'id': 'browser-report', 'created_at': '2026-01-01T00:00:00+00:00',
              'snapshot_id': snapshot.id, 'snapshot_version': snapshot.version,
              'summary': 'Проверить изменения.', 'items': [
                  {'id': 'browser-item', 'section': 'Проверка', 'title': 'Пункт с diff',
                   'explanation': 'Проверить строки.', 'reviewed': False,
                   'fragment_ids': [fragment['id']], 'source_ids': []}], 'findings': []}
    await service.store.put('report', report['id'], report)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page(viewport=viewport, is_mobile=viewport['width'] < 700)
        await page.add_init_script("sessionStorage.setItem('review-token', 'test-secret');")
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            await expand_report_section(page, 'Проверка')
            await page.get_by_role('button', name='Пункт с diff', exact=True).click()
            stage = page.get_by_role('button', name='Stage строк', exact=True)
            await expect(page.get_by_role('button', name='Stage файла', exact=True)).to_be_enabled()
            await expect(page.locator('.overview-editor-host .cm-content').last).to_have_attribute('contenteditable', 'false')
            header = page.locator('.overview-diff-header')
            assert (await header.bounding_box())['height'] < 50
            await page.get_by_label('Staged').check()
            await expect(page.locator('.cm-review-staged-line').first).to_be_visible()
            changed = page.locator('.overview-editor-host .cm-changedText').first
            await expect(changed).to_be_visible()
            assert await changed.evaluate('(node) => getComputedStyle(node).backgroundImage') == 'none'
            await page.locator('.overview-editor-host .cm-merge-b .cm-line').filter(has_text='TWO').dblclick()
            await expect(stage).to_be_enabled()
            await stage.click()
            await expect(page.locator('.overview-notice').filter(has_text='прежнему снимку')).to_have_count(0)
            await expect(page.locator('.overview-journal-row')).to_have_count(1)
            assert git_command(repo, 'show', ':example.py') == b'ONE\nTWO\nthree\nfour\n'
            await page.locator('.overview-editor-host .cm-merge-b .cm-line').filter(has_text='TWO').dblclick()
            unstage = page.get_by_role('button', name='Unstage строк', exact=True)
            await expect(unstage).to_be_enabled()
            await unstage.click()
            await expect(page.locator('.overview-journal-row')).to_have_count(2)
            assert git_command(repo, 'show', ':example.py') == b'ONE\ntwo\nthree\nfour\n'
            await page.locator('.overview-journal-row').first.get_by_role('button', name='Откатить').click()
            await expect(page.locator('.overview-journal-row')).to_have_count(3)
            assert git_command(repo, 'show', ':example.py') == b'ONE\nTWO\nthree\nfour\n'
            await page.get_by_label('Staged').uncheck()
            await page.get_by_role('button', name='Редактировать', exact=True).click()
            editor = page.locator('.overview-editor-host .cm-content').last
            await expect(editor).to_have_attribute('contenteditable', 'true')
            await editor.click()
            await page.keyboard.press('ControlOrMeta+End')
            await page.keyboard.insert_text('extra\n')
            await expect(page.get_by_role('button', name='Сохранить файл')).to_be_enabled()
            await page.get_by_role('button', name='Сохранить файл').click()
            await expect(page.locator('.overview-journal-row')).to_have_count(4)
            await expect(page.locator('.overview-notice').filter(has_text='прежнему снимку')).to_have_count(0)
            assert (repo / 'example.py').read_text().endswith('extra\n')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            await browser.close()


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
            await expect(page.get_by_role('button', name='Первый пункт', exact=True)).to_have_count(0)
            assert not await service.store.list('report')
            gates[0].set()
            await expand_report_section(page, 'Проверка')
            await expect(page.get_by_role('button', name='Первый пункт', exact=True)).to_be_visible()
            await page.get_by_role('button', name='Первый пункт', exact=True).click()
            await expect(page.get_by_role('heading', name='Первый пункт', exact=True)).to_be_visible()
            await expect(page.get_by_label('Просмотрено')).to_have_count(0)
            await expect(page.get_by_role('button', name='Спросить об этом')).to_be_disabled()
            await expect(page.get_by_role('button', name='Stage файла')).to_have_count(0)
            await page.get_by_role('button', name='Источник 1').click()
            await expect(page.locator('#dialog')).to_contain_text('Frozen streaming evidence')
            await page.locator('#dialog-close').click()
            await page.reload()
            await expand_report_section(page, 'Проверка')
            await expect(page.get_by_role('button', name='Первый пункт', exact=True)).to_be_visible()
            await page.get_by_role('button', name='Первый пункт', exact=True).click()
            # Force an SSE disconnect; connect() must resync the saved draft.
            for queue in service.subscribers:
                queue.put_nowait({'type': 'shutdown'})
            await expect(page.locator('#connection')).to_have_text('Переподключение…')
            await expect(page.locator('#connection')).to_have_text('Подключено', timeout=10000)
            await page.locator('.overview-editor-host .cm-editor').first.evaluate('(node) => window.previewEditor = node')
            gates[1].set()
            await expect(page.get_by_role('button', name='Второй пункт', exact=True)).to_be_attached()
            assert await page.locator('.overview-editor-host .cm-editor').first.evaluate('(node) => node === window.previewEditor')
            assert not await service.store.list('report')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=f"test-results/draft-{viewport['width']}.png", full_page=True)
            control['before_finish'].set()
            await expect(page.locator('.draft-status')).to_have_count(0)
            await expand_report_section(page, 'Проверка')
            await expect(page.get_by_role('button', name='Первый пункт', exact=True)).to_be_visible()
            await page.get_by_role('button', name='Первый пункт', exact=True).click()
            await expect(page.get_by_label('Просмотрено')).to_be_enabled()
            await expect(page.get_by_role('button', name='Спросить об этом')).to_be_enabled()
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
            await expand_report_section(page)
            await expect(page.get_by_role('button', name='Сохраняем инвариант', exact=True)).to_be_visible()
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
            await expand_report_section(page)
            await expect(page.get_by_role('button', name='Сохраняем инвариант', exact=True)).to_be_visible()
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

    async def delayed_generate(job_id, *, model):
        await ready.wait()
        return await original_generate(job_id, model=model)

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
            await expand_report_section(page)
            await expect(page.get_by_role('button', name='Сохраняем инвариант', exact=True)).to_be_visible(timeout=15000)
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
    saved = await service.generate('saved')
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
            await expand_report_section(page)
            await expect(page.get_by_role('button', name='Сохраняем инвариант', exact=True)).to_be_visible()
            await expect(page.get_by_label('Версия отчёта')).to_have_value('report:' + saved['report_id'])
            assert failed_responses == [], console_errors
            assert console_errors == []
        finally:
            await browser.close()
            await llm.close()


@pytest.mark.browser
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
async def test_model_selection_streaming_manual_entry_and_reload(client, service, repo, fake_llm, viewport):
    (repo / 'example.py').write_text('changed\n')
    llm = await attach(service, fake_llm)
    control = fake_llm[1]
    control['before_finish'] = asyncio.Event()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        context = await browser.new_context(viewport=viewport, is_mobile=viewport['width'] < 700,
                                            has_touch=viewport['width'] < 700)
        await context.add_init_script("sessionStorage.setItem('review-token', 'test-secret');")
        page = await context.new_page()
        other = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            await other.goto(str(client.make_url('/')))
            await expect(page.get_by_label('Модель для отчётов и чата')).to_have_value('test-model')
            await page.get_by_label('Модель для отчётов и чата').click()
            await expect(page.locator('#model-popup')).to_be_visible()
            await expect(page.locator('#model-options .model-option').filter(has_text='other-model')).to_have_count(1)
            await page.locator('#model-options .model-option').filter(has_text='other-model').click()
            await expect(page.get_by_label('Модель для отчётов и чата')).to_have_value('other-model')
            await page.get_by_role('button', name='Применить', exact=True).click()
            await expect(page.locator('#model-current')).to_contain_text('Сейчас: other-model')
            await expect(other.get_by_label('Модель для отчётов и чата')).to_have_value('other-model')
            await page.locator('#generate').click()
            await expect(page.locator('.draft-status')).to_contain_text('Генерируется')
            await expect(page.locator('.report-model')).to_have_text('Модель: other-model')

            control['models_status'] = 503
            await page.get_by_label('Модель для отчётов и чата').click()
            await page.get_by_role('button', name='Обновить список').click()
            await expect(page.locator('#model-list-status')).to_contain_text('HTTP 503')
            await page.get_by_label('Модель для отчётов и чата').fill('manual-model')
            await page.get_by_role('button', name='Применить', exact=True).click()
            await expect(page.locator('#model-current')).to_contain_text('Сейчас: manual-model')
            control['before_finish'].set()
            await expect(page.locator('.draft-status')).to_have_count(0)
            await expect(page.locator('.report-model')).to_have_text('Модель: other-model')
            await page.locator('#generate').click()
            await expect(page.locator('.report-model')).to_have_text('Модель: manual-model')
            await expect(page.locator('.draft-status')).to_have_count(0)

            await page.locator('#chat-toggle').click()
            await page.get_by_label('Вопрос об изменениях').fill('Что проверить?')
            await page.get_by_role('button', name='Спросить ↑').click()
            await expect(page.locator('.message.assistant')).to_contain_text('manual-model')
            await expect(page.locator('.message.assistant')).to_contain_text('Проверка граничных случаев необходима.')
            await page.locator('#chat-close').click()
            assert [request['model'] for request in control['requests']] == ['other-model', 'manual-model', 'manual-model']

            await page.reload()
            await expect(page.get_by_label('Модель для отчётов и чата')).to_have_value('manual-model')
            await expect(page.locator('.report-model')).to_have_text('Модель: manual-model')
            await page.screenshot(path=f"test-results/model-{viewport['width']}.png", full_page=True)
            await page.get_by_label('Модель для отчётов и чата').click()
            await page.get_by_role('button', name='Из окружения').click()
            await expect(page.get_by_label('Модель для отчётов и чата')).to_have_value('test-model')
            await expect(other.get_by_label('Модель для отчётов и чата')).to_have_value('test-model')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            control['before_finish'].set()
            await browser.close()
            await llm.close()

@pytest.mark.browser
@pytest.mark.parametrize('viewport', [
    {'width': 1440, 'height': 1000},
    {'width': 1280, 'height': 800},
    {'width': 390, 'height': 844},
])
async def test_overview_diff_scroll_split_and_collapsed_chat(client, service, repo, viewport):
    (repo / 'example.py').write_text(''.join(f'changed line {n}\n' for n in range(250)))
    await service.refresh()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('REVIEW_TEST_BROWSER'))
        page = await browser.new_page(viewport=viewport, is_mobile=viewport['width'] < 700,
                                      has_touch=viewport['width'] < 700)
        await page.add_init_script("sessionStorage.setItem('review-token', 'test-secret');")
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(str(client.make_url('/')))
            host = page.locator('.overview-editor-host')
            await expect(host.locator('.cm-line').first).to_be_visible()
            await expect(page.locator('#chat-panel')).to_have_class('chat-panel collapsed')
            scroller = host.locator('.cm-mergeView' if viewport['width'] > 800 else '.cm-scroller').first
            assert await scroller.evaluate('(node) => node.scrollHeight > node.clientHeight')
            box = await host.bounding_box()
            await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(100)
            assert await scroller.evaluate('(node) => node.scrollTop > 0')
            await host.focus()
            await page.keyboard.press('End')
            assert await scroller.evaluate('(node) => node.scrollTop >= node.scrollHeight - node.clientHeight - 2')
            await page.keyboard.press('Home')
            assert await scroller.evaluate('(node) => node.scrollTop == 0')

            splitter = page.get_by_role('separator', name='Разделитель текста и диффа')
            detail = page.locator('.overview-detail')
            initial = (await detail.bounding_box())['height']
            diff_height = (await page.locator('.overview-diff').bounding_box())['height']
            assert abs(initial - diff_height) < 5
            await host.focus()
            await page.keyboard.press('PageDown')
            scroll_before_split = await scroller.evaluate('(node) => node.scrollTop')
            assert scroll_before_split > 0
            await splitter.focus()
            await page.keyboard.press('ArrowDown')
            after_key = (await detail.bounding_box())['height']
            assert after_key > initial
            split_box = await splitter.bounding_box()
            await page.mouse.move(split_box['x'] + split_box['width'] / 2, split_box['y'] + split_box['height'] / 2)
            await page.mouse.down()
            await page.mouse.move(split_box['x'] + split_box['width'] / 2, split_box['y'] + split_box['height'] / 2 + 25)
            await page.mouse.up()
            after_drag = (await detail.bounding_box())['height']
            assert after_drag > after_key
            assert await scroller.evaluate('(node) => node.scrollTop > 0')

            await page.locator('#chat-toggle').click()
            await expect(page.locator('#chat-body')).to_be_visible()
            await page.get_by_label('Вопрос об изменениях').fill('Незавершённый вопрос')
            await page.locator('#chat-close').click()
            await expect(page.locator('#chat-panel')).to_have_class('chat-panel collapsed')
            await expect(page.get_by_label('Вопрос об изменениях')).to_have_value('Незавершённый вопрос')
            await page.reload()
            await expect(host.locator('.cm-line').first).to_be_visible()
            await expect(page.locator('#chat-panel')).to_have_class('chat-panel collapsed')
            assert abs((await detail.bounding_box())['height'] - after_drag) < 5
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            await browser.close()
