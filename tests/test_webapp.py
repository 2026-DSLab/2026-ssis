"""웹페이지(webapp/app.py) 라우트 테스트.

진짜 DB 없이 돎 — repo를 가짜로 주입함(create_app(repo=...)).
법령 요약이 실제로 law_summary에 어떻게 JSON으로 저장/디코딩되는지는
tests/test_summary_db.py가 이미 검증하므로, 여기서는 "그 dict가 웹페이지에
정확히 반영되는가"만 본다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from webapp.app import create_app
from webapp.live import PeriodResult


class _FakeRepo:
    """batch_date 는 이제 주간 리포트에 무엇을 보여줄지 고르는 값이 아니라,
    비었을 때 "배치를 거른 것인가"를 안내하는 데만 쓰는 값임 — 렌더링
    테스트들은 여전히 이 인자를 넘기지만 목록 자체는 laws 로만 정해짐."""

    def __init__(
        self, *, batch_date: date | None, laws: list[dict] | None = None,
        after_week_count: int = 0,
    ):
        self._batch_date = batch_date
        self._laws = laws or []
        self._after_week_count = after_week_count
        self.enforce_periods: list[tuple[date, date]] = []
        self.counted_periods: list[tuple[date, date]] = []

    def latest_batch_date(self):
        return self._batch_date

    def fetch_by_enforce_period(self, from_date, to_date):
        self.enforce_periods.append((from_date, to_date))
        return self._laws

    def count_by_enforce_period(self, from_date, to_date):
        self.counted_periods.append((from_date, to_date))
        return self._after_week_count


def _law_row(**kw) -> dict:
    base = dict(
        law_id="009199", new_serial_no="268103", law_name="전자정부법",
        law_type="법률", enforce_date="2025-07-08", revision_type="일부개정",
        source_url="https://www.law.go.kr/x", source_file="weekly_contract_2026-07-19_b.json",
        headline="정보시스템 장애관리 체계 강화", overview="이번 개정은 ...",
        caveats=[], article_summaries=[], mappings=[], verifier_issues=[],
    )
    base.update(kw)
    return base


def test_law_dominant_kind_prioritizes_new_over_amend():
    """신설이 1건만 섞여 있어도(나머지는 개정) 카드 강조색은 신설이어야
    함 — 다수결로 고르면 눈에 띄어야 할 신설이 묻힘."""
    from webapp.app import _law_dominant_kind

    law = {"article_summaries": [
        {"unit": {"change_type": "개정", "no_change": False}},
        {"unit": {"change_type": "개정", "no_change": False}},
        {"unit": {"change_type": "신설", "no_change": False}},
    ]}

    assert _law_dominant_kind(law) == "신설"


def test_law_dominant_kind_falls_back_when_no_articles():
    from webapp.app import _law_dominant_kind

    assert _law_dominant_kind({"article_summaries": []}) == "변경"


def test_index_shows_empty_state_when_no_batch():
    app = create_app(repo=_FakeRepo(batch_date=None))
    client = app.test_client()

    resp = client.get("/summary")

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "시행된 개정이 없습니다" in html
    assert "아직 한 번도 배치를 돌리지 않았습니다" in html


def test_index_selects_last_full_calendar_week():
    """주간 리포트는 마지막 배치가 아니라 지난 달력 주(월~일)를 창으로
    씀 — 배치를 언제 돌렸는지와 무관해야 함."""
    from lawtrack.week import last_full_week

    repo = _FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()])
    create_app(repo=repo).test_client().get("/summary")

    assert repo.enforce_periods == [last_full_week()]


def test_index_shows_week_range_instead_of_batch_date():
    laws = [_law_row()]
    repo = _FakeRepo(batch_date=date(2026, 7, 20), laws=laws)
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    week_from, week_to = repo.enforce_periods[0]
    assert f"{week_from} ~ {week_to} 시행" in html
    assert "2026-07-20 기준" not in html


def test_index_empty_week_hints_that_batch_may_be_overdue():
    """지난주 시행분이 0건일 때, 마지막 배치가 그 주보다 오래됐으면
    "개정이 없었다"가 아니라 "배치를 아직 안 돌렸다"일 수 있음을 알림."""
    repo = _FakeRepo(batch_date=date(2020, 1, 1), laws=[])
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    assert "마지막 배치는 2020-01-01 입니다" in html


def test_index_notes_revisions_enforced_after_the_week():
    """주간 리포트의 창은 지난 한 주라, 이번 주 들어 시행된 개정은 여기
    안 뜨고 옆의 "최근 5일"에만 뜸 — "그럼 그건 어디 있나"에 화면에서
    바로 답함."""
    repo = _FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()], after_week_count=3)
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    assert "이 주 이후에 시행된 개정이" in html
    assert "<strong>3</strong>건 더 있습니다" in html


def test_after_week_notice_counts_from_the_day_after_the_window():
    """세는 구간은 창 다음 날부터 오늘까지 — 창 안의 개정을 다시 세면
    "더 있습니다"가 거짓말이 됨."""
    from lawtrack.week import last_full_week

    _, week_to = last_full_week()
    repo = _FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()], after_week_count=1)
    create_app(repo=repo).test_client().get("/summary")

    assert repo.counted_periods == [(week_to + timedelta(days=1), date.today())]


def test_after_week_notice_links_to_a_window_that_covers_it():
    """알린 건수를 전부 담는 가장 짧은 프리셋으로 보냄. 일요일에는 창
    다음 날(월)부터 오늘까지가 6일이라 "최근 5일"로는 월요일이 빠지므로
    "최근 2주"여야 함 — "3건 더 있다"고 해놓고 눌렀더니 안 보이면 같은
    종류의 어긋남임."""
    from webapp.app import _after_week_notice

    class _Counter:
        def count_by_enforce_period(self, f, t):
            return 3

    # 목요일에 열었을 때 — 알릴 구간 월~목(3일)
    thu = _after_week_notice(_Counter(), date(2026, 8, 23), date(2026, 8, 27))
    assert (thu["period"], thu["label"]) == ("5d", "최근 5일")

    # 일요일에 열었을 때 — 알릴 구간 월~일(6일)
    sun = _after_week_notice(_Counter(), date(2026, 8, 23), date(2026, 8, 30))
    assert (sun["period"], sun["label"]) == ("2w", "최근 2주")


def test_no_after_week_notice_when_nothing_enforced_since():
    repo = _FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()], after_week_count=0)
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    assert "이 주 이후에 시행된 개정이" not in html


def test_after_week_notice_also_shows_on_the_empty_week():
    """이번 주가 0건일 때야말로 이 안내가 가장 필요함 — 빈 화면만 보고
    "아무 일도 없었다"로 읽으면 안 되기 때문."""
    repo = _FakeRepo(batch_date=None, laws=[], after_week_count=2)
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    assert "시행된 개정이 없습니다" in html
    assert "<strong>2</strong>건 더 있습니다" in html


def test_period_screen_has_no_after_week_notice():
    """기간 탭은 창이 오늘까지라 알릴 뒷구간 자체가 없음."""
    result = _period_result(laws=[_law_row()])
    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    html = app.test_client().get("/summary?period=5d").get_data(as_text=True)

    assert "이 주 이후에 시행된 개정이" not in html


def test_index_empty_week_stays_quiet_when_batch_is_current():
    """배치는 제때 돌았는데 그 주에 시행된 개정이 없었던 정상 상황 —
    괜히 배치 안내를 띄우지 않음."""
    from lawtrack.week import last_full_week

    _, week_to = last_full_week()
    repo = _FakeRepo(batch_date=week_to + timedelta(days=1), laws=[])
    html = create_app(repo=repo).test_client().get("/summary").get_data(as_text=True)

    assert "시행된 개정이 없습니다" in html
    assert "마지막 배치는" not in html


def test_index_renders_headline_and_article_row():
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제56조의2⑤", "change_type": "이동후개정",
                "no_change": False, "moved_from": "②",
            },
            "summary": "정보화 수요 조사 대신 장애관리계획 수립·시행 등을 규칙으로 정하도록 변경",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    client = app.test_client()

    resp = client.get("/summary")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "정보시스템 장애관리 체계 강화" in html
    # location_label은 조/항 구획마다 다른 span으로 쪼개져 렌더링됨(format_location).
    # 항/호/목은 색 대신 회색 글자(항/호/목)로 구분함.
    assert '<span class="loc-article">제56조의2</span>' in html
    assert '<span class="loc-clause">⑤<span class="loc-suffix">항</span></span>' in html
    assert "이동개정" in html  # _TAG 축약 규칙 적용됨
    # moved_from("②")도 "제N조" 없이 항/호만 오는 값이라 같은
    # format_location 규칙(항/호/목 회색 접미사)을 거쳐 렌더링됨.
    assert '※ 이동 전 위치: <span class="item-loc"><span class="loc-clause">②<span class="loc-suffix">항</span></span></span>' in html


def test_index_shows_enforce_date_small_above_location():
    """설계: 조문 위치 줄 위에 작고 옅은 색으로
    시행일을 덧붙임 — 본문보다 눈에 띄면 안 되므로 별도 클래스로."""
    laws = [_law_row(enforce_date="2026-07-10", article_summaries=[
        {
            "unit": {"location_label": "제9조⑥", "change_type": "개정", "no_change": False},
            "summary": "요약문", "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert '<div class="item-enforce-date">시행일 2026-07-10</div>' in html


def test_index_omits_enforce_date_line_when_missing():
    laws = [_law_row(enforce_date=None, article_summaries=[
        {
            "unit": {"location_label": "제9조⑥", "change_type": "개정", "no_change": False},
            "summary": "요약문", "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert "item-enforce-date" not in html


def test_format_location_wraps_branch_numbered_item_as_one_chip():
    """실측(국고금관리법 제10조의2②12의2.): 호가지번호가
    있는 "12의2." 같은 표기는 이전 정규식(\\d+\\.)이 "12"부터 이어지는
    "의2"를 못 잡아, "의2"만 스타일 없는 맨 텍스트로 남아 어색하게
    붙어 보였음. "의N"까지 통째로 하나의 loc-item 칩으로 감싸야 함."""
    from webapp.app import _format_location

    html = str(_format_location("제10조의2②12의2."))
    assert html == (
        '<span class="loc-article">제10조의2</span>'
        '<span class="loc-clause">②<span class="loc-suffix">항</span></span>'
        '<span class="loc-item">12의2<span class="loc-suffix">호</span></span>'
    )


def test_format_location_leaves_deletion_sentence_untouched():
    """실측: db/repo.py._to_row()가 삭제 항목에 이미 사람이
    읽는 문장("(삭제됨 — 개정 전 ①항 참고)")을 만들어 두는데, 이 문장을
    그대로 토큰 스캔에 태우면 "①"이 다시 항 패턴으로 잡혀 "항"이 중복
    붙음("①항항 참고"). "("로 시작하는 값은 이미 완성된 문장으로 보고
    그대로 반환해야 함."""
    from webapp.app import _format_location

    html = str(_format_location("(삭제됨 — 개정 전 ①항 참고)"))
    assert html == "(삭제됨 — 개정 전 ①항 참고)"
    assert "loc-clause" not in html


def test_index_bolds_only_changed_words_in_fulltext():
    """"원문 보기"의 개정 전/후는 어절 단위로 diff해서 바뀐 부분만
    <mark class="diff-changed">로 감싸야 함 — summarizer.textdiff의
    같은 어절 diff를 재사용(diff_old_html/diff_new_html)."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제20조②", "change_type": "개정", "no_change": False,
                "moved_from": None, "old_text_is_context": False,
                "old_text": "② 위원은 기획재정부 차관으로 한다.",
                "new_text": "② 위원은 기획예산처 차관으로 한다.",
            },
            "summary": "기획재정부가 기획예산처로 변경되었습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert '<mark class="diff-changed">기획재정부</mark>' in html
    assert '<mark class="diff-changed">기획예산처</mark>' in html
    # 안 바뀐 어절("위원은", "차관으로")은 굵게 감싸면 안 됨.
    assert '<mark class="diff-changed">위원은</mark>' not in html
    # 실측: 선행 항 기호("②")도 readable_text
    # 필터와 마찬가지로 굵게 나와야 함 — diff 강조 경로(_diff_html)는
    # 별도 코드라 이 처리가 빠져 있었음.
    assert "<strong>②</strong>" in html


