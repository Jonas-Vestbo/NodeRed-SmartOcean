import pytest
from pathlib import Path
from parity import VENDORS, MqttPair, preflight


def pytest_collection_modifyitems(config, items):
    """Run preflight once and skip all tests with a clear reason if it fails."""
    ok, reason = preflight()
    if not ok:
        skip = pytest.mark.skip(reason=f'preflight failed: {reason}')
        for it in items:
            it.add_marker(skip)


@pytest.fixture(scope='session')
def mqtt_pair():
    pair = MqttPair()
    for v in VENDORS.values():
        pair.subscribe(v)
    yield pair
    pair.close()


def pytest_generate_tests(metafunc):
    """Parametrize test_parity over (vendor, file) for every corpus file."""
    if 'vendor_key' in metafunc.fixturenames and 'filename' in metafunc.fixturenames:
        from parity import vendor_files
        cases = []
        ids = []
        for key, v in VENDORS.items():
            for f, rel in vendor_files(v):
                cases.append((key, f))
                ids.append(f'{key}/{rel}')
        metafunc.parametrize('vendor_key,filename', cases, ids=ids)
