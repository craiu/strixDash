#!/usr/bin/env python3
"""Read-only single-node Qwen/AMD dashboard. Python standard library only."""
import argparse
from contextlib import contextmanager
from collections import deque
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


def parse_prometheus(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^([a-zA-Z_:][\w:]*)(?:\{.*\})?\s+([^\s]+)', line)
        if match:
            try:
                value = float(match[2])
                if math.isfinite(value):
                    result[match[1]] = value
            except ValueError:
                pass
    return result


def counter_rate(current, previous, elapsed):
    if current is None or previous is None or not all(math.isfinite(v) for v in (current, previous, elapsed)) or elapsed <= 0 or current < previous:
        return None
    return (current - previous) / elapsed


def safe_ratio(numerator, denominator):
    if numerator is None or denominator is None or not all(math.isfinite(v) for v in (numerator, denominator)) or denominator <= 0:
        return None
    return numerator / denominator


def read_number(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.lock = threading.Lock()
        with database(self.db_path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS samples (timestamp REAL PRIMARY KEY, output_tps REAL, cpu_percent REAL, gpu_percent REAL, temp_c REAL, kv_ratio REAL, active REAL, ram_used REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp REAL, level TEXT, message TEXT)')
        self.count = 0

    def add_sample(self, snapshot):
        host, model = snapshot['host'], snapshot['model']
        values = (snapshot['timestamp'], model.get('output_tps'), host.get('cpu_percent'), host.get('gpu', {}).get('busy_percent'), host.get('gpu', {}).get('temp_c'), model.get('kv_ratio'), model.get('active'), host.get('memory', {}).get('used'))
        with self.lock, database(self.db_path) as db:
            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?)', values)
            self.count += 1
            if self.count % 300 == 1:
                db.execute('DELETE FROM samples WHERE timestamp < ?', (time.time() - 7 * 86400,))

    def history(self, range_seconds):
        start = time.time() - range_seconds
        bucket = max(2, math.ceil(range_seconds / 600))
        fields = ['output_tps', 'cpu_percent', 'gpu_percent', 'temp_c', 'kv_ratio', 'active', 'ram_used']
        with self.lock, database(self.db_path) as db:
            rows = db.execute('SELECT MAX(timestamp), ' + ', '.join('AVG(' + field + ')' for field in fields) + ' FROM samples WHERE timestamp >= ? GROUP BY CAST(timestamp / ? AS INTEGER) ORDER BY MAX(timestamp)', (start, bucket)).fetchall()
        return [dict(zip(['timestamp'] + fields, row)) for row in rows][-600:]

    def add_event(self, level, message):
        with self.lock, database(self.db_path) as db:
            db.execute('INSERT INTO events(timestamp,level,message) VALUES (?,?,?)', (time.time(), level, message))
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)')

    def events(self):
        with self.lock, database(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT timestamp,level,message FROM events ORDER BY id DESC LIMIT 200')]


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return result.stdout + result.stderr if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


class ViewerPresence:
    """Short browser leases expire even when a tab cannot send its leave beacon."""
    def __init__(self, ttl=20):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.leases = {}

    def touch(self, viewer_id, now=None):
        if not isinstance(viewer_id, str) or re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', viewer_id) is None:
            return False
        now = time.monotonic() if now is None else now
        with self.lock:
            self.leases = {key: expiry for key, expiry in self.leases.items() if expiry > now}
            if viewer_id not in self.leases and len(self.leases) >= 256:
                return False
            self.leases[viewer_id] = now + self.ttl
        return True

    def release(self, viewer_id):
        with self.lock:
            self.leases.pop(viewer_id, None)

    def count(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self.leases = {key: expiry for key, expiry in self.leases.items() if expiry > now}
            return len(self.leases)


class Collector:
    def __init__(self, store, model_url):
        self.store, self.model_url = store, model_url.rstrip('/')
        self.lock = threading.Lock()
        self.viewers = ViewerPresence()
        self.model_paused = True
        self.model_requests_total = 0
        self.model_last_sample_at = None
        self.hardware_samples_total = 0
        self.history_samples_total = 0
        self.snapshot = {'timestamp': time.time(), 'host': {}, 'model': {}, 'sources': {}}
        self.prev_cpu = self.prev_net = self.prev_metrics = None
        self.prev_time = self.prev_monotonic = None
        self.last_generation = None
        self.output_window = deque()
        self.last_decode = self.last_prefill = None
        self.last_decode_at = self.last_prefill_at = None
        self.health = {}
        self.last_health_check = 0
        self.health_last_ok = None
        self.cache = {}
        self.last_cache_check = 0
        self.cache_last_ok = None
        self.source_state = {}
        self.allocations = None
        self.allocations_source = None
        self.last_metadata = 0
        self.service = {}
        self.log_since = time.time()
        self.log_seen = set()
        self.last_service_state = None
        self.stopping = threading.Event()

    def fetch(self, path, as_json=True):
        self.model_requests_total += 1
        with urllib.request.urlopen(self.model_url + path, timeout=1.5) as response:
            body = response.read(262144).decode()
        return json.loads(body) if as_json else body

    def source(self, name, ok, error=None):
        old = self.source_state.get(name, {})
        if old.get('ok') is not None and old['ok'] != ok:
            self.store.add_event('info' if ok else 'warning', name.title() + (' connection restored' if ok else ' metrics unavailable'))
        self.source_state[name] = {'ok': ok, 'last_ok': time.time() if ok else old.get('last_ok'), 'error': error}

    def hardware(self, elapsed):
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            mem[key] = int(value.split()[0]) * 1024
        cpu = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
        cpu_total, cpu_idle = sum(cpu), cpu[3] + cpu[4]
        cpu_percent = None
        if self.prev_cpu:
            total_delta, idle_delta = cpu_total - self.prev_cpu[0], cpu_idle - self.prev_cpu[1]
            cpu_percent = max(0, min(100, 100 * (1 - idle_delta / total_delta))) if total_delta > 0 else None
        self.prev_cpu = (cpu_total, cpu_idle)
        network = {}
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            name, values = line.split(':')
            if name.strip() in ('eno1', 'ctidao'):
                numbers = values.split()
                network[name.strip()] = {'rx': int(numbers[0]), 'tx': int(numbers[8])}
        rates = {}
        for name, counters in network.items():
            previous = (self.prev_net or {}).get(name, {})
            rates[name] = {direction + '_bps': counter_rate(counters[direction], previous.get(direction), elapsed) for direction in ('rx', 'tx')}
        self.prev_net = network
        gpu = {}
        for device in Path('/sys/class/drm').glob('card[0-9]*/device'):
            try:
                if (device / 'vendor').read_text().strip() != '0x1002':
                    continue
            except OSError:
                continue
            gpu['busy_percent'] = read_number(device / 'gpu_busy_percent')
            for name in ('gtt', 'vram'):
                for field in ('used', 'total'):
                    gpu[name + '_' + field] = read_number(device / ('mem_info_' + name + '_' + field))
            for sensor in (device / 'hwmon').glob('hwmon*'):
                for key, filename, scale in [('temp_c', 'temp1_input', 1000), ('power_w', 'power1_average', 1e6), ('clock_mhz', 'freq1_input', 1e6)]:
                    value = read_number(sensor / filename)
                    gpu[key] = value / scale if value is not None else None
            break
        disk = shutil.disk_usage(Path(__file__).resolve().parent)
        return {'cpu_percent': cpu_percent, 'load': list(os.getloadavg()), 'uptime_seconds': float(Path('/proc/uptime').read_text().split()[0]), 'memory': {'total': mem['MemTotal'], 'available': mem['MemAvailable'], 'used': mem['MemTotal'] - mem['MemAvailable'], 'swap_used': mem['SwapTotal'] - mem['SwapFree'], 'swap_total': mem['SwapTotal']}, 'gpu': gpu, 'disk': dict(zip(('total', 'used', 'free'), disk)), 'network': {**rates.get('eno1', {}), 'interfaces': rates}}

    def metadata(self, now):
        if now - self.last_metadata < 15:
            return
        self.last_metadata = now
        state_text = command(['systemctl', 'show', 'qwen.service', '-p', 'ActiveState', '-p', 'ActiveEnterTimestamp', '-p', 'NRestarts'])
        self.service = dict(line.split('=', 1) for line in state_text.splitlines() if '=' in line)
        state = self.service.get('ActiveState')
        if state and state != self.last_service_state:
            self.store.add_event('info' if state == 'active' else 'warning', 'Qwen service: ' + state)
            self.last_service_state = state
        if self.allocations is None:
            logs = command(['podman', 'logs', '--tail', '2000', 'qwen-flash'])
            found = re.search(r'memory: ([\d.]+) GiB of weights locked in RAM, ([\d.]+) GiB of KV pool, ([\d.]+) GiB of working memory', logs)
            if found:
                self.allocations = dict(zip(('weights_gib', 'kv_gib', 'working_gib'), map(float, found.groups())))
                self.allocations_source = 'Engine startup report; observed ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.log_since))
        logs = command(['podman', 'logs', '--timestamps', '--since', since, '--tail', '200', 'qwen-flash'])
        self.log_since = now
        for line in logs.splitlines():
            stamp = line.split(' ', 1)[0]
            kind = None
            if 'client disconnected mid-stream' in line:
                kind = ('warning', 'Client disconnected during a streaming response')
            elif re.search(r'HTTP/1\.1" [45]\d\d', line):
                code = re.search(r'HTTP/1\.1" ([45]\d\d)', line)[1]
                kind = ('warning', 'API returned HTTP ' + code)
            elif 'out of memory' in line.lower():
                kind = ('error', 'Runtime reported an out-of-memory condition')
            if kind and (stamp, kind) not in self.log_seen:
                self.store.add_event(*kind)
                self.log_seen.add((stamp, kind))
        if len(self.log_seen) > 1000:
            self.log_seen.clear()

    def sample(self):
        now, monotonic = time.time(), time.monotonic()
        elapsed = monotonic - self.prev_monotonic if self.prev_monotonic else 0
        viewer_count = self.viewers.count()
        if not viewer_count:
            if not self.model_paused:
                self.store.add_event('info', 'Monitoring paused: no dashboard viewers')
            self.model_paused = True
            self.prev_metrics = None
            self.output_window.clear()
            self.last_decode = self.last_prefill = None
            self.last_decode_at = self.last_prefill_at = None
            self.last_generation = None
            previous_model = self.snapshot.get('model', {})
            model = {key: previous_model.get(key) for key in ('id', 'version', 'context', 'slots', 'kv_positions', 'allocations', 'allocations_source')}
            model.update({'online': None, 'sampling_paused': True})
            self.source_state['model'] = {'ok': None, 'paused': True, 'last_ok': self.source_state.get('model', {}).get('last_ok'), 'error': None}
            self.source_state['hardware'] = {'ok': None, 'paused': True, 'last_ok': self.source_state.get('hardware', {}).get('last_ok'), 'error': None}
            snapshot = {'timestamp': now, 'host': {}, 'model': model, 'sources': dict(self.source_state), 'monitoring': {'viewers': 0, 'model_sampling': False, 'hardware_sampling': False, 'model_requests_total': self.model_requests_total, 'model_last_sample_at': self.model_last_sample_at, 'hardware_samples_total': self.hardware_samples_total, 'history_samples_total': self.history_samples_total}}
            with self.lock:
                self.snapshot = snapshot
            self.prev_cpu = self.prev_net = None
            self.prev_time = self.prev_monotonic = None
            return
        if self.model_paused:
            self.store.add_event('info', 'Monitoring resumed: dashboard connected')
            self.last_health_check = self.last_cache_check = self.last_metadata = 0
            self.model_paused = False
        self.hardware_samples_total += 1
        try:
            host = self.hardware(elapsed)
            self.source('hardware', True)
        except Exception:
            host = {}
            self.source('hardware', False, 'Hardware collection failed')
        model = {'online': False}
        try:
            metrics = parse_prometheus(self.fetch('/metrics', False))
            if not metrics:
                raise ValueError('Empty metrics')
            active_now = metrics.get('llamacpp:requests_processing')
            if active_now == 0 and now - self.last_cache_check >= 15:
                self.last_cache_check = now
                try:
                    self.cache = self.fetch('/cache')
                    self.cache_last_ok = now
                except Exception:
                    pass
            cache = self.cache if self.cache_last_ok is not None and now - self.cache_last_ok <= 45 else {}
            if now - self.last_health_check >= 30:
                self.last_health_check = now
                try:
                    self.health = self.fetch('/health')
                    self.health_last_ok = now
                except Exception:
                    pass
            health = self.health
            prefix = 'llamacpp:'
            if metrics.get(prefix + 'predicted_tokens_seconds', 0) > 0:
                self.last_decode = metrics[prefix + 'predicted_tokens_seconds']
                self.last_decode_at = now
            if metrics.get(prefix + 'prompt_tokens_seconds', 0) > 0:
                self.last_prefill = metrics[prefix + 'prompt_tokens_seconds']
                self.last_prefill_at = now
            output = metrics.get(prefix + 'tokens_predicted_total')
            previous = self.prev_metrics or {}
            recent_rate = counter_rate(output, previous.get(prefix + 'tokens_predicted_total'), elapsed)
            if recent_rate is not None and recent_rate > 0:
                self.last_generation = now
            if output is not None:
                if self.output_window and output < self.output_window[-1][1]:
                    self.output_window.clear()
                self.output_window.append((monotonic, output))
                while len(self.output_window) > 2 and self.output_window[1][0] < monotonic - 60:
                    self.output_window.popleft()
            output_tps = counter_rate(output, self.output_window[0][1], monotonic - self.output_window[0][0]) if self.output_window else None
            active = metrics.get(prefix + 'requests_processing')
            engine_ready = health.get('engine', {}).get('responds', False) and health.get('status') == 'ok' and self.health_last_ok is not None and now - self.health_last_ok <= 90
            model = {'online': engine_ready or (active is not None and active > 0), 'id': health.get('model'), 'version': health.get('version', {}).get('engine'), 'context': health.get('context'), 'slots': health.get('slots'), 'kv_positions': health.get('kv_pool_positions'), 'active': active, 'queued': metrics.get(prefix + 'requests_deferred'), 'kv_used': metrics.get(prefix + 'kv_cache_tokens'), 'kv_ratio': metrics.get(prefix + 'kv_cache_usage_ratio'), 'completed': metrics.get('halogen:requests_total'), 'prompt_tokens': metrics.get(prefix + 'prompt_tokens_total'), 'output_tokens': output, 'output_tps': output_tps, 'prefill_tps': metrics.get(prefix + 'prompt_tokens_seconds'), 'decode_tps': metrics.get(prefix + 'predicted_tokens_seconds'), 'last_generation_at': self.last_generation, 'cache_hit_ratio': safe_ratio(cache.get('hits'), (cache.get('hits', 0) + cache.get('misses', 0))), 'cache_bytes': cache.get('bytes'), 'draft_accept_ratio': safe_ratio(metrics.get('halogen:draft_tokens_accepted_total'), metrics.get('halogen:draft_tokens_total')), 'health_measured_at': self.health_last_ok}
            self.prev_metrics = metrics
            self.source('model', model['online'], None if model['online'] else 'Engine not ready')
        except Exception:
            self.prev_metrics = None
            self.output_window.clear()
            self.source('model', False, 'Model endpoint unavailable')
        self.metadata(now)
        model.update({'service': self.service, 'allocations': self.allocations, 'allocations_source': self.allocations_source})
        if model.get('online'):
            model.update({'decode_tps': self.last_decode, 'prefill_tps': self.last_prefill, 'decode_measured_at': self.last_decode_at, 'prefill_measured_at': self.last_prefill_at, 'output_window_seconds': monotonic - self.output_window[0][0] if self.output_window else 0})
            processed = model.get('prompt_tokens')
            reused = metrics.get('halogen:prompt_tokens_cached_total')
            model['cache_request_hit_ratio'] = model.get('cache_hit_ratio')
            model['cache_hit_ratio'] = safe_ratio(reused, processed + reused) if processed is not None and reused is not None else None
        model['sampling_paused'] = False
        self.model_last_sample_at = now
        self.history_samples_total += 1
        snapshot = {'timestamp': now, 'host': host, 'model': model, 'sources': dict(self.source_state), 'monitoring': {'viewers': viewer_count, 'model_sampling': True, 'hardware_sampling': True, 'model_requests_total': self.model_requests_total, 'model_last_sample_at': self.model_last_sample_at, 'hardware_samples_total': self.hardware_samples_total, 'history_samples_total': self.history_samples_total}}
        self.store.add_sample(snapshot)
        with self.lock:
            self.snapshot = snapshot
        self.prev_time, self.prev_monotonic = now, monotonic

    def run(self):
        self.store.add_event('info', 'strixDash collector started')
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                self.sample()
            except Exception as error:
                print('Collector cycle failed: ' + type(error).__name__, flush=True)
            self.stopping.wait(max(0.1, 2 - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3000)
    parser.add_argument('--model-url', default='http://127.0.0.1:8888')
    parser.add_argument('--data-dir', default=str(Path(__file__).parent / 'data'))
    args = parser.parse_args()
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / 'history.sqlite3')
    collector = Collector(store, args.model_url)
    threading.Thread(target=collector.run, daemon=True).start()
    web = (Path(__file__).parent / 'web').resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/api/status':
                viewer_id = self.headers.get('X-StrixDash-Viewer')
                if viewer_id and not collector.viewers.touch(viewer_id):
                    self.send_error(400, 'Invalid viewer identifier')
                    return
                with collector.lock:
                    self.send_json(collector.snapshot)
                return
            if url.path == '/api/history':
                selected = parse_qs(url.query).get('range', ['1h'])[0]
                self.send_json({'points': store.history({'1h': 3600, '24h': 86400, '7d': 604800}.get(selected, 3600))})
                return
            if url.path == '/api/events':
                self.send_json({'events': store.events()})
                return
            path = (web / ('index.html' if url.path == '/' else url.path.lstrip('/'))).resolve()
            if not path.is_relative_to(web) or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urlsplit(self.path).path != '/api/viewer/leave':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 80:
                    raise ValueError()
                viewer_id = self.rfile.read(length).decode('ascii')
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            collector.viewers.release(viewer_id)
            self.send_json({'ok': True})

        def send_json(self, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *values):
            if values and str(values[1]) != '200':
                print('HTTP request status ' + str(values[1]), flush=True)

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print(f'strixDash listening on {args.bind}:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
