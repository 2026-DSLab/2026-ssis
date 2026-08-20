"""요약 DB 적재 — LawSummaryRepo 와 DbSink.

실제 PostgreSQL 없이 돌아간다. 커넥션 계층(Database)을 가짜로 바꿔 끼워
"어떤 SQL 에 어떤 파라미터가 실렸는가"만 본다.

★ 왜 실 DB 를 안 쓰는가: 이 계층에서 틀릴 수 있는 것은 SQL 문법이
  아니라 '무엇을 키로 삼았는가', '실패한 한 건이 나머지를 죽이는가',
  'dataclass 를 JSON 으로 옮길 때 빠지는 필드가 없는가' 다. 그건 전부
  가짜 커넥션으로 확인된다. 실 DB 연결은 테스트를 느리고 불안정하게
  만들 뿐 이 질문들에 답해주지 않는다.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date

import pytest

from lawtrack.db.repo import LawSummaryRepo, _decode_summary_row, _parse_iso_date
from summarizer.models import ArticleSummary, ArticleUnit, ContractSummary, LawSummary
from summarizer.sinks import DbSink


# ---------------------------------------------------------------------------
# 가짜 DB
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeDb:
    """Database 의 transaction()/cursor() 계약만 흉내낸다."""

    def __init__(self, rows=None, *, fail_on: str = ""):
        self.cursor_obj = FakeCursor(rows)
        self.committed = 0
        self.rolled_back = 0
        self._fail_on = fail_on

    @contextmanager
    def transaction(self, *, dictionary: bool = True):
        try:
            yield None, self.cursor_obj
            # 방금 실린 파라미터만 본다. 누적된 호출 이력 전체를 보면
            # 한 번 실패한 뒤의 모든 호출이 덩달아 실패한다.
            last = self.cursor_obj.calls[-1] if self.cursor_obj.calls else (None, ())
            if self._fail_on and self._fail_on in str(last[1]):
                raise RuntimeError("의도적 DB 실패")
            self.committed += 1
        except Exception:
            self.rolled_back += 1
            raise

    @contextmanager
    def cursor(self, *, dictionary: bool = True):
        yield None, self.cursor_obj


# ---------------------------------------------------------------------------
# 표본
# ---------------------------------------------------------------------------

def make_unit(**kw) -> ArticleUnit:
    base = dict(
        law_id="012045",
        law_name="국민기초생활 보장법",
        location_label="제5조①",
        change_type="개정",
        old_text="통계청장이 정한다.",
        new_text="국가데이터처장이 정한다.",
        match_status="성공",
    )
    base.update(kw)
    return ArticleUnit(**base)


def make_law(**kw) -> LawSummary:
    base = dict(
        law_id="012045",
        law_name="국민기초생활 보장법",
        law_type="법률",
        new_serial_no="276657",
        enforce_date="2025-10-01",
        revision_type="타법개정",
        source_url="https://www.law.go.kr/...",
        headline="통계청 명칭 변경",
        body="본문",
        overview="취지",
        caveats=["조문 요약 실패: 제3조①"],
        article_summaries=[
            ArticleSummary(unit=make_unit(), summary="'통계청'이 '국가데이터처'로 변경되었습니다.")
        ],
    )
    base.update(kw)
    return LawSummary(**base)


def make_contract(laws=None) -> ContractSummary:
    return ContractSummary(
        source_file="weekly_contract_2026-07-19.json",
        batch_date="2026-07-20",
        laws=laws if laws is not None else [make_law()],
    )


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2025-10-01", date(2025, 10, 1)),
        ("", None),
        (None, None),
        ("   ", None),
        # date.fromisoformat 은 3.11+ 부터 압축 표기('20251001')도 받는다.
        # 계약은 하이픈 표기지만, 받아준다고 손해볼 것이 없어 그대로 둔다.
        ("20251001", date(2025, 10, 1)),
        ("2025-13-99", None),      # 달·일이 범위를 벗어나면 조용히 None
        ("시행일 미상", None),      # 날짜가 아닌 문자열도 터지지 않고 NULL
        (date(2025, 10, 1), date(2025, 10, 1)),
    ],
)
def test_parse_iso_date(raw, expected):
    assert _parse_iso_date(raw) == expected


# ---------------------------------------------------------------------------
# LawSummaryRepo
# ---------------------------------------------------------------------------

def test_upsert_uses_law_id_and_serial_as_key():
    """키가 (law_id, new_serial_no) 라는 것이 이 테이블 설계의 핵심이다."""
    db = FakeDb()
    LawSummaryRepo(db).upsert(
        law_id="012045", new_serial_no="276657", law_name="국민기초생활 보장법",
    )

    sql, params = db.cursor_obj.calls[0]
    assert "INSERT INTO law_summary" in sql
    assert "ON CONFLICT (law_id, new_serial_no) DO UPDATE SET" in sql
    assert params[0] == "012045"
    assert params[1] == "276657"
    assert db.committed == 1


def test_upsert_rejects_empty_serial_no():
    """빈 일련번호를 허용하면 서로 다른 개정분이 같은 키('')로 몰려
    마지막 것만 남는다 — 조용히 데이터가 사라지므로 여기서 막는다."""
    repo = LawSummaryRepo(FakeDb())
    with pytest.raises(ValueError, match="new_serial_no"):
        repo.upsert(law_id="012045", new_serial_no="", law_name="법")
    with pytest.raises(ValueError, match="law_id"):
        repo.upsert(law_id="", new_serial_no="276657", law_name="법")


def test_upsert_serializes_json_columns_as_utf8():
    """한글이 \\uXXXX 로 이스케이프되면 DB 에서 눈으로 못 읽는다."""
    db = FakeDb()
    LawSummaryRepo(db).upsert(
        law_id="012045", new_serial_no="276657", law_name="법",
        caveats=["원문 확인 필요"],
    )
    _, params = db.cursor_obj.calls[0]
    caveats_json = params[10]
    assert "원문 확인 필요" in caveats_json
    assert json.loads(caveats_json) == ["원문 확인 필요"]


def test_upsert_empty_lists_become_null():
    """빈 목록을 '[]' 문자열로 넣으면 '값이 있는데 빈 것'과 '아직 안 넣은 것'을
    구분할 수 없다. 빈 것은 NULL 로 둔다."""
    db = FakeDb()
    LawSummaryRepo(db).upsert(
        law_id="012045", new_serial_no="276657", law_name="법", caveats=[],
    )
    _, params = db.cursor_obj.calls[0]
    assert params[10] is None


def test_upsert_converts_date_strings():
    db = FakeDb()
    LawSummaryRepo(db).upsert(
        law_id="012045", new_serial_no="276657", law_name="법",
        enforce_date="2025-10-01", batch_date="2026-07-20",
    )
    _, params = db.cursor_obj.calls[0]
    assert params[4] == date(2025, 10, 1)     # enforce_date
    assert params[16] == date(2026, 7, 20)    # batch_date


def test_fetch_decodes_json_columns():
    row = {
        "law_id": "012045",
        "caveats": '["원문 확인 필요"]',
        "article_summaries": None,
        "mappings": '[]',
        "verifier_issues": None,
    }
    db = FakeDb(rows=[row])
    got = LawSummaryRepo(db).fetch("012045", "276657")

    assert got["caveats"] == ["원문 확인 필요"]
    assert got["article_summaries"] == []   # NULL 은 빈 목록으로
    assert got["mappings"] == []
    assert got["verifier_issues"] == []


def test_decode_summary_row_accepts_already_parsed_json():
    """드라이버 버전에 따라 JSON 컬럼이 이미 객체로 오기도 한다."""
    row = {"caveats": ["이미 파싱됨"], "article_summaries": [], "mappings": [], "verifier_issues": []}
    assert _decode_summary_row(row)["caveats"] == ["이미 파싱됨"]


def test_decode_summary_row_none():
    assert _decode_summary_row(None) is None


# ---------------------------------------------------------------------------
# DbSink
# ---------------------------------------------------------------------------

def test_dbsink_writes_every_law():
    db = FakeDb()
    saved = DbSink(db, llm_provider="openai", llm_model="gpt-5.4-mini").write(
        [make_contract([make_law(law_id="A", new_serial_no="1"),
                        make_law(law_id="B", new_serial_no="2")])]
    )
    assert saved == 2
    assert len(db.cursor_obj.calls) == 2


def test_dbsink_carries_provider_and_model():
    """모델을 바꾼 뒤 요약 품질이 달라졌을 때 어느 판본인지 알아야 한다."""
    db = FakeDb()
    DbSink(db, llm_provider="anthropic", llm_model="claude-opus-5").write([make_contract()])

    _, params = db.cursor_obj.calls[0]
    assert params[14] == "anthropic"
    assert params[15] == "claude-opus-5"


def test_dbsink_serializes_nested_dataclasses():
    """article_summaries 는 dataclass 중첩(ArticleSummary → ArticleUnit)이다.
    asdict 로 풀지 않으면 json.dumps 가 터진다."""
    db = FakeDb()
    DbSink(db).write([make_contract()])

    _, params = db.cursor_obj.calls[0]
    articles = json.loads(params[11])
    assert articles[0]["unit"]["location_label"] == "제5조①"
    assert articles[0]["unit"]["old_text"] == "통계청장이 정한다."
    assert "국가데이터처" in articles[0]["summary"]


def test_dbsink_keeps_going_when_one_law_fails(caplog):
    """한 건이 실패해도 나머지는 들어가야 한다 — run_weekly 가 워치리스트
    한 건의 실패로 배치를 죽이지 않는 것과 같은 원칙."""
    db = FakeDb(fail_on="터질법")
    saved = DbSink(db).write(
        [make_contract([
            make_law(law_id="A", new_serial_no="1"),
            make_law(law_id="B", new_serial_no="2", law_name="터질법"),
            make_law(law_id="C", new_serial_no="3"),
        ])]
    )
    assert saved == 2          # 3건 중 1건만 실패
    assert db.rolled_back == 1


def test_dbsink_records_failed_summaries_too():
    """LLM 호출이 실패한 요약도 행으로 남긴다 — 실패를 조용히 빼지 않는다."""
    db = FakeDb()
    DbSink(db).write([make_contract([make_law(error="API timeout", headline="", body="")])])

    _, params = db.cursor_obj.calls[0]
    assert params[18] == "API timeout"


def test_dbsink_skips_law_without_serial_no():
    """일련번호가 없는 요약은 저장하지 않되, 나머지는 계속 저장한다."""
    db = FakeDb()
    saved = DbSink(db).write(
        [make_contract([
            make_law(law_id="A", new_serial_no=""),
            make_law(law_id="B", new_serial_no="2"),
        ])]
    )
    assert saved == 1
