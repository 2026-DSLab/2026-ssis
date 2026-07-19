"""parse/oldnew.py classify() 회귀 테스트. 케이스는 실측 사례 기반."""

from lawtrack.parse.oldnew import ChangeType, classify, extract_admrul_unchanged


class TestDeletedWholeBlockMarker:
    """실측(2026-07-16, 지능정보화 기본법·재난적의료비 지원에 관한 법률):
    조/항 전체가 삭제되면 "<삭  제>"처럼 꺾쇠괄호로 감싸이고 공백 개수도
    들쭉날쭉하다. 예전엔 이 형태를 못 잡아 DELETED로 분류되지 못하고
    AMENDED로 잘못 분류되어, 존재하지 않는 "<삭  제>" 텍스트를 새 전문에서
    찾으려다 항상 0건실패로 죽었다."""

    def test_bracketed_deletion_with_irregular_spacing(self):
        old = "<P>①  국가기관등은 정보통신망을 통하여 정보나 서비스를 제공할 때…</P>"
        new = "<P><삭  제></P>"
        assert classify(old, new) is ChangeType.DELETED

    def test_bracketed_deletion_no_extra_space(self):
        old = "<P>기존 조문 내용</P>"
        new = "<P><삭제></P>"
        assert classify(old, new) is ChangeType.DELETED

    def test_numbered_item_deletion_still_works(self):
        """기존에 이미 되던 케이스(예: '4. 삭제') — 회귀 방지."""
        old = "<P>4. 정보통신접근성 품질인증 운영 지원</P>"
        new = "<P>4. 삭제</P>"
        assert classify(old, new) is ChangeType.DELETED

    def test_newly_created_still_works(self):
        """대칭 케이스(신설) — 회귀 방지."""
        old = "<P><신  설></P>"
        new = "<P>② 새로 추가된 조항 내용</P>"
        assert classify(old, new) is ChangeType.NEWLY_CREATED

    def test_unwrapped_deletion_with_clause_marker_and_date(self):
        """실측(2026-07-16, 조달청 내자구매업무 처리규정): <P> 태그로
        전혀 감싸이지 않은 채(new_fragments=()) "③ <삭제> (2004.11.12.)"
        처럼 항 기호+삭제마커+삭제일자가 통째로 오는 경우도 DELETED로
        분류돼야 한다."""
        old = "③ 제1항 및 제2항에도 불구하고 다음 각 호의 어느 하나에 해당하는 경우…"
        new = "③ <삭제> (2004.11.12.)"
        assert classify(old, new) is ChangeType.DELETED

    def test_unwrapped_deletion_without_date(self):
        old = "④ 제1항에 따른 안내공고에 불구하고 다음 각 호의…"
        new = "④ <삭제>"
        assert classify(old, new) is ChangeType.DELETED

    def test_short_real_content_not_mistaken_for_deletion(self):
        """'삭제'라는 단어가 우연히 들어간 진짜 내용까지 삭제로 오인하면
        안 된다 — 길이 10자 이하 + 부분일치 조건으로 과매칭을 막는다."""
        old = "<P>기존 문구</P>"
        new = "<P>자료를 삭제하는 절차를 마련한다</P>"  # 10자 초과, 실제 개정 내용
        assert classify(old, new) is not ChangeType.DELETED


