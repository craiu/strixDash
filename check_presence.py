"""Exercise browser-driven polling against the deployed dashboard."""
import json
import os
import time
import urllib.request
from playwright.sync_api import sync_playwright

URL = os.environ.get('STRIXDASH_URL', 'http://127.0.0.1:3000').rstrip('/')


def status():
    # Diagnostic reads deliberately do not acquire a viewer lease.
    with urllib.request.urlopen(URL + '/api/status', timeout=5) as response:
        return json.load(response)


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = status()
        if predicate(snapshot):
            return snapshot
        time.sleep(.3)
    raise AssertionError('Timed out waiting for monitoring state: ' + json.dumps(snapshot.get('monitoring')))


initial = status()['monitoring']
print('Initial: ' + json.dumps(initial), flush=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('STRIXDASH_BROWSER_CHANNEL', 'msedge'), headless=True)
    first, second = browser.new_page(), browser.new_page()
    first.goto(URL)
    second.goto(URL)
    active = wait_for(lambda s: s['monitoring']['viewers'] >= 2 and s['monitoring']['model_sampling'])
    before = active['monitoring']['model_requests_total']
    wait_for(lambda s: s['monitoring']['model_requests_total'] > before)
    first.goto('about:blank')
    time.sleep(3)
    remaining = status()['monitoring']
    assert remaining['viewers'] >= 1 and remaining['model_sampling'], remaining
    second.goto('about:blank')
    if initial['viewers'] == 0:
        paused = wait_for(lambda s: not s['monitoring']['model_sampling'])
        requests = paused['monitoring']['model_requests_total']
        hardware_samples = paused['monitoring']['hardware_samples_total']
        history_samples = paused['monitoring']['history_samples_total']
        time.sleep(5)
        still_paused = status()['monitoring']
        assert still_paused['model_requests_total'] == requests, still_paused
        assert not still_paused['model_sampling'], still_paused
        assert not still_paused['hardware_sampling'], still_paused
        assert still_paused['hardware_samples_total'] == hardware_samples, still_paused
        assert still_paused['history_samples_total'] == history_samples, still_paused
        print('No browsers: zero model requests, hardware reads, or history writes across 5 seconds; ' + json.dumps(still_paused), flush=True)
        first.goto(URL)
        resumed = wait_for(lambda s: s['monitoring']['model_sampling'] and s['monitoring']['model_requests_total'] > requests)
        assert resumed['monitoring']['hardware_sampling'], resumed
        assert resumed['monitoring']['hardware_samples_total'] > hardware_samples, resumed
        print('Reopened browser: sampling resumed; ' + json.dumps(resumed['monitoring']), flush=True)
        first.goto('about:blank')
    else:
        print('Other viewers present; idle test skipped to preserve their live monitoring.', flush=True)
    browser.close()
print('Browser presence checks passed.', flush=True)
