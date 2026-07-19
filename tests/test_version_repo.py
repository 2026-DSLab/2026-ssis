"""VersionRepo.insert_law/insert_admrul 테스트. DB 연결 없이 SQL/파라미터만 검증.

★ 실측 요구사항(2026-07-18): law_full_text(원본)와 별도로 조/항/호/목
파싱 결과도 DB에 저장해야 한다는 요구사항에 따라 parsed_articles/
parsed_units 파라미터를 추가했다 — 이 테스트는 그 옵션이 없을 때(기존
동작 유지)와 있을 때(새 컬럼까지 포함해 INSERT) 둘 다 검증한다.
"""

import json
from unittest.mock import MagicMock

from lawtrack.db.repo import VersionRepo


def _mock_db():
    db = MagicMock()
    cur = MagicMock()
    conn = MagicMock()
    db.transaction.return_value.__enter__.return_value = (conn, cur)
    return db, cur


class TestInsertLawParsedArticles:
    def test_without_parsed_articles_uses_4_column_insert(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        repo.insert_law("전자정부법", "009199", "268103", {"법령": {}})

        sql, params = cur.execute.call_args[0]
        assert "law_articles_parsed" not in sql
        assert params == ("전자정부법", "009199", "268103", json.dumps({"법령": {}}, ensure_ascii=False))

    def test_with_parsed_articles_includes_new_column(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        parsed = [{"label": "제1조", "clauses": []}]
        repo.insert_law("전자정부법", "009199", "268103", {"법령": {}}, parsed_articles=parsed)

        sql, params = cur.execute.call_args[0]
        assert "law_articles_parsed" in sql
        assert params[4] == json.dumps(parsed, ensure_ascii=False)


class TestInsertAdmrulParsedUnits:
    def test_without_parsed_units_uses_4_column_insert(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        repo.insert_admrul("정보시스템 감리기준", "33483", "2100000243290", {"AdmRulService": {}})

        sql, params = cur.execute.call_args[0]
        assert "administrative_rule_articles_parsed" not in sql
        assert params == (
            "정보시스템 감리기준", "33483", "2100000243290",
            json.dumps({"AdmRulService": {}}, ensure_ascii=False),
        )

    def test_with_parsed_units_includes_new_column(self):
        db, cur = _mock_db()
        repo = VersionRepo(db)
        parsed = [{"article_label": "제1조", "clause_no": "", "text": "x"}]
        repo.insert_admrul(
            "정보시스템 감리기준", "33483", "2100000243290", {"AdmRulService": {}},
            parsed_units=parsed,
        )

        sql, params = cur.execute.call_args[0]
        assert "administrative_rule_articles_parsed" in sql
        assert params[4] == json.dumps(parsed, ensure_ascii=False)
