"""요약 파이프라인 — 검증(코드 대조), 보고서 생성, 배치 배선.

LLM 을 부르지 않는다. 부르는 곳은 전부 가짜 클라이언트로 바꿔 끼운다 —
테스트가 네트워크와 과금에 의존하면 아무도 돌리지 않게 된다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from summarizer.models import ArticleSummary, ArticleUnit, ContractSummary, LawSummary
from summarizer.report import build_report, verify_report
from summarizer.verifier import verify_summaries

from scripts.run_weekly import _parse_args


def unit(**kw) -> ArticleUnit:
    base = dict(
        law_id="012045",
        law_name="국민기초생활 보장법",
        location_label="제5조①",
        change_type="개정",
        old_text="통계청장이 정하여 고시한다.",
        new_text="국가데이터처장이 정하여 고시한다.",
        match_status="성공",
        diff_parts=["'통계청' → '국가데이터처'"],
    )
    base.update(kw)
    return ArticleUnit(**base)


# ---------------------------------------------------------------------------
# 검증 — 코드로 원문과 대조한다 (LLM 판단 없음)
# ---------------------------------------------------------------------------

def test_accurate_summary_passes():
    """정확한 요약에 트집을 잡지 않아야 한다. LLM 감수를 코드 대조로
    바꾼 이유가 바로 이 오탐 때문이었다."""
    s = ArticleSummary(unit=unit(), summary="'통계청'이 '국가데이터처'로 변경되었습니다.")
    assert verify_summaries([s]) == []


def test_hallucinated_quote_is_caught():
    """요약이 인용한 조각이 원문 어디에도 없으면 지어낸 것이다."""
    s = ArticleSummary(unit=unit(), summary="'보건복지부장관'이 '국가데이터처'로 변경되었습니다.")
    issues = verify_summaries([s])

    assert len(issues) == 1
    assert issues[0].severity == "high"
    assert "보건복지부장관" in issues[0].problem


def test_reversed_direction_is_caught():
    """요약이 개정 전후를 뒤집어 말하면 잡아야 한다."""
    s = ArticleSummary(unit=unit(), summary="'국가데이터처'가 '통계청'으로 변경되었습니다.")
    issues = verify_summaries([s])

    assert any("방향오류" in i.problem for i in issues)


def test_multiple_quote_pairs_do_not_false_alarm():
    """따옴표 쌍이 여러 개인 문장에서, 1번 닫음~2번 여는 따옴표 사이를
    인용어로 잘못 잡던 버그(verifier.py 주석 참조)의 회귀 테스트."""
    u = unit(
        old_text="그 사실을 안 때에는 등을 신고한다.",
        new_text="그 사실을 안 경우에는 등을 신고한다.",
        diff_parts=["'때' → '경우'"],
    )
    s = ArticleSummary(unit=u, summary="'등'이 추가되고, '때'가 '경우'로 바뀌었습니다.")
    assert verify_summaries([s]) == []


def test_failed_summary_is_reported():
    s = ArticleSummary(unit=unit(), summary="", error="API timeout")
    issues = verify_summaries([s])

    assert len(issues) == 1
    assert issues[0].severity == "high"


def test_rule_based_summaries_are_not_verified():
    """이동(내용 동일)·변경없음은 코드가 만든 확정 문장이라 검증 대상이 아니다."""
    moved = ArticleSummary(
        unit=unit(change_type="이동", move_is_identical=True, diff_parts=[]),
        summary="제5조①이 제5조②로 이동했습니다(내용 동일).",
    )
    unchanged = ArticleSummary(
        unit=unit(no_change=True, diff_parts=[]),
        summary="이 항목은 이번 개정에서 내용이 바뀌지 않았습니다.",
    )
    assert verify_summaries([moved, unchanged]) == []


# ---------------------------------------------------------------------------
# HWPX 보고서 — 만든 문서를 다시 열어 원본과 대조한다
# ---------------------------------------------------------------------------

def sample_contract(**kw) -> ContractSummary:
    law = LawSummary(
        law_id="012045",
        law_name="국민기초생활 보장법",
        law_type="법률",
        new_serial_no="276657",
        enforce_date="2025-10-01",
        revision_type="타법개정",
        source_url="https://www.law.go.kr/",
        headline="통계청의 국가데이터처 개편에 따른 명칭 정비",
        body="본문",
        overview="국가 데이터 체계의 재설계를 위해 통계청을 국가데이터처로 개편합니다.",
        caveats=["조문 구조 변경 판정 확인 필요(제5조) — 원문 대조 권장"],
        article_summaries=[
            ArticleSummary(unit=unit(), summary="'통계청'이 '국가데이터처'로 변경되었습니다."),
            ArticleSummary(
                unit=unit(location_label="제60조의2②1.가.", change_type="신설"),
                summary="자활급여 지급 대상에 관한 세부 기준이 신설되었습니다.",
            ),
        ],
    )
    base = dict(source_file="weekly_contract_2026-07-19.json", batch_date="2026-07-20", laws=[law])
    base.update(kw)
    return ContractSummary(**base)


def test_report_contains_every_summary(tmp_path):
    """요약이 문서에 온전히 들어갔는지 — 생성 후 다시 열어 대조한다."""
    contract = sample_contract()
    path = build_report(contract, tmp_path / "report.hwpx")

    assert path.exists()
    assert verify_report(contract, path) == []


def test_report_survives_xml_special_characters(tmp_path):
    """HWPX 는 XML+ZIP 이라 &, <, > 가 그대로 들어가면 문서가 깨진다."""
    law = sample_contract().laws[0]
    tricky = ArticleSummary(
        unit=unit(location_label="제7조③"),
        summary="'A & B' 가 '<C> 및 \"D\"' 로 변경되었습니다. ①②③ ㆍ 100% 초과",
    )
    contract = ContractSummary(
        source_file="x.json", batch_date="2026-07-20",
        laws=[replace(law, article_summaries=[tricky])],
    )
    path = build_report(contract, tmp_path / "special.hwpx")

    assert verify_report(contract, path) == []


def test_report_with_no_changes(tmp_path):
    """개정 0건인 주에도 보고서는 나와야 한다 — '이번 주 개정 없음'이
    보고서로 남는 것 자체가 결과물이다."""
    contract = ContractSummary(source_file="empty.json", batch_date="2026-07-20", laws=[])
    path = build_report(contract, tmp_path / "empty.hwpx")

    assert path.exists()
    assert verify_report(contract, path) == []


def test_report_includes_unresolved_and_no_comparison(tmp_path):
    """위치 미확정·비교 불가는 요약하지 않고 그대로 싣는다."""
    contract = ContractSummary(
        source_file="x.json", batch_date="2026-07-20", laws=[],
        unresolved=[{"law_name": "사회보장기본법", "reason": "위치확정실패", "detail": "제3조 조각 2건"}],
        no_comparison=[{"law_name": "아동복지법", "reason": "신구법없음", "note": "제정"}],
    )
    path = build_report(contract, tmp_path / "passthrough.hwpx")

    from hwpx import HwpxDocument
    text = HwpxDocument.open(str(path)).export_text()
    assert "사회보장기본법" in text
    assert "아동복지법" in text


def test_verify_report_detects_missing_text(tmp_path):
    """검증이 실제로 누락을 잡는지 — 검증 자체가 늘 빈 목록만 내면 무의미하다."""
    contract = sample_contract()
    path = build_report(contract, tmp_path / "r.hwpx")

    # 문서를 만든 뒤 원본에 없던 문장을 기대치에 추가하면 누락으로 잡혀야 한다.
    tampered = replace(
        contract,
        laws=[replace(contract.laws[0], overview="이 문장은 보고서에 들어간 적이 없습니다.")],
    )
    assert verify_report(tampered, path) != []


# ---------------------------------------------------------------------------
# 배치 배선 — 어떤 조합의 플래그가 무엇을 켜는가
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv, summarize, hwpx, to_db",
    [
        ([],                 False, False, False),   # 기본은 감지만 — LLM 비용 없음
        (["--summarize"],    True,  False, False),
        (["--hwpx"],         True,  True,  False),   # 보고서는 요약을 함의한다
        (["--summary-db"],   True,  False, True),
        (["--full"],         True,  True,  True),
    ],
)
def test_flag_combinations(argv, summarize, hwpx, to_db):
    args = _parse_args(argv)
    assert (args.summarize, args.hwpx, args.summary_db) == (summarize, hwpx, to_db)


def test_console_survives_characters_the_codepage_lacks(monkeypatch):
    """cp949 콘솔에서 em dash(—) 를 출력해도 죽지 않아야 한다.

    회귀 테스트: 요약 caveat 에 em dash 가 들어있어 화면 출력 도중
    UnicodeEncodeError 로 프로세스가 죽던 버그(2026-07-28). 요약을 다
    만든 뒤에 죽는 것이라 LLM 호출 비용을 그대로 날렸다.
    """
    import io
    import sys

    from lawtrack.config import setup_console

    cp949 = io.TextIOWrapper(io.BytesIO(), encoding="cp949", newline="")
    monkeypatch.setattr(sys, "stdout", cp949)
    monkeypatch.setattr(sys, "stderr", cp949)

    with pytest.raises(UnicodeEncodeError):
        print("감수: 요약이 원문과 다름 — 원문 확인 필요", file=cp949)
        cp949.flush()

    setup_console()
    print("감수: 요약이 원문과 다름 — 원문 확인 필요", file=cp949)   # 죽지 않아야 한다
    cp949.flush()


def test_summary_stage_reports_failure_without_losing_detection(monkeypatch, tmp_path, capsys):
    """요약이 실패해도 감지 결과는 이미 저장되어 있다는 것을 알려야 한다."""
    import scripts.run_weekly as rw

    def boom(*_a, **_kw):
        raise RuntimeError("LLM 서버 응답 없음")

    monkeypatch.setattr("summarizer.llm.build_client", boom)
    errors = rw.run_summary_stage(tmp_path / "c.json", db=None, hwpx=False, to_db=False)

    assert errors == 1
    assert "감지·비교 결과는 이미" in capsys.readouterr().out
