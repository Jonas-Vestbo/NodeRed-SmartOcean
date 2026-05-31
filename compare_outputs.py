#!/usr/bin/env python3
"""
Side-by-side parity check: Python data_transformer convert() vs. Node-RED pipeline (via MQTT).
Publish a test file to test/<vendor>/in, read what comes out on test/<vendor>/raw, diff against
Python's convert() output on the same file.

Usage:
    python compare_outputs.py aadi   /path/to/file.xml
    python compare_outputs.py wsense /path/to/file.json
"""
import json, subprocess, sys, time, os
from os import path

DATA_TX = '/home/jonas/Skule/data_transformer'
sys.path.insert(0, DATA_TX)
sys.path.insert(0, path.join(DATA_TX, 'transformer'))
sys.path.insert(0, path.join(DATA_TX, 'transformer/datamodels'))

VENDORS = {
    'aadi':   {'mod': 'transformer.converter.aadi_converter',   'topic_in': 'test/aadi/in',   'topic_out': 'test/aadi/raw'},
    'wsense': {'mod': 'transformer.converter.wsense_converter', 'topic_in': 'test/wsense/in', 'topic_out': 'test/wsense/raw'},
}

def python_output(vendor: str, file_path: str):
    cfg = VENDORS[vendor]
    import importlib
    mod = importlib.import_module(cfg['mod'])
    with open(file_path) as f:
        data = f.read()
    out = mod.convert(data, 'test')
    if out is None:
        return None
    if isinstance(out, str):
        return json.loads(out)
    return out

def nodered_output(vendor: str, file_path: str, timeout=15):
    cfg = VENDORS[vendor]
    sub = subprocess.Popen(
        ['mosquitto_sub', '-h', 'localhost', '-t', cfg['topic_out'], '-C', '1', '-W', str(timeout)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(0.5)  # let sub connect
    pub = subprocess.run(
        ['mosquitto_pub', '-h', 'localhost', '-t', cfg['topic_in'], '-f', file_path, '-q', '1'],
        capture_output=True
    )
    if pub.returncode != 0:
        return None, f'pub failed: {pub.stderr.decode()}'
    try:
        out, err = sub.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        sub.kill()
        return None, 'sub timeout: Node-RED produced nothing'
    if not out.strip():
        return None, 'empty response'
    try:
        return json.loads(out.decode()), None
    except json.JSONDecodeError as e:
        return None, f'not JSON: {e}'

def normalize(obj, drop_keys=None):
    """Sort keys recursively. Optionally drop specific keys at any depth."""
    drop_keys = drop_keys or set()
    if isinstance(obj, dict):
        return {k: normalize(v, drop_keys) for k, v in sorted(obj.items()) if k not in drop_keys}
    if isinstance(obj, list):
        return [normalize(x, drop_keys) for x in obj]
    return obj

def diff(py, nr):
    """Walk both trees and return list of differences."""
    diffs = []
    def walk(a, b, path):
        if type(a) is not type(b):
            diffs.append(f'{path}: TYPE  python={type(a).__name__}({a!r}) nodered={type(b).__name__}({b!r})')
            return
        if isinstance(a, dict):
            ka, kb = set(a), set(b)
            for k in ka - kb:
                diffs.append(f'{path}.{k}: ONLY-IN-PYTHON  ({a[k]!r})')
            for k in kb - ka:
                diffs.append(f'{path}.{k}: ONLY-IN-NODERED ({b[k]!r})')
            for k in ka & kb:
                walk(a[k], b[k], f'{path}.{k}')
        elif isinstance(a, list):
            if len(a) != len(b):
                diffs.append(f'{path}: LEN python={len(a)} nodered={len(b)}')
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f'{path}[{i}]')
        else:
            if a != b:
                diffs.append(f'{path}: VAL python={a!r} nodered={b!r}')
    walk(py, nr, '$')
    return diffs

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    vendor, file_path = sys.argv[1], sys.argv[2]
    print(f'== Comparing {vendor}: {path.basename(file_path)} ==')
    py = python_output(vendor, file_path)
    nr, err = nodered_output(vendor, file_path)
    if py is None:
        print('  Python returned None (error during convert)')
    if err:
        print(f'  Node-RED issue: {err}')
    if py is None or nr is None:
        sys.exit(2 if (py is None) ^ (nr is None) else 0)

    # Normalize: drop null fields from Python (Node-RED omits absents);
    # also drop keys we know diverge by design.
    drop_in_py = {'datapoints'}  # WSense Python adds duplicate 'datapoints'
    def strip_nulls(o):
        if isinstance(o, dict):
            return {k: strip_nulls(v) for k, v in o.items() if v is not None and k not in drop_in_py}
        if isinstance(o, list):
            return [strip_nulls(x) for x in o]
        return o
    pyN = normalize(strip_nulls(py))
    nrN = normalize(nr)

    d = diff(pyN, nrN)
    if not d:
        print('  ✅ outputs match (after dropping nulls and Python-only `datapoints` key)')
        return
    print(f'  ❌ {len(d)} differences:')
    for line in d[:60]:
        print(f'    {line}')
    if len(d) > 60:
        print(f'    ... ({len(d)-60} more)')

if __name__ == '__main__':
    main()
