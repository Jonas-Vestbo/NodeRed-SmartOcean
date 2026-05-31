"""
Shared helpers for Python ↔ Node-RED equivalence tests.

- Python converter invocation
- MQTT publisher / per-vendor subscriber queues
- Normalizer (strip nulls from Python output; drop fields Python adds redundantly)
- Recursive diff
"""
from __future__ import annotations
import json, importlib, sys
from pathlib import Path
from queue import Queue, Empty
from dataclasses import dataclass

# Make data_transformer importable
DATA_TX = Path('/home/jonas/Skule/data_transformer')
TESTDATA = DATA_TX / 'tests/testdata'
for p in (DATA_TX, DATA_TX / 'transformer', DATA_TX / 'transformer/datamodels'):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

import paho.mqtt.client as mqtt


@dataclass(frozen=True)
class Vendor:
    key: str
    py_module: str
    topic_in: str
    topic_out: str
    corpus_root: Path        # walked recursively for *all* corpus files
    timeseries_id: str       # value passed to Python's convert(); should match Node-RED's default
    file_ext: tuple[str, ...] = ('.xml', '.json', '.txt')


VENDORS = {
    'aadi': Vendor(
        key='aadi',
        py_module='transformer.converter.aadi_converter',
        topic_in='test/aadi/in',
        topic_out='test/aadi/raw',
        corpus_root=TESTDATA / 'aadi',
        timeseries_id='ignored',   # AADI converter overwrites with Device.@SessionID internally
        file_ext=('.xml',),
    ),
    'wsense': Vendor(
        key='wsense',
        py_module='transformer.converter.wsense_converter',
        topic_in='test/wsense/in',
        topic_out='test/wsense/raw',
        corpus_root=TESTDATA / 'wsense',
        timeseries_id='wsense',
        file_ext=('.json', '.txt'),
    ),
    'fluorometer': Vendor(
        key='fluorometer',
        py_module='transformer.converter.fluorometer_converter',
        topic_in='test/fluorometer/in',
        topic_out='test/fluorometer/raw',
        corpus_root=TESTDATA / 'fluorometer',
        timeseries_id='ignored',  # converter falls back to payload's device_id
        file_ext=('.json',),
    ),
}


def vendor_files(vendor: Vendor):
    """All corpus files for a vendor, recursive. Returns list of (Path, relative-key)."""
    out = []
    for f in sorted(vendor.corpus_root.rglob('*')):
        if not f.is_file():
            continue
        if not f.suffix.lower() in vendor.file_ext:
            continue
        rel = f.relative_to(vendor.corpus_root)
        out.append((f, str(rel)))
    return out


# ───────────────────────────────────────────────────────────── Python side ──

_INPUT_VALIDATORS = {
    'aadi':   ('transformer.input_validator', 'validate_aadi_format'),
    'wsense': ('transformer.input_validator', 'validate_wsense_format'),
}


def python_convert(vendor: Vendor, raw: str):
    """Returns (obj, error). Mirrors the FULL Python pipeline:
       input-validate (when defined) → convert. Parity is judged against this full chain,
       not against convert() alone — same as the live Python service flow."""
    # 1. Input validation (raises ValidateException on bad input)
    if vendor.key in _INPUT_VALIDATORS:
        vmod_name, vfn_name = _INPUT_VALIDATORS[vendor.key]
        vmod = importlib.import_module(vmod_name)
        try:
            getattr(vmod, vfn_name)(raw)
        except Exception as e:
            return None, f'input-validate {type(e).__name__}: {e}'

    # 2. Vendor-specific conversion
    mod = importlib.import_module(vendor.py_module)
    try:
        out = mod.convert(raw, vendor.timeseries_id)
    except Exception as e:
        return None, f'convert {type(e).__name__}: {e}'
    if out is None:
        return None, 'convert returned None'
    try:
        parsed = json.loads(out) if isinstance(out, str) else out
    except Exception as e:
        return None, f'output not JSON: {e}'
    return parsed, None