class TestPartialMarkerNotWholeBlock:
    """★★ 실측(2026-07-16, 산업재해보상보험법 제116조②): "<후단 신설>"
    "<단서 신설>"처럼 "일부만 새로 생겼다"는 마커도 예전엔 "신설"이라는
    부분 문자열만 보고 조각 전체를 NEWLY_CREATED로 잘못 분류했다. 실제
    old_text 에는 "② 사업주는…하여야 한다"라는 진짜 기존 내용이 그대로
    남아있는데도 change_type="신설"로 나가, old_text가 있는데도 "신설"
    이라고 LLM팀에 잘못 전달되고 있었다(오늘 발견한 위치재배치 플래그
    로직도 change_type==NEWLY_CREATED 를 신호로 쓰므로 이 오분류의 여파가
    거기까지 번진다)."""

    def test_trailing_clause_insertion_not_whole_block_created(self):
        old = (
            "② 사업주는 보험급여를 받을 사람이 보험급여를 받는 데에 필요한 "
            "증명을 요구하면 그 증명을 하여야 <P>한다</P>. <P><후단 신설></P>"
        )
        new = (
            "② 사업주는 보험급여를 받을 사람이 보험급여를 받는 데에 필요한 "
            "증명을 요구하면 그 증명을 하여야 <P>하고, 대통령령으로 정하는 "
            "자료의 제공을 요청하면 정당한 사유가 없으면 이에 따라야 한다</P>. "
            "<P>이 경우 증명 또는 자료의 제공 절차 및 방법 등에 관하여 필요한 "
            "사항은 대통령령으로 정한다.</P>"
        )
        assert classify(old, new) is ChangeType.AMENDED

    def test_proviso_insertion_not_whole_block_created(self):
        old = "① 협상에 참가하고자 하는 자는 다음 각 호에 해당하여야 <P>한다</P>. <P><단서 신설></P>"
        new = "① 협상에 참가하고자 하는 자는 다음 각 호에 해당하여야 <P>한다</P>. <P>다만, 예외로 한다.</P>"
        assert classify(old, new) is ChangeType.AMENDED

    def test_pure_whole_block_created_still_works(self):
        """대칭 회귀 방지: 진짜 "조각 전체가 신설"인 경우는 그대로 유지."""
        assert classify("<P><신  설></P>", "<P>새 항 내용</P>") is ChangeType.NEWLY_CREATED
        assert classify("<P><신설></P>", "<P>새 항 내용</P>") is ChangeType.NEWLY_CREATED

    def test_range_deletion_still_recognized(self):
        """★★ 실측 발견(2026-07-16, 표준 개인정보 보호지침): "후단/단서/전단"
        제외 로직을 처음엔 "정확히 '삭제' 두 글자만" 요구하는 식으로 너무
        엄격하게 짰다가, "1. ∼5. 삭제"(1호부터 5호까지 범위로 전부 삭제)
        같은 정상 케이스까지 놓치는 회귀를 만들었다 — 재검증 스윕에서
        미확정 건수가 늘어난 것으로 발견했다. 범위 삭제 표기는 "후단/단서/
        전단" 접두어가 없으므로 정상적으로 DELETED 로 인정되어야 한다."""
        assert classify("<P>1. 삭제</P>", "<P>1. ∼5. 삭제</P>") is ChangeType.DELETED
        assert classify("<P>1. 삭제</P>", "<P>1. ∼3. 삭제</P>") is ChangeType.DELETED


