#!/usr/bin/env python3
"""
Dump Python and Node-RED conversions for every file in each vendor's valid_data corpus,
side-by-side, so you can `diff -r` them or commit them as fixtures.

Output layout:
    snapshots/
    ├── python/
    │   ├── aadi/aaditestdata.json
    │   └── wsense/payload.json
    └── nodered/
        ├── aadi/aaditestdata.json
        └── wsense/payload.json

Usage:
    ./snapshot.py                  # generate snapshots and print summary
    ./snapshot.py --diff           # generate + show per-file diff summary
    ./snapshot.py --only wsense    # one vendor only
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from parity import VENDORS, MqttPair, python_convert, strip_python_quirks, diff, preflight


SNAP_DIR = HERE / 'snapshots'


def write_json(target: Path, obj):
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w') as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write('\n')


NR_TIMEOUT = 4.0


def generate(only: str | None = None):
    """Walk the corpora, write both sides to disk (snapshot dir mirrors the source layout)."""
    from parity import vendor_files

    if only and only not in VENDORS:
        sys.exit(f'unknown vendor: {only!r}')

    ok, reason = preflight()
    if not ok:
        sys.exit(f'preflight failed: {reason}')

    pair = MqttPair()
    for v in VENDORS.values():
        pair.subscribe(v)

    counts = {'total': 0, 'py_ok': 0, 'py_err': 0, 'nr_ok': 0, 'nr_err': 0}
    try:
        for key, vendor in VENDORS.items():
            if only and key != only:
                continue
            files = vendor_files(vendor)
            print(f'\n=== {key} ({len(files)} files) ===')
            for i, (src, rel) in enumerate(files):
                raw = src.read_text(errors='replace')
                snap_rel = Path(key) / Path(rel).with_suffix('.json')
                py_path = SNAP_DIR / 'python'  / snap_rel
                nr_path = SNAP_DIR / 'nodered' / snap_rel

                py_obj, py_err = python_convert(vendor, raw)
                if py_err:
                    counts['py_err'] += 1
                    write_json(py_path, {'__error__': py_err})
                else:
                    counts['py_ok'] += 1
                    write_json(py_path, strip_python_quirks(py_obj))

                nr_obj, nr_err = pair.round_trip(vendor, raw, timeout=NR_TIMEOUT)
                if nr_err:
                    counts['nr_err'] += 1
                    write_json(nr_path, {'__error__': nr_err})
                else:
                    counts['nr_ok'] += 1
                    write_json(nr_path, nr_obj)

                counts['total'] += 1
                if counts['total'] % 100 == 0:
                    print(f'  ... {counts["total"]}/{sum(len(vendor_files(VENDORS[k])) for k in VENDORS if not only or k == only)} files')
    finally:
        pair.close()

    print(f"\nWrote {counts['total']} pairs to {SNAP_DIR}/")
    print(f"  python: {counts['py_ok']} ok, {counts['py_err']} errored")
    print(f"  nodered: {counts['nr_ok']} ok, {counts['nr_err']} errored")
    return counts


def compare(only: str | None = None):
    """Walk the dumped snapshots, report per-vendor outcome counts.

    Outcomes per file:
      - both-error: ✅ parity (Python errored, Node-RED errored)
      - both-ok-match: ✅ parity (both succeeded with identical output)
      - both-ok-differ: ❌ same path, different content
      - divergence: ❌ one succeeded, the other didn't
    """
    py_root = SNAP_DIR / 'python'
    nr_root = SNAP_DIR / 'nodered'
    if not py_root.exists() or not nr_root.exists():
        sys.exit('snapshots/ does not exist — run generate first')

    def is_err(obj): return isinstance(obj, dict) and '__error__' in obj

    by_vendor = {}
    for py_file in sorted(py_root.rglob('*.json')):
        rel = py_file.relative_to(py_root)
        vendor = rel.parts[0]
        if only and vendor != only:
            continue
        nr_file = nr_root / rel
        py = json.loads(py_file.read_text())
        nr = json.loads(nr_file.read_text()) if nr_file.exists() else {'__error__': 'snapshot missing'}

        stats = by_vendor.setdefault(vendor, {
            'both_error': 0, 'both_ok_match': 0, 'both_ok_differ': 0,
            'divergence': 0, 'examples': []
        })

        if is_err(py) and is_err(nr):
            stats['both_error'] += 1
        elif is_err(py) and not is_err(nr):
            stats['divergence'] += 1
            if len(stats['examples']) < 3:
                stats['examples'].append((str(rel), 'python errored but node-red produced output', None))
        elif not is_err(py) and is_err(nr):
            stats['divergence'] += 1
            if len(stats['examples']) < 3:
                stats['examples'].append((str(rel), f'node-red errored: {nr["__error__"]}', None))
        else:
            diffs = diff(py, nr)
            if not diffs:
                stats['both_ok_match'] += 1
            else:
                stats['both_ok_differ'] += 1
                if len(stats['examples']) < 3:
                    stats['examples'].append((str(rel), f'{len(diffs)} content diffs', diffs[:5]))

    overall_ok = True
    for vendor, s in by_vendor.items():
        total = s['both_error'] + s['both_ok_match'] + s['both_ok_differ'] + s['divergence']
        ok = s['both_ok_differ'] == 0 and s['divergence'] == 0
        overall_ok = overall_ok and ok
        bar = '✅' if ok else '❌'
        print(f"{bar} {vendor:8s} {total:5d} files: "
              f"{s['both_ok_match']:5d} match, "
              f"{s['both_error']:4d} both-errored, "
              f"{s['both_ok_differ']:4d} content-differ, "
              f"{s['divergence']:4d} divergence")
        for rel, summary, head in s['examples']:
            print(f'    · {rel}: {summary}')
            if head:
                for p, pv, nv in head:
                    print(f'        {p}: py={pv!r} nr={nv!r}')
    return overall_ok


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--diff', action='store_true', help='also print per-file diff summary')
    ap.add_argument('--only', help='restrict to one vendor (aadi, wsense, fluorometer)')
    args = ap.parse_args()

    t0 = time.time()
    generate(only=args.only)
    if args.diff:
        print(f'\n--- diff summary ---')
        compare(only=args.only)
    print(f'\nelapsed: {time.time() - t0:.1f}s')
