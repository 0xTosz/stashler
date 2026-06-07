from pathlib import Path

from stasher.config import DEFAULT_UI_PORT, Config, user_data_dir


def test_default_port_is_uncommon():
    assert DEFAULT_UI_PORT == 7137


def test_user_data_dir_named_stashler():
    assert user_data_dir().name == "Stashler"


def test_storage_defaults_to_data_dir():
    c = Config()
    assert c.data_dir  # resolved to the per-user dir
    assert c.db_path == str(Path(c.data_dir) / "stasher.db")


def test_explicit_db_path_is_kept(tmp_path):
    c = Config(db_path=str(tmp_path / "x.db"))
    assert c.db_path == str(tmp_path / "x.db")
    assert c.data_dir  # still resolved for rules


def test_explicit_data_dir_places_db(tmp_path):
    c = Config(data_dir=str(tmp_path))
    assert c.db_path == str(tmp_path / "stasher.db")
