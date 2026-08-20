"""어절 단위 diff 와 사전 선별 테스트.

선별의 안전 원칙을 테스트로 못 박는다:
    실질 변경이 FORMAL 로 새어나가면 보고서에서 누락된다. 이게 최악이다.

★ 출처: seongbeen2 브랜치(tests/test_mas_triage.py)에서 이식(2026-07-30).
  summarizer.triage / summarizer.textdiff 로 import 경로만 바꿨다.
  triage_many(dict 기반 일괄처리)는 hyunki 쪽 ArticleUnit 기반 파이프라인에서
  쓰이지 않아 이식하지 않았다 — 관련 테스트(TestTriageMany)도 제외했다.
"""

import pytest

from summarizer.textdiff import changed_words, diff_segments, similarity, tokenize
from summarizer.triage import ChangeClass, triage


class TestTokenize:
    def test_공백을_보존한다(self):
        assert "".join(tokenize("가 나  다")) == "가 나  다"

    def test_빈문자열(self):
        assert tokenize("") == []
        assert tokenize(None) == []


class TestDiffSegments:
    def test_원문이_그대로_복원된다(self):
        old = "① 기초급여액은 통계청장이 고시한다."
        new = "① 기초급여액은 국가데이터처장이 고시한다."
        left, right = diff_segments(old, new)
        assert "".join(s.text for s in left) == old
        assert "".join(s.text for s in right) == new

    def test_어절이_조각나지_않는다(self):
        """글자 단위였다면 '통계청'->'국가데' 처럼 쪼개졌다."""
        dels, inss = changed_words("통계청장이 고시한다", "국가데이터처장이 고시한다")
        assert dels == ["통계청장이"]
        assert inss == ["국가데이터처장이"]

    def test_같은_종류_세그먼트는_병합된다(self):
        left, _ = diff_segments("가 나 다 라", "가 라")
        assert [s.kind for s in left] == ["eq", "del", "eq"]

    def test_완전히_같으면_변경어절이_없다(self):
        assert changed_words("동일한 문장", "동일한 문장") == ([], [])


class TestSimilarity:
    def test_동일하면_1(self):
        assert similarity("가 나 다", "가 나 다") == 1.0

    def test_둘다_비면_1(self):
        assert similarity("", "") == 1.0

    def test_공백차이는_무시된다(self):
        assert similarity("가 나", "가  나") == 1.0


class TestTriageSafety:
    """실질 변경이 FORMAL 로 분류되면 안 된다."""

    def test_내용이_바뀌면_실질변경(self):
        r = triage(
            "① 신청은 30일 이내에 하여야 한다.",
            "① 신청은 60일 이내에 하여야 하며, 부득이한 경우 연장할 수 있다.",
        )
        assert r.change_class is ChangeClass.SUBSTANTIVE

    def test_기간_숫자만_바뀌어도_FORMAL_이_아니다(self):
        """'30일 -> 60일' 은 어절 하나지만 실무 영향이 크다."""
        r = triage("신청은 30일 이내에 하여야 한다", "신청은 60일 이내에 하여야 한다")
        assert r.change_class is not ChangeClass.FORMAL

    def test_신설은_형식정비가_될_수_없다(self):
        r = triage("", "새 조문", change_type="신설")
        assert r.change_class is ChangeClass.SUBSTANTIVE

    def test_삭제는_형식정비가_될_수_없다(self):
        r = triage("있던 조문", "", change_type="삭제")
        assert r.change_class is ChangeClass.SUBSTANTIVE

    def test_한쪽이_비면_보수적으로_실질변경(self):
        assert triage("본문", "").change_class is ChangeClass.SUBSTANTIVE
        assert triage("", "본문").change_class is ChangeClass.SUBSTANTIVE

    @pytest.mark.parametrize("old,new", [
        ("금액은 100만원으로 한다", "금액은 200만원으로 한다"),
        ("위원은 5명 이내로 한다", "위원은 9명 이내로 한다"),
        ("승인을 받아야 한다", "신고를 하여야 한다"),
    ])
    def test_실무영향_있는_변경은_FORMAL_이_아니다(self, old, new):
        assert triage(old, new).change_class is not ChangeClass.FORMAL

    @pytest.mark.parametrize("old,new,why", [
        ("금액은 100만원으로 한다", "금액은 200만원으로 한다", "'원'이 기관 접미사로 오인됨"),
        ("전국 단위로 시행한다", "지역 단위로 시행한다", "'국'이 기관 접미사로 오인됨"),
        ("그 결과를 통보한다", "그 성과를 통보한다", "'과'가 기관 접미사로 오인됨"),
        ("사실을 확인한다", "현실을 확인한다", "'실'이 기관 접미사로 오인됨"),
        ("일부를 지원한다", "전부를 지원한다", "'부'가 기관 접미사로 오인됨"),
    ])
    def test_기관명_오탐_회귀(self, old, new, why):
        """한 글자 접미사가 일반 명사를 기관명으로 오인하면 실질 변경이 누락된다."""
        assert triage(old, new).change_class is not ChangeClass.FORMAL, why

    def test_숫자가_섞인_어절은_기관명이_아니다(self):
        r = triage("제3부장관은 승인한다", "제5부장관은 승인한다")
        assert r.change_class is not ChangeClass.FORMAL