def strip_python_quirks(obj):
    """Drop only the duplicate `datapoints` key that wsense_converter writes alongside `data`.
    Node-RED now emits nulls for optional pydantic fields explicitly, so we no longer strip them."""
    DROP = {'datapoints'}
    def w(o):
        if isinstance(o, dict):
            return {k: w(v) for k, v in o.items() if k not in DROP}
        if isinstance(o, list):
            return [w(x) for x in o]
        return o
    return w(obj)


# ────────────────────────────────────────────────────────── MQTT round-trip ──

class MqttPair:
    """One publisher + one persistent per-vendor subscriber with a queue."""
    def __init__(self, host='localhost', port=1883):
        self.host, self.port = host, port
        self.pub = mqtt.Client(client_id='parity-pub')
        self.pub.connect(host, port)
        self.pub.loop_start()
        self.queues: dict[str, Queue] = {}
        self.subs: list[mqtt.Client] = []

    def subscribe(self, vendor: Vendor):
        q: Queue = Queue()
        self.queues[vendor.key] = q
        c = mqtt.Client(client_id=f'parity-sub-{vendor.key}')
        c.on_message = lambda cli, ud, msg, q=q: q.put(msg.payload)
        c.connect(self.host, self.port)
        c.subscribe(vendor.topic_out, qos=1)
        c.loop_start()
        self.subs.append(c)

    def drain(self, vendor: Vendor):
        q = self.queues[vendor.key]
        while True:
            try:
                q.get_nowait()
            except Empty:
                break

    def round_trip(self, vendor: Vendor, raw: str, timeout=15):
        self.drain(vendor)
        self.pub.publish(vendor.topic_in, payload=raw, qos=1).wait_for_publish(timeout=5)
        try:
            payload = self.queues[vendor.key].get(timeout=timeout)
        except Empty:
            return None, 'timeout: Node-RED did not respond on ' + vendor.topic_out
        try:
            return json.loads(payload), None
        except json.JSONDecodeError as e:
            return None, f'response was not valid JSON: {e}'

    def close(self):
        self.pub.loop_stop(); self.pub.disconnect()
        for c in self.subs:
            c.loop_stop(); c.disconnect()


# ─────────────────────────────────────────────────────────────────── Diff ──

def _is_numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def diff(a, b, path='$'):
    out = []
    def walk(x, y, p):
        # int/float equivalence: Python's pydantic keeps `3622.0`; JS JSON drops `.0`. Same number.
        if _is_numeric(x) and _is_numeric(y):
            if x != y:
                out.append((p, x, y))
            return
        if type(x) is not type(y):
            out.append((p, x, y))
            return
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                if k not in x:   out.append((f'{p}.{k}', None, y[k]))
                elif k not in y: out.append((f'{p}.{k}', x[k], None))
                else:            walk(x[k], y[k], f'{p}.{k}')
        elif isinstance(x, list):
            if len(x) != len(y):
                out.append((f'{p}.len', len(x), len(y)))
            for i, (av, bv) in enumerate(zip(x, y)):
                walk(av, bv, f'{p}[{i}]')
        else:
            if x != y:
                out.append((p, x, y))
    walk(a, b, path)
    return out


# ──────────────────────────────────────────────────────────────── Preflight ──

def preflight():
    """Verify a broker is reachable and that Node-RED responds on the AADI topic.
    Returns (ok: bool, reason: str)."""
    import socket
    try:
        with socket.create_connection(('localhost', 1883), timeout=2):
            pass
    except OSError as e:
        return False, f'cannot reach mosquitto on localhost:1883 ({e})'

    pair = MqttPair()
    pair.subscribe(VENDORS['aadi'])
    sample = VENDORS['aadi'].corpus_root / 'valid_data/aaditestdata.xml'
    raw = sample.read_text()
    nr, err = pair.round_trip(VENDORS['aadi'], raw, timeout=5)
    pair.close()
    if nr is None:
        return False, f'broker is up but Node-RED is not responding on test/aadi/raw — is the latest flows.json loaded? ({err})'
    return True, 'ok'
