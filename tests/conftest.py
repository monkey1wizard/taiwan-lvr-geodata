"""Shared test setup."""
import pytest

from lvr_pipeline.addresses import admin


@pytest.fixture(autouse=True)
def restore_admin_towns_path():
    """A test may point the town table at a synthetic one; every test starts and ends with the committed file."""
    original = admin.ADMIN_TOWNS_PATH
    admin.load_admin_towns.cache_clear()
    yield
    admin.ADMIN_TOWNS_PATH = original
    admin.load_admin_towns.cache_clear()
