"""VersionRepo 테스트. DB 연결 없이 SQL/파라미터만 검증.

 DB 간소화(1): law_articles_parsed/administrative_rule_articles_parsed
캐시 컬럼을 제거했음 — 아무 코드도 그 캐시를 다시 읽지 않았고(write-only),
필요하면 parse_articles(law_full_text)로 언제든 그 자리에서 다시 만들 수
있음.

 DB 간소화(2): laws/administrative_rules 두 테이블을 documents
하나로 합쳤음 — 컬럼 구성이 이름만 다를 뿐 완전히 같았음. kind 구분
컬럼('law'/'admrul')으로 어느 쪽인지 가름.
"""

import json
from unittest.mock import MagicMock

from lawtrack.db.repo import VersionRepo


def _mock_db():
    db = MagicMock()
    cur = MagicMock()
    conn = MagicMock()
    db.transaction.return_value.__enter__.return_value = (conn, cur)
    db.cursor.return_value.__enter__.return_value = (conn, cur)
    return db, cur


class TestInsertLaw:
    def test_inserts_full_text_with_law_kind(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        repo.insert_law("전자정부법", "009199", "268103", {"법령": {}})

        sql, params = cur.execute.call_args[0]
        assert "documents" in sql
        assert params == (
            "law", "009199", "268103", "전자정부법",
            json.dumps({"법령": {}}, ensure_ascii=False),
        )


class TestInsertAdmrul:
    def test_inserts_full_text_with_admrul_kind(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        repo.insert_admrul("정보시스템 감리기준", "33483", "2100000243290", {"AdmRulService": {}})

        sql, params = cur.execute.call_args[0]
        assert "documents" in sql
        assert params == (
            "admrul", "33483", "2100000243290", "정보시스템 감리기준",
            json.dumps({"AdmRulService": {}}, ensure_ascii=False),
        )


class TestExistsQueriesUseKind:
    """law_exists/admrul_exists 는 같은 documents 테이블을 kind로만 갈라
    조회해야 함 — 서로 다른 kind끼리 doc_id/doc_serial_no 가 우연히
    같아도 섞이면 안 됨."""

    def test_law_exists_filters_by_law_kind(self):
        db, cur = _mock_db()
        cur.fetchone.return_value = {"?column?": 1}
        repo = VersionRepo(db)
        assert repo.law_exists("009199", "268103") is True

        sql, params = cur.execute.call_args[0]
        assert "kind=%s" in sql
        assert params == ("law", "009199", "268103")

    def test_admrul_exists_filters_by_admrul_kind(self):
        db, cur = _mock_db()
        cur.fetchone.return_value = None
        repo = VersionRepo(db)
        assert repo.admrul_exists("33483", "2100000243290") is False

        sql, params = cur.execute.call_args[0]
        assert "kind=%s" in sql
        assert params == ("admrul", "33483", "2100000243290")