class TestExtractAdmrulUnchanged:
    """실측(2026-07-18, (계약예규) 전자정부사업관리 위탁에 관한 규정
    42496 제12조): admrul은 항제개정유형 같은 공식 태그가 없어 신구법
    비교의 "(생략)/(현행과 같음)" 스킵 표시를 대신 근거로 쓴다. 스킵
    블록은 자기 조문 헤더를 반복하지 않는 경우가 많아(제12조③ 블록은
    "제12조" 없이 "③ (생 략)"만 옴) 앞선 블록에서 본 조문 컨텍스트를
    이어받아야 한다 — 실제 API 응답을 그대로 재현한 케이스."""

    def test_context_inherited_from_earlier_block_without_own_header(self):
        old_texts = [
            "<P>제12조(위탁용역 보정대가) ① (생  략)</P>",
            "<P>②위탁용역이 다수의 위탁대상사업을 포함하는 경우…</P>",
            "<P>③ (생  략)</P>",
        ]
        new_texts = [
            "<P>제12조(위탁용역 보정대가) ① (현행과 같음)</P>",
            "<P>②위탁용역이 …로 산정한다.</P>",
            "<P>③ (현행과 같음)</P>",
        ]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제12조"})
        assert result == {"제12조": ["①", "③"]}

    def test_item_number_range_skip(self):
        old_texts = ["<P>제2조(정의) 1. ∼ 4. (생 략)</P>"]
        new_texts = ["<P>제2조(정의) 1. ∼ 4. (현행과 같음)</P>"]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제2조"})
        assert result == {"제2조": ["1.", "2.", "3.", "4."]}

    def test_untouched_article_excluded(self):
        """touched_articles에 없는 조문은 이번 배치의 관심사가 아니므로
        스킵 표시가 있어도 결과에 포함하지 않는다."""
        old_texts = ["<P>제9조(적용범위) ① (생 략)</P>"]
        new_texts = ["<P>제9조(적용범위) ① (현행과 같음)</P>"]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제2조"})
        assert result == {}

    def test_no_skip_marker_returns_empty(self):
        old_texts = ["<P>제3조(정의) 이 훈령에서 사용하는 용어의 뜻은 다음과 같다.</P>"]
        new_texts = ["<P>제3조(정의) 이 훈령에서 사용하는 용어의 뜻은 다음과 같다(개정).</P>"]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제3조"})
        assert result == {}

    def test_item_labels_disambiguated_by_enclosing_clause(self):
        """실측(2026-07-18, 공공기관의 데이터베이스 표준화 지침 제5조): 호
        번호는 항마다 새로 1부터 시작한다. ①과 ③이 둘 다 "1.~6."을 갖고
        내용이 서로 다르면, 항 구분 없이 "1."만 내보내면 어느 항의 1.인지
        알 수 없어 오독 위험이 생긴다 — 항 라벨을 접두어로 붙여야 한다."""
        old_texts = [
            "<P>제5조(공공기관의 역할) ① 공공기관의 장은…다음 각 호의 업무를 수행하여야 한다.</P>",
            "<P>1. 다음 각목에 해당되는 공공데이터베이스 표준화 관리</P>",
            "<P>3. ∼ 6. (생  략)</P>",
            "<P>② 실무담당자는…총괄하여 수행하여야 한다.</P>",
            "<P>③ 업무담당자는…다음 각 호의 업무를 수행하여야 한다.</P>",
            "<P>1. (생  략)</P>",
        ]
        new_texts = [
            "<P>제5조(공공기관의 역할) ① 공공기관의 장은…다음 각 호의 업무를 수행하여야 한다.</P>",
            "<P>1. 다음 각목에 해당되는 공공데이터베이스 표준화 관리(개정됨)</P>",
            "<P>3. ∼ 6. (현행과 같음)</P>",
            "<P>실무담당자는…총괄하여 수행하여야 한다.</P>",
            "<P>③ 업무담당자는…다음 각 호의 업무를 수행하여야 한다.</P>",
            "<P>1. (현행과 같음)</P>",
        ]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제5조"})
        assert result == {"제5조": ["①3.", "①4.", "①5.", "①6.", "③1."]}

    def test_middle_dot_separator_recognized(self):
        """실측(2026-07-18, 보안업무규정 시행규칙 제56조①③): "∼" 대신
        가운뎃점(·, U+00B7)으로 인접한 두 호를 잇는 표기("3.·4. (생 략)")도
        스킵 표시로 인식해야 한다."""
        old_texts = [
            "<P>제56조(조사기관 및 조사대상) ① 국가정보원장은…</P>",
            "<P>3.·4. (생  략)</P>",
        ]
        new_texts = [
            "<P>제56조(조사기관 및 조사대상) ① 국가정보원장은…(개정)</P>",
            "<P>3.·4. (현행과 같음)</P>",
        ]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제56조"})
        assert result == {"제56조": ["①3.", "①4."]}

    def test_branch_numbered_range_out_of_scope(self):
        """★ 설계에서 명시적으로 out of scope: "6의2.∼10의2." 같은
        가지번호 낀 범위는 확장하지 않는다(애매해서 스킵)."""
        old_texts = ["<P>제5조(정의) 6의2. ∼ 10의2. (생 략)</P>"]
        new_texts = ["<P>제5조(정의) 6의2. ∼ 10의2. (현행과 같음)</P>"]
        result = extract_admrul_unchanged(old_texts, new_texts, {"제5조"})
        assert result == {}
