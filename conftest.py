import pytest

from storage.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "bot.db"))