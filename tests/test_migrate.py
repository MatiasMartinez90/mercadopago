from pathlib import Path

import pytest

from mercadopago_service.migrate import migration_up


def test_extracts_only_up_migration(tmp_path: Path):
    path = tmp_path / "001.sql"
    path.write_text(
        "-- migrate:up\nCREATE TABLE example(id int);\n"
        "-- migrate:down\nDROP TABLE example;\n",
        encoding="utf-8",
    )
    assert migration_up(path) == "CREATE TABLE example(id int);"


def test_rejects_migration_without_down_marker(tmp_path: Path):
    path = tmp_path / "broken.sql"
    path.write_text("-- migrate:up\nSELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid migration markers"):
        migration_up(path)
