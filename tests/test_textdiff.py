"""summarizer/textdiff.py — 어절 단위 diff. 리스트(콤마/및/또는) 항목
재배치 케이스는 실측 기반(2026-07-31, 공공기관의 정보공개에 관한 법률
시행령 제20조②)."""

from __future__ import annotations

from summarizer.textdiff import Segment, diff_segments, tokenize


def _reconstruct(segments: list[Segment]) -> str:
    return "".join(s.text for s in segments)


class TestDiffSegmentsReconstruction:
    """모든 케이스에서 왼쪽/오른쪽 세그먼트를 이으면 원문이 그대로
    복원돼야 한다 — 강조는 표시만 바꾸는 것이지 원문을 훼손하면 안 된다."""

    def test_identical_text_is_all_equal(self):
        text = "① 이 규칙은 공포한 날부터 시행한다."
        left, right = diff_segments(text, text)
        assert _reconstruct(left) == text
        assert _reconstruct(right) == text
        assert all(s.kind == "eq" for s in left)
        assert all(s.kind == "eq" for s in right)

    def test_pure_insert(self):
        old, new = "", "새로 추가된 문장입니다."
        left, right = diff_segments(old, new)
        assert _reconstruct(left) == old
        assert _reconstruct(right) == new
        assert all(s.kind == "ins" for s in right)

    def test_pure_delete(self):
        old, new = "삭제될 문장입니다.", ""
        left, right = diff_segments(old, new)
        assert _reconstruct(left) == old
        assert _reconstruct(right) == new
        assert all(s.kind == "del" for s in left)

    def test_simple_word_substitution_reconstructs(self):
        old = "대한올림픽위원회는 매년 총회를 연다."
        new = "대한체육회는 매년 총회를 연다."
        left, right = diff_segments(old, new)
        assert _reconstruct(left) == old
        assert _reconstruct(right) == new

    def test_list_reordering_reconstructs(self):
        old = (
            "② 법 제23조제2항제1호에 따른 위원은 기획재정부 제2차관, 법무부 차관, "
            "행정안전부 차관 및 국무조정실 국무1차장으로 한다."
        )
        new = (
            "② 법 제23조제2항제1호에 따른 위원은 법무부 차관, 행정안전부 차관, "
            "기획예산처 차관 및 국무조정실 국무1차장으로 한다."
        )
        left, right = diff_segments(old, new)
        assert _reconstruct(left) == old
        assert _reconstruct(right) == new


class TestSimpleSubstitutionUnaffected:
    """목록(콤마/및/또는)이 없는 평범한 단어 치환은 기존과 동일하게
    최소 범위만 강조돼야 한다 — 회귀 방지."""

    def test_single_word_change_highlights_only_that_word(self):
        old = "대한올림픽위원회는 매년 총회를 연다."
        new = "대한체육회는 매년 총회를 연다."
        left, right = diff_segments(old, new)

        # 어절(공백) 단위 토큰화라 "는"까지 붙은 통짜 어절이 강조 단위다.
        del_texts = [s.text for s in left if s.kind == "del"]
        ins_texts = [s.text for s in right if s.kind == "ins"]
        assert del_texts == ["대한올림픽위원회는"]
        assert ins_texts == ["대한체육회는"]


class TestListItemReorderingPrecision:
    """★★ 실측(2026-07-31, 공공기관의 정보공개에 관한 법률 시행령
    제20조②): 목록에서 항목 하나가 빠지고 다른 항목이 들어오면, 신구
    양쪽에 반복되는 단어("차관")가 있어도 실제로 바뀐 항목만 정확히
    강조해야 한다 — 안 바뀐 "법무부 차관"/"행정안전부 차관"까지 강조
    범위에 딸려 들어가면 안 된다."""

    OLD = (
        "② 법 제23조제2항제1호에 따른 위원은 기획재정부 제2차관, 법무부 차관, "
        "행정안전부 차관 및 국무조정실 국무1차장으로 한다."
    )
    NEW = (
        "② 법 제23조제2항제1호에 따른 위원은 법무부 차관, 행정안전부 차관, "
        "기획예산처 차관 및 국무조정실 국무1차장으로 한다."
    )

    def test_old_side_marks_only_removed_item(self):
        left, _ = diff_segments(self.OLD, self.NEW)
        del_texts = [s.text for s in left if s.kind == "del"]
        assert del_texts == ["기획재정부 제2차관, "]

    def test_new_side_marks_only_added_item_not_unchanged_neighbors(self):
        _, right = diff_segments(self.OLD, self.NEW)
        ins_texts = [s.text for s in right if s.kind == "ins"]
        assert ins_texts == ["기획예산처 차관 및 "]
        # "법무부 차관"/"행정안전부 차관"은 안 바뀌었으므로 삽입으로
        # 잡히면 안 된다(예전 버그: " 차관, 기획예산처"로 범위가 밀림).
        assert not any("법무부" in t for t in ins_texts)
        assert not any("행정안전부" in t for t in ins_texts)

    def test_trailing_common_tail_stays_equal(self):
        left, right = diff_segments(self.OLD, self.NEW)
        assert left[-1].kind == "eq"
        assert right[-1].kind == "eq"
        assert left[-1].text.endswith("국무조정실 국무1차장으로 한다.")
        assert right[-1].text.endswith("국무조정실 국무1차장으로 한다.")


class TestTokenizeRoundTrip:
    def test_tokenize_reconstructs_original(self):
        text = "이 문장은 여러 어절과 공백  으로 이루어져 있다."
        assert "".join(tokenize(text)) == text
