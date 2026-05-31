"""Python ↔ Node-RED equivalence across the full vendor corpus.

For each input file we run Python's convert() and Node-RED's pipeline (via MQTT).
Four outcomes:
  - both succeed → diff outputs; fail if any
  - both error   → ✅ (parity)
  - one succeeds, the other doesn't → ❌ (divergence)
"""
import pytest
from parity import VENDORS, python_convert, strip_python_quirks, diff


# Node-RED's pipeline error path doesn't publish anything to /raw, so for files
# Python errors on we wait this long for Node-RED before declaring "Node-RED also errored".
NR_TIMEOUT = 4.0


def test_parity(vendor_key, filename, mqtt_pair):
    vendor = VENDORS[vendor_key]
    raw = filename.read_text()

    py_obj, py_err = python_convert(vendor, raw)
    nr_obj, nr_err = mqtt_pair.round_trip(vendor, raw, timeout=NR_TIMEOUT)

    if py_err and nr_err:
        return                                              # ✅ both errored

    if py_err and not nr_err:
        pytest.fail(f'Python errored ({py_err}) but Node-RED produced output')
    if nr_err and not py_err:
        pytest.fail(f'Python succeeded but Node-RED did not respond ({nr_err})')

    py_norm = strip_python_quirks(py_obj)
    diffs = diff(py_norm, nr_obj)
    if diffs:
        lines = [f'  {p}: py={pv!r} nr={nv!r}' for p, pv, nv in diffs[:25]]
        if len(diffs) > 25:
            lines.append(f'  ... ({len(diffs) - 25} more)')
        pytest.fail(f'{len(diffs)} differences:\n' + '\n'.join(lines))