def test_index_diff_bolds_leading_marker_matching_readable_text():
    """실측("5. 금고 이상의 실형을…"에서
    선행 기호 볼드 누락): 성공적으로 매칭된 항목(can_diff=True)의 "개정
    전/후" 패널은 readable_text() 필터가 아니라 diff_old_html/diff_new_html
    을 타는 완전히 다른 코드 경로라, readable_text에만 있던 선행 기호
    볼드 처리가 빠져 있었음. 양쪽 old_text/new_text의 선행 기호가 같으면
    (제자리 개정이라 번호 자체는 안 바뀐 경우) 그 기호를 diff 대상에서
    떼어 먼저 굵게 내고, 나머지만 어절 diff함."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제16조5.", "change_type": "개정", "no_change": False,
                "moved_from": None, "old_text_is_context": False,
                "old_text": "5. 금고 이상의 실형을 선고받고 집행이 면제된 날부터 5년",
                "new_text": "5. 금고 이상의 실형을 선고받고 집행이 면제된 날부터 7년",
            },
            "summary": "기간이 5년에서 7년으로 변경되었습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert "<strong>5.</strong>" in html
    assert '<mark class="diff-changed">5년</mark>' in html
    assert '<mark class="diff-changed">7년</mark>' in html
    # 선행 기호 자체가 어절 diff에 다시 걸려 이중으로 강조되면 안 됨.
    assert '<mark class="diff-changed">5.</mark>' not in html


def test_index_no_diff_highlight_when_old_text_is_context():
    """old_text_is_context=True(구조확장 등)인 경우 old_text가 이 위치의
    진짜 개정 전 문장이 아니므로, new_text를 그것과 diff해서 강조하면
    안 됨 — 원문 그대로(강조 없이) 보여야 함."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제56조의3①", "change_type": "신설", "no_change": False,
                "moved_from": None, "old_text_is_context": True,
                "old_text": "① 통짜 조문 전체 문장입니다.",
                "new_text": "① 완전히 다른 새 문장입니다.",
            },
            "summary": "새 기준을 마련한다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert '<mark class="diff-changed">' not in html
    assert "완전히 다른 새 문장입니다" in html


