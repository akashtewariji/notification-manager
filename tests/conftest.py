import os
import sys
import tempfile

import pytest

# Point the app at a throwaway data dir *before* it is imported.
os.environ['NM_DATA_DIR'] = tempfile.mkdtemp(prefix='nm-test-')
os.environ.pop('NM_HOST', None)
os.environ.pop('NM_ALLOWED_HOSTS', None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as nm  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


@pytest.fixture
def fixture_lines():
    def load(name):
        with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
            return f.readlines()
    return load


@pytest.fixture
def client():
    nm.init_db()
    nm.app.config['TESTING'] = True
    with nm.app.test_client() as c:
        yield c


@pytest.fixture
def token():
    return nm.get_token()
