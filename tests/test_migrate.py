"""기존 DB용 마이그레이션의 누락 확인과 재실행 안전성 테스트."""

from unittest.mock import MagicMock

from lawtrack.db.migrate import REQUIRED_COLUMNS, apply_migrations


def _mock_db(*column_results, update_rowcount=0):
    db = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    db.connection.return_value.__enter__.return_value = conn
    conn.cursor.return_value = cur
    cur.fetchone.side_effect = column_results
    cur.rowcount = update_rowcount
    return db, conn, cur


def test_adds_only_missing_columns_and_corrects_watchlist():
    db, conn, cur = _mock_db(None, {"1": 1}, None, update_rowcount=1)

    applied = apply_migrations(db)

    executed_sql = [call.args[0] for call in cur.execute.call_args_list]
    assert REQUIRED_COLUMNS[0].ddl in executed_sql
    assert REQUIRED_COLUMNS[1].ddl not in executed_sql
    assert REQUIRED_COLUMNS[2].ddl in executed_sql
    assert applied == [
        "laws.law_articles_parsed 추가",
        "change_log.promulgation_date 추가",
        "watchlist 42433 공식 명칭 교정",
    ]
    conn.commit.assert_called_once_with()
    cur.close.assert_called_once_with()


def test_is_noop_when_database_is_already_current():
    db, conn, cur = _mock_db({"1": 1}, {"1": 1}, {"1": 1}, update_rowcount=0)

    applied = apply_migrations(db)

    assert applied == []
    assert not any(
        call.args[0].startswith("ALTER TABLE")
        for call in cur.execute.call_args_list
    )
    conn.commit.assert_called_once_with()


def test_rolls_back_and_closes_cursor_on_failure():
    db, conn, cur = _mock_db(None)
    cur.execute.side_effect = [None, RuntimeError("DDL failed")]

    try:
        apply_migrations(db)
    except RuntimeError as exc:
        assert str(exc) == "DDL failed"
    else:
        raise AssertionError("migration error was not propagated")

    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()
    cur.close.assert_called_once_with()
