"""Browser smoke checks against the deployed dashboard, no model requests."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).parent
URL = os.environ.get('STRIXDASH_URL', 'http://127.0.0.1:3000').rstrip('/')

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('STRIXDASH_BROWSER_CHANNEL', 'msedge'), headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors, console_errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
    page.goto(URL, wait_until='networkidle')
    expect(page.locator('#model-subtitle')).to_contain_text('v0.9.1')
    expect(page.locator('#model-state-label')).to_have_text('Online', timeout=10000)
    page.screenshot(path=str(ROOT / 'desktop.png'), full_page=True, animations='disabled')
    print(json.dumps({'desktop_title': page.title(), 'body_excerpt': page.locator('body').inner_text()[:1800]}))
    for name in ('Performance', 'Resources', 'Activity', 'Overview'):
        page.get_by_role('button', name=name, exact=True).first.click()
        assert page.locator('.page.is-active').count() == 1
        expect(page.locator('.nav-item[aria-current="page"]')).to_contain_text(name)
        if name != 'Overview':
            page.screenshot(path=str(ROOT / (name.lower() + '.png')), full_page=True, animations='disabled')
    page.locator('#theme-toggle').click()
    page.screenshot(path=str(ROOT / 'light.png'), full_page=True, animations='disabled')
    page.locator('#theme-toggle').click()
    page.get_by_role('button', name='7d', exact=True).first.click()
    page.get_by_role('button', name='1h', exact=True).first.click()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Desktop overflow'
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(ROOT / 'mobile.png'), full_page=True, animations='disabled')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
    page.route('**/api/status', lambda route: route.fulfill(status=503, content_type='application/json', body='{}'))
    page.wait_for_timeout(3000)
    expect(page.locator('#stale-banner')).to_be_visible()
    page.unroute('**/api/status')
    page.wait_for_timeout(3000)
    expect(page.locator('#stale-banner')).to_be_hidden()
    print(json.dumps({'page_errors': errors, 'console_errors': console_errors}))
    assert not errors, errors
    assert all('503' in message for message in console_errors), console_errors
    browser.close()
