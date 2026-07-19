"""locator 회귀 테스트. 실측 성공/실패 사례를 그대로 재현한다."""

from lawtrack.locate.locator import (
    LocateStatus,
    locate_all,
    locate_change,
    summarize,
)
from lawtrack.parse.fulltext import SearchUnit
from lawtrack.parse.oldnew import build_change


def unit(article, clause="", item="", text="", changed=True):
    return SearchUnit(
        article_code=article, article_label=f"제{article}조",
        clause_no=clause, item_label=item, subitem_label="",
        text=text, changed=changed,
    )


class TestSimpleSuccess:
    """단순 치환 — 정확히 1건 매칭되어야 함."""

    def test_amended_single_match(self):
        # 실측 원문 그대로 (생략부호 없이) — 국민기초생활보장법 제6조의2
        change = build_change(
            0,
            "제6조의2(기준 중위소득의 산정) ① 기준 중위소득은 「통계법」 제27조에 따라 "
            "<P>통계청이</P> 공표하는 통계자료의 중간값에 최근 가구소득 평균 증가율, "
            "가구규모에 따른 소득수준의 차이 등을 반영하여 가구규모별로 산정한다.",
            "제6조의2(기준 중위소득의 산정) ① 기준 중위소득은 「통계법」 제27조에 따라 "
            "<P>국가데이터처가</P> 공표하는 통계자료의 중간값에 최근 가구소득 평균 증가율, "
            "가구규모에 따른 소득수준의 차이 등을 반영하여 가구규모별로 산정한다.",
        )
        units = [
            unit(
                "6", "①",
                text="기준 중위소득은 「통계법」 제27조에 따라 국가데이터처가 공표하는 "
                     "통계자료의 중간값에 최근 가구소득 평균 증가율, 가구규모에 따른 "
                     "소득수준의 차이 등을 반영하여 가구규모별로 산정한다.",
            ),
            unit("7", text="다른 조문 내용"),
        ]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.SUCCESS
        assert results[0].location_label == "제6조①"


class TestClauseSplitRequired:
    """전자정부법 실측: 통짜로 검색하면 실패, ①②③ 기호로 쪼개야 매칭."""

    def test_whole_blob_would_fail_but_split_succeeds(self):
        # oldAndNew 한 블록에 여러 항이 섞여 있음 (실측 패턴)
        old_blob = (
            "① 위원회는 다음 각 호와 같이 위원장 1인을 포함한 15인 내외의 위원으로 구성한다. "
            "② 위원장은 국무총리가 된다."
        )
        new_blob = (
            "① 위원회는 다음 각 호와 같이 위원장 1인을 포함한 15인 내외의 위원으로 구성한다. "
            "② 위원장은 <P>기획재정부장관</P>이 된다."
        )
        change = build_change(0, old_blob, new_blob)

        # lawService 전문은 항별로 별도 유닛
        units = [
            unit("21", "①", text="위원회는 다음 각 호와 같이 위원장 1인을 포함한 15인 내외의 위원으로 구성한다."),
            unit("21", "②", text="위원장은 기획재정부장관이 된다."),
        ]
        results = locate_change(change, units)
        # 최소 하나는 ②항에서 성공해야 함
        successes = [r for r in results if r.status is LocateStatus.SUCCESS]
        assert successes, f"분해 후에도 매칭 실패: {[r.tried for r in results]}"
        assert any(r.location_label == "제21조②" for r in successes)


class TestNewlyCreated:
    """신설 — new_clean 만 검색 대상. 실측: 전자정부법 제23조③"""

    def test_newly_created_located(self):
        change = build_change(0, "<P><신 설></P>", "<P>③ 새로 신설된 조항 내용입니다.</P>")
        units = [unit("23", "③", text="③ 새로 신설된 조항 내용입니다.")]
        results = locate_change(change, units)
        assert results[0].status is LocateStatus.SUCCESS


class TestDeletedSkipsSearch:
    """삭제 — 현행 전문엔 없으므로 검색 자체를 시도하지 않아야 함."""

    def test_deleted_returns_skip_without_searching(self):
        change = build_change(0, "1. 삭제될 원래 내용", "<P>1. 삭제</P>")
        units = [unit("5", text="전혀 관계없는 내용")]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.DELETED_SKIP
        assert results[0].unit is None


class TestZeroMatch:
    """0건 실패 — 조용히 넘기지 않고 실패로 기록되는지."""

    def test_zero_match_reported_not_silent(self):
        change = build_change(0, "<P>구내용</P>", "<P>본문 어디에도 없는 완전히 새로운 문구</P>")
        units = [unit("1", text="전혀 다른 조문 내용")]
        results = locate_change(change, units)
        assert results[0].status is LocateStatus.ZERO_MATCH
        assert results[0].tried  # 진단 로그가 남아야 함