class TestTriageFormal:
    """확실한 조직 개편 정비만 걸러낸다."""

    def test_기관명_변경은_형식정비(self):
        r = triage(
            "환경부장관은 매년 기본계획을 수립하여야 한다. 이 경우 관계 중앙행정기관의 장과 협의한다.",
            "기후에너지환경부장관은 매년 기본계획을 수립하여야 한다. 이 경우 관계 중앙행정기관의 장과 협의한다.",
        )
        assert r.change_class is ChangeClass.FORMAL
        assert not r.needs_llm

    def test_직위명_변경도_형식정비(self):
        r = triage(
            "「통계법」 제3조에 따라 통계청장이 매년 고시하는 전국소비자물가변동률을 반영하여 매년 고시한다.",
            "「통계법」 제3조에 따라 국가데이터처장이 매년 고시하는 전국소비자물가변동률을 반영하여 매년 고시한다.",
        )
        assert r.change_class is ChangeClass.FORMAL

    def test_기관명이_아닌_어절이_섞이면_FORMAL_이_아니다(self):
        r = triage(
            "환경부장관은 30일 이내에 승인한다",
            "기후에너지환경부장관은 60일 이내에 승인한다",
        )
        assert r.change_class is not ChangeClass.FORMAL

    def test_변경어절이_많으면_FORMAL_이_아니다(self):
        r = triage(
            "가부 나부 다부 라부 마부 를 둔다",
            "가처 나처 다처 라처 마처 를 둔다",
        )
        assert r.change_class is not ChangeClass.FORMAL


class TestTriageNoChange:
    def test_전후_동일하면_변경없음(self):
        text = "1. 주요재료비     계약목적물의 기본적 구성형태를 이루는 물품의 가치"
        r = triage(text, text)
        assert r.change_class is ChangeClass.NO_CHANGE
        assert not r.needs_llm

    def test_공백만_다르면_변경없음(self):
        r = triage("가 나 다", "가  나   다")
        assert r.change_class is ChangeClass.NO_CHANGE


class TestOrgCategoryRegression:
    """실데이터에서 잡힌 오탐 회귀 방지."""

    def test_권한이관은_형식정비가_아니다(self):
        """'심의위원회 -> 보건복지부장관' 은 개편이 아니라 권한 이관이다."""
        r = triage(
            "심의위원회가 그 적정성을 심의하여 결정한다. 이 경우 관계 서류를 첨부하여야 한다.",
            "보건복지부장관이 그 적정성을 심의하여 결정한다. 이 경우 관계 서류를 첨부하여야 한다.",
        )
        assert r.change_class is not ChangeClass.FORMAL

    def test_같은_범주_명칭변경은_형식정비(self):
        """직위 -> 직위, 기관 -> 기관 은 통과해야 한다."""
        pairs = [
            ("통계청장이 매년 고시하는 물가변동률을 반영하여 산정한다.",
             "국가데이터처장이 매년 고시하는 물가변동률을 반영하여 산정한다."),
            ("기획재정부장관은 예산을 배정하고 그 결과를 통보하여야 한다.",
             "재정경제부장관은 예산을 배정하고 그 결과를 통보하여야 한다."),
        ]
        for old, new in pairs:
            assert triage(old, new).change_class is ChangeClass.FORMAL, (old, new)

    def test_대응개수가_다르면_형식정비가_아니다(self):
        r = triage(
            "환경부장관과 국토교통부장관은 협의하여 이를 정하고 공고한다.",
            "기후에너지환경부장관은 협의하여 이를 정하고 공고한다.",
        )
        assert r.change_class is not ChangeClass.FORMAL
