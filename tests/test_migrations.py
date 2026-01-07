from trader.persistence import Database


def test_db_init_idempotent(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    db = Database(str(db_path))
    # run second time to ensure idempotency
    Database(str(db_path))

    cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(fills)")}
    assert "exec_id" in cols

    indexes = {row["name"] for row in db.conn.execute("PRAGMA index_list(fills)")}
    assert "idx_fills_exec_id" in indexes
