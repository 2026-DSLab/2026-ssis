"""lawtrack.text.split.split_all() 회귀 테스트. 케이스는 실측 사례 기반."""

from lawtrack.text.split import Level, searchable_fragments


class TestItemPreambleMarkerPreserved:
    """실측 발견(영유아보육법 제48조②2.):
    호 하나가 "본문 + 가./나./다." 구조면(다음 각 목에 해당하는 사람 …)
    split_by_subitem()이 본문(목 목록 앞 전제문)을 marker=None 인 별도
    Fragment로 떼어내는데, 이 전제문도 개념상 그 호(item, 예: "2.")에
    속함. 항→호 전제문 유실(전자정부법 제56조의2① 등,
    이미 고쳐짐)과 완전히 같은 문제가 호→목 한 층 아래에서 재현된 것 —
    실측: 신구법 API 원문은 "2. 제1항제3호에 해당하는 경우…"로 시작했는데
    (구/신 양쪽 다), DB에 저장된 new_text 만 "2. "가 빠진 채였음. 위치확정된
    조각(fragment.raw)이 곧 화면/보고서의 new_text가 되므로, 이 라벨
    유실이 그대로 사용자에게 노출됐음."""

    def test_item_with_trailing_subitems_keeps_item_marker_on_preamble(self):
        text = (
            "2. 제1항제3호에 해당하는 경우: 10년. 다만, 다음 각 목에 해당하는 "
            "사람의 경우 형의 집행이 종료(종료된 것으로 보는 경우를 포함한다)"
            "되거나 집행이 면제된 날 또는 형의 집행유예가 확정된 날부터 20년 "
            "이내에서 범죄의 종류, 형기의 장단 및 재범위험성 등을 고려하여 "
            "대통령령으로 정하는 기간이 지나지 아니하면 자격을 재교부할 수 "
            "없다.가. 「아동복지법」 제3조제7호의2에 따른 아동학대관련범죄로 "
            "금고 이상의 실형을 선고받은 사람나. 「아동복지법」 제3조제7호의2에 "
            "따른 아동학대관련범죄로 금고 이상의 형의 집행유예를 선고받은 사람"
        )
        frags = searchable_fragments(text)

        preamble = frags[0]
        assert preamble.raw.startswith("2. 제1항제3호에 해당하는 경우")
        assert frags[1].marker == "가."
        assert frags[2].marker == "나."

    def test_subitem_markers_unaffected_by_preamble_fix(self):
        """전제문에 호 마커를 되살려 붙이는 수정이, 뒤따르는 가./나. 조각
        자체의 마커까지 건드리면 안 됨.

        "…설명입니다."처럼 "다"로 끝나는 문장은 그 "다."가 목(가나다…)
        문자 패턴과 우연히 겹쳐 이번 수정과 무관한 기존 공백 아티팩트를
        유발함(실측: 실제 프로덕션 데이터에도 "…재교부할 수 없 다."처럼
        이미 있었고, git stash로 이번 수정 이전 코드에서도 재현 확인함) —
        이 테스트의 관심사가 아니므로 그 글자로 안 끝나는 문장을 씀."""
        text = "3. 본문 설명임.가. 첫째나. 둘째"
        frags = searchable_fragments(text)

        assert frags[0].raw.startswith("3. 본문 설명임")
        assert frags[1].marker == "가."
        assert frags[1].raw == "가. 첫째"
        assert frags[2].marker == "나."
        assert frags[2].raw == "나. 둘째"

    def test_clause_with_trailing_subitems_still_keeps_clause_marker(self):
        """회귀 방지: 항 바로 아래 목이 오는 기존 케이스(이미
        고쳐진 경로)가 이번 수정으로 깨지지 않아야 함."""
        text = "① 다음 각 목에 해당하는 자는 제외한다.가. 첫째나. 둘째"
        frags = searchable_fragments(text)

        assert frags[0].raw.startswith("① 다음 각 목에 해당하는 자는 제외한다")

    def test_item_without_trailing_subitems_unaffected(self):
        """목 목록이 없는 보통의 호는 기존처럼 item 자체가 그대로 남아야
        함(이번 수정이 손대는 경로가 아님)."""
        text = "5. 금고 이상의 실형을 선고받고 집행이 면제된 날부터 5년"
        frags = searchable_fragments(text)

        assert len(frags) == 1
        assert frags[0].level is Level.ITEM
        assert frags[0].marker == "5."
        assert frags[0].raw == text