def test_index_deletion_shows_old_text_without_diff_or_placeholder():
    """실측: 삭제 항목은 old_text_is_context=True로
    저장되지만(loader.py의 일괄 규칙), 삭제의 old_text는 구조확장과 달리
    애매한 공유 맥락이 아니라 "삭제된 바로 그 문장"이라 정반대로 다뤄야
    함 — 개정 전 문장은 (강조 없이) 그대로 보여주고, new_text 쪽은 합성
    표시("<삭  제>") 대신 "삭제되었습니다"라는 사람이 읽는 문구를 보여줌
    (후속 요청: "개정후에도 삭제되었다고 보여줘")."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "(삭제됨 — 개정 전 제46조①항 참고)",
                "change_type": "삭제", "no_change": False,
                "moved_from": None, "old_text_is_context": True,
                "old_text": "① 국가기관등은 정보통신망을 통하여…",
                "new_text": "<삭  제>",
            },
            "summary": "관련 조항이 삭제되었습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    # readable_text 필터가 선행 항 기호("①")를 굵게 감싸므로(
    # 후속 요청) 원문이 토막 없이 그대로 이어붙진 않음 — 기호와 본문이
    # 각각 온전히 들어있는지를 확인함.
    assert "<strong>①</strong>" in html
    assert "국가기관등은 정보통신망을 통하여…" in html
    assert '<mark class="diff-changed">' not in html
    assert "&lt;삭  제&gt;" not in html
    assert "<삭  제>" not in html
    assert "삭제되었습니다" in html


def test_readable_text_breaks_run_together_clauses_onto_separate_lines():
    """실측(지능정보화 기본법 제46조①~⑦ 통짜
    텍스트): 신구법 비교 API가 조문 전체를 <P> 블록 하나로 통짜로 주다 보니,
    "원문 보기"에 old_text를 그대로 뿌리면 항/호 경계 없이 벽처럼 붙어 나와
    "구분이 안 된다"는 지적을 받았음. text.split.split_all()(이미 검색에
    쓰며 소수점ㆍ날짜ㆍ괄호참조 오탐을 걸러내도록 다듬어진 로직)을 재사용해
    조각 경계마다 줄바꿈만 넣음."""
    from webapp.app import _readable_text

    text = "①  국가기관등은 정보통신망을…1. 웹사이트2. 이동통신단말장치② 지능정보서비스 제공자는…"
    result = _readable_text(text)
    lines = result.split("\n")
    assert lines[0].startswith("<strong>①</strong>")
    assert lines[1] == "<strong>1.</strong> 웹사이트"
    assert lines[2] == "<strong>2.</strong> 이동통신단말장치"
    assert lines[3].startswith("<strong>②</strong>")


def test_readable_text_bolds_only_leading_marker_not_whole_line():
    """실측("7. 영유아의 인권 보호에 관한
    업무"): 줄바꿈만으론 부족하고, 줄 맨 앞의 "7." 같은 항/호/목 기호
    "숫자기호들만" 굵게 강조해야 한다는 후속 요청. 본문까지 통째로
    굵어지면 안 됨."""
    from webapp.app import _readable_text

    result = str(_readable_text("7. 영유아의 인권 보호에 관한 업무"))
    assert result == "<strong>7.</strong> 영유아의 인권 보호에 관한 업무"
    assert "<strong>7. 영유아" not in result  # 본문까지 같이 굵어지면 안 됨


def test_readable_text_returns_plain_text_when_no_markers():
    from webapp.app import _readable_text

    assert _readable_text("항/호 기호 없는 평범한 한 문장입니다.") == "항/호 기호 없는 평범한 한 문장입니다."
    assert _readable_text("") == ""
    assert _readable_text(None) == ""


def test_index_shows_no_change_kind_even_though_change_type_is_amend():
    """no_change=True 인 항목은 change_type("개정")이 아니라 "변경없음"으로
    보여야 함 — summarizer/report/builder.py의 _kind_of()와 동일 규칙."""
    laws = [_law_row(article_summaries=[
        {
            "unit": {
                "location_label": "제92조③", "change_type": "개정",
                "no_change": True, "moved_from": None,
            },
            "summary": "이 항목은 이번 개정에서 내용이 바뀌지 않았습니다.",
            "caveats": [], "error": None,
        }
    ])]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert "변경없음" in html


def test_index_never_renders_highlight_box():
    """설계: "확인이 필요한 항목" 박스를
    완전히 없앴음. caveats/verifier_issues가 있어도 화면에 별도 경고
    박스로 노출되면 안 됨 — 데이터 자체(DB)는 남아있어도 웹페이지엔
    안 보여줌."""
    laws = [_law_row(
        article_summaries=[
            {
                "unit": {"location_label": "제56조의3①", "change_type": "신설", "no_change": False, "moved_from": None},
                "summary": "정보시스템 등급산정 기준을 마련한다.",
                "caveats": ["[제56조의3①] 구조확장(구법미분리) — 개정 전 문장이 이 위치에 정확히 대응하지 않음"],
                "error": None,
            }
        ],
        verifier_issues=[
            {"severity": "high", "where": "제5조", "problem": "이동/신설 판정이 원문과 다름"},
        ],
    )]
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=laws))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert 'class="highlight-box"' not in html
    assert "확인이 필요한 항목" not in html


def test_download_builds_report_from_the_week_rows(tmp_path):
    """주간 리포트 다운로드는 미리 만들어 둔 파일을 찾아 주는 게 아니라,
    화면에 보이는 그 행들로 그 자리에서 만듦 — 한 주에 여러 배치가
    걸쳐도 화면과 파일이 어긋나지 않게 하려는 것."""
    from lawtrack.week import last_full_week

    hwpx = tmp_path / "week.hwpx"
    hwpx.write_bytes(b"fake-hwpx-bytes")
    seen: list[tuple] = []

    def fake_builder(laws, week_from, week_to):
        seen.append((laws, week_from, week_to))
        return hwpx

    app = create_app(
        repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()]),
        week_report_builder=fake_builder,
    )
    resp = app.test_client().get("/download")

    assert resp.status_code == 200
    assert resp.data == b"fake-hwpx-bytes"
    assert len(seen) == 1
    assert (seen[0][1], seen[0][2]) == last_full_week()


def test_download_404_when_report_build_fails(tmp_path):
    app = create_app(
        repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()]),
        week_report_builder=lambda laws, f, t: None,
    )
    resp = app.test_client().get("/download")

    assert resp.status_code == 404


def test_download_404_when_week_is_empty():
    """개정이 없는 주에는 만들 보고서 자체가 없음 — 빈 파일을 만들지
    않고 404 로 끝냄."""
    def fail_if_called(laws, week_from, week_to):
        raise AssertionError("빈 주에는 보고서를 만들면 안 됨")

    app = create_app(
        repo=_FakeRepo(batch_date=None), week_report_builder=fail_if_called,
    )
    resp = app.test_client().get("/download")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 기간 지정 즉석 조회 (webapp/live.py 연동) — live_check를 주입해 실
# API/LLM/DB 없이 라우팅·렌더링만 확인함.
# ---------------------------------------------------------------------------

def _period_result(**kw) -> PeriodResult:
    base = dict(
        window_key="5d", from_date=date(2026, 7, 15), to_date=date(2026, 7, 20),
        laws=[], hwpx_path=None, computed_at=0.0, newly_detected=0, errors=[],
    )
    base.update(kw)
    return PeriodResult(**base)


def test_index_period_mode_calls_live_check_with_key():
    calls = []

    def fake_live_check(window_key):
        calls.append(window_key)
        return _period_result(window_key=window_key)

    app = create_app(repo=_FakeRepo(batch_date=None), live_check=fake_live_check)
    resp = app.test_client().get("/summary?period=2w")

    assert resp.status_code == 200
    assert calls == ["2w"]


def test_index_rejects_unknown_period():
    def fail_if_called(window_key):
        raise AssertionError("알 수 없는 기간이면 live_check가 호출되면 안 된다")

    app = create_app(repo=_FakeRepo(batch_date=None), live_check=fail_if_called)
    resp = app.test_client().get("/summary?period=bogus")

    assert resp.status_code == 400


def test_index_period_renders_period_laws_not_batch_repo():
    """period 모드에서는 _repo.fetch_by_batch가 아니라 live_check 결과를
    보여줘야 함 — _FakeRepo에 다른 batch_date를 넣어도 무시되는지 확인."""
    laws = [_law_row(law_name="기간조회법", headline="기간 내 발견된 개정")]
    result = _period_result(laws=laws)
    app = create_app(
        repo=_FakeRepo(batch_date=date(2020, 1, 1)),  # period 모드에선 쓰이지 않아야 함
        live_check=lambda window_key: result,
    )
    html = app.test_client().get("/summary?period=5d").get_data(as_text=True)

    assert "기간조회법" in html
    assert "기간 내 발견된 개정" in html


def test_index_period_shows_period_label_and_range():
    result = _period_result(window_key="1m", from_date=date(2026, 6, 1), to_date=date(2026, 7, 1))
    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    html = app.test_client().get("/summary?period=1m").get_data(as_text=True)

    assert "최근 1개월" in html
    assert "2026-06-01" in html and "2026-07-01" in html


def test_index_period_empty_shows_period_specific_message():
    result = _period_result(laws=[])
    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    html = app.test_client().get("/summary?period=2w").get_data(as_text=True)

    assert "동안 감지된 개정사항이 없습니다" in html
    # 배치 모드 전용 안내(run_weekly.py 실행법)는 기간 모드에서 안 보여야 함.
    assert "python scripts/run_weekly.py --full" not in html


def test_index_period_shows_partial_error_note():
    result = _period_result(errors=["전자정부법(009199): API 오류"])
    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    html = app.test_client().get("/summary?period=5d").get_data(as_text=True)

    assert "1건 확인에 실패했습니다" in html


def test_index_batch_mode_shows_no_period_error_note():
    app = create_app(repo=_FakeRepo(batch_date=date(2026, 7, 20), laws=[_law_row()]))
    html = app.test_client().get("/summary").get_data(as_text=True)

    assert "확인에 실패했습니다" not in html


def test_download_period_serves_live_hwpx(tmp_path):
    hwpx = tmp_path / "live_period.hwpx"
    hwpx.write_bytes(b"fake-period-hwpx-bytes")
    result = _period_result(hwpx_path=hwpx)

    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    resp = app.test_client().get("/download?period=5d")

    assert resp.status_code == 200
    assert resp.data == b"fake-period-hwpx-bytes"


def test_download_period_404_when_no_changes_in_period():
    result = _period_result(hwpx_path=None)
    app = create_app(repo=_FakeRepo(batch_date=None), live_check=lambda k: result)
    resp = app.test_client().get("/download?period=5d")

    assert resp.status_code == 404


def test_download_rejects_unknown_period():
    def fail_if_called(window_key):
        raise AssertionError("알 수 없는 기간이면 live_check가 호출되면 안 된다")

    app = create_app(repo=_FakeRepo(batch_date=None), live_check=fail_if_called)
    resp = app.test_client().get("/download?period=bogus")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /period-check, /period-status — 로딩 화면이 폴링하는 논블로킹 진입점.
# sweep_starter/progress_getter를 주입해 실 스레드/API 없이 확인함.
# ---------------------------------------------------------------------------

def test_period_check_returns_ready_true_when_no_wait_needed():
    app = create_app(repo=_FakeRepo(batch_date=None), sweep_starter=lambda k: False)
    resp = app.test_client().get("/period-check?period=5d")

    assert resp.status_code == 200
    assert resp.get_json() == {"ready": True}


def test_period_check_returns_ready_false_when_sweep_started():
    calls = []

    def sweep_starter(window_key):
        calls.append(window_key)
        return True

    app = create_app(repo=_FakeRepo(batch_date=None), sweep_starter=sweep_starter)
    resp = app.test_client().get("/period-check?period=2w")

    assert resp.get_json() == {"ready": False}
    assert calls == ["2w"]


def test_period_check_rejects_unknown_period():
    app = create_app(repo=_FakeRepo(batch_date=None), sweep_starter=lambda k: False)
    resp = app.test_client().get("/period-check?period=bogus")

    assert resp.status_code == 400


def test_period_check_requires_period_param():
    app = create_app(repo=_FakeRepo(batch_date=None), sweep_starter=lambda k: False)
    resp = app.test_client().get("/period-check")

    assert resp.status_code == 400


def test_period_status_reports_in_progress_stage():
    app = create_app(
        repo=_FakeRepo(batch_date=None),
        progress_getter=lambda k: {"stage": "국가법령정보 API 확인 중", "detail": "42/102건"},
    )
    resp = app.test_client().get("/period-status?period=5d")

    assert resp.get_json() == {
        "stage": "국가법령정보 API 확인 중", "detail": "42/102건", "done": False,
    }


def test_period_status_reports_done_when_stage_is_완료():
    app = create_app(
        repo=_FakeRepo(batch_date=None),
        progress_getter=lambda k: {"stage": "완료", "detail": ""},
    )
    resp = app.test_client().get("/period-status?period=5d")

    assert resp.get_json()["done"] is True


def test_period_status_rejects_unknown_period():
    app = create_app(repo=_FakeRepo(batch_date=None), progress_getter=lambda k: {"stage": "", "detail": ""})
    resp = app.test_client().get("/period-status?period=bogus")

    assert resp.status_code == 400