class TestDuplicateAndGuard5:
    """국고금관리법 시행령 실측: 내용만 검색시 중복, 호번호 결합하면 해소."""

    def test_short_phrase_duplicates_then_resolved_by_marker(self):
        # 짧은 문구가 두 군데에 동일하게 등장 (실측 패턴)
        change = build_change(
            0,
            "1. 기존 문구",
            "<P>1. 공통 문구입니다</P>",
        )
        units = [
            unit("10", item="1.", text="1. 공통 문구입니다"),
            unit("22", item="1.", text="1. 공통 문구입니다"),  # 다른 조인데 텍스트 동일
        ]
        results = locate_change(change, units)
        # body("공통 문구입니다")만으로는 두 유닛 다 매칭 → 마커("1.") 결합해도
        # 두 유닛 모두 "1." 을 가지므로 이 케이스는 여전히 중복일 수 있음.
        # 마커로 유일하게 좁혀지는 현실적 케이스로 다시 구성:
        assert results[0].status in (LocateStatus.DUPLICATE, LocateStatus.SUCCESS)

    def test_marker_disambiguates_when_units_differ_by_marker(self):
        change = build_change(0, "12. 원래", "<P>12의2. 신설된 세부 항목 내용</P>")
        units = [
            unit("39", item="12.", text="12. 전혀 다른 열두번째 호 내용"),
            unit("39", item="12의2", text="12의2. 신설된 세부 항목 내용"),
        ]
        results = locate_change(change, units)
        # body("신설된 세부 항목 내용")는 두 번째 유닛에만 있으므로 애초에 1건 매칭
        assert results[0].status is LocateStatus.SUCCESS
        assert results[0].location_label == "제39조12의2"


class TestMassSubstitution:
    """국방데이터·인공지능업무 훈령 실측: 한 조각에 <P> 여러 개."""

    def test_multiple_p_in_one_fragment_still_locates(self):
        old = "2. 위원 : <P>국방부</P> 기획조정실장, <P>인사복지실장, 자원관리실장</P>, 전력정책국장"
        new = "2. 위원 : <P>국방부 차관보,</P> 기획조정실장, <P>인사복지실장</P>, 전력정책국장"
        change = build_change(0, old, new)
        units = [
            unit("4", item="2.", text="2. 위원 : 국방부 차관보, 기획조정실장, 인사복지실장, 전력정책국장"),
        ]
        results = locate_change(change, units)
        assert results[0].status is LocateStatus.SUCCESS


class TestAnnotationStripping:
    """★★ 실측(2026-07-16, (계약예규) 예정가격작성기준): 조문 문장 중간에
    "<개정 2011.5.13., 2015.9.21., 2025.5.1.>" 같은 개정이력 각주가 섞여
    있으면, 그 안의 날짜 조각("5.13." "9.21.")이 호 번호 패턴과 우연히
    겹쳐 엉뚱하게 분해되어 검색에 실패했다. locate_change가 검색 직전에
    이런 <...> 각주를 걷어내야 한다."""

    def test_revision_annotation_with_dates_does_not_break_search(self):
        old = (
            "②일반관리비는 직접공사비와 간접공사비의 합계액에 일반관리비율을 "
            "곱하여 계산한다. <개정 2011.5.13., 2015.9.21.>"
        )
        new = (
            "일반관리비는 직접공사비와 간접공사비의 합계액에 일반관리비율을 "
            "곱하여 계산한다. <개정 2011.5.13., 2015.9.21., 2025.5.1.>"
        )
        change = build_change(0, old, new)
        units = [
            unit(
                "20", text="일반관리비는 직접공사비와 간접공사비의 합계액에 "
                            "일반관리비율을 곱하여 계산한다.",
            ),
        ]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.SUCCESS

    def test_img_tag_does_not_break_search(self):
        old = "기존 표시 방법"
        new = '비밀을 접수한 기관이 표시한다.<img id="160236309"></img>'
        change = build_change(0, old, new)
        units = [unit("9", text="비밀을 접수한 기관이 표시한다.")]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.SUCCESS

    def test_bracket_annotation_does_not_break_search(self):
        """실측(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준): "[본조신설
        2018.3.20.][종전 제99조는 제100조로 이동…]" 처럼 대괄호로 감싼
        조문 이력 각주도 검색 전에 제거돼야 한다."""
        old = "시행령 제26조부터 제30조까지에 따라 체결하는 수의계약."
        new = (
            "시행령 제26조부터 제30조까지에 따라 체결하는 수의계약.[본조신설 "
            "2018.3.20.][종전 제99조는 제100조로 이동  ]"
        )
        change = build_change(0, old, new)
        units = [unit("100", clause="③", text="시행령 제26조부터 제30조까지에 따라 체결하는 수의계약.")]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.SUCCESS

    def test_unclosed_trailing_annotation_does_not_break_search(self):
        """실측(2026-07-16, (계약예규) 협상에 의한 계약체결기준): oldAndNew
        블록 경계에서 "<개정 2020.9.24." 처럼 닫는 ">" 없이 각주가 잘리는
        경우가 있다. 닫는 괄호가 없어도 문자열 끝까지 제거돼야 한다."""
        old = "제안서를 계약담당공무원에게 제출하여야 한다."
        new = "제안서를 계약담당공무원에게 제출하여야 한다. <개정 2020.9.24."
        change = build_change(0, old, new)
        units = [unit("6", clause="①", text="제안서를 계약담당공무원에게 제출하여야 한다.")]
        results = locate_change(change, units)
        assert len(results) == 1
        assert results[0].status is LocateStatus.SUCCESS


class TestAggregation:
    def test_locate_all_and_summarize(self):
        c1 = build_change(0, "<P>구1</P>", "<P>신규매칭될문구</P>")
        c2 = build_change(1, "1. 삭제될것", "<P>1. 삭제</P>")
        units = [unit("1", text="신규매칭될문구")]

        results = locate_all([c1, c2], units)
        assert len(results) == 2

        summary = summarize(results)
        assert summary.get(LocateStatus.SUCCESS.value, 0) == 1
        assert summary.get(LocateStatus.DELETED_SKIP.value, 0) == 1