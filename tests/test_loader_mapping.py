"""apply_mappings()가 매핑의 "이동"(내용 그대로 번호만 이동) 주장을
맹신하지 않고 코드로 재검증하는지 확인한다.

★★ 실측(2026-07-30, 전자정부법 제56조의2 구②→신⑤): MappingAgent(LLM)가
이 위치를 "이동"이라 판정했는데, 그 판정의 note 자체에는 "재구성됨"이라고
써서 자기모순이었다(실제로 문장이 확장됨 — "제1항에 따른"→"제1항부터
제4항까지에 따른"). apply_mappings()가 relation=="이동"이면 basis를
따지지 않고 무조건 move_is_identical=True 를 줘버려서, ArticleAgent가
LLM 요약도 verifier 검증도 없이 "내용 변경 없이 번호만 이동했습니다"라는
사실과 다른 문장을 그대로 냈다.

matching.py의 resolve_article()은 이미 이 정확한 케이스를 실측 근거로
남겨뒀다 — 완전일치(코드 계산)로는 못 풀리고(문장이 확장돼 있어서),
그래서 LLM 판정으로 넘어갔다는 것 자체가 "애매함"의 신호다. apply_mappings()
가 그 LLM 판정을 곧이곧대로 믿지 않고 content_ratio 로 한 번 더 확인해야
한다.
"""

from __future__ import annotations

from summarizer.loader import apply_mappings
from summarizer.models import ArticleMapping, ArticleUnit, PositionMapping


def _unit(location_label: str, old_text: str = "", new_text: str = "") -> ArticleUnit:
    return ArticleUnit(
        law_id="009199", law_name="전자정부법",
        location_label=location_label, change_type="개정",
        old_text=old_text, new_text=new_text, match_status="위치재배치의심",
    )


def test_llm_claimed_move_with_reworded_content_becomes_moved_and_amended():
    """LLM이 "이동"이라 주장해도, 실제 문장이 재구성됐으면(유사도 낮음)
    이동후개정으로 강등해 diff/요약 경로를 그대로 타게 해야 한다."""
    units = [
        _unit("제56조의2②", old_text="", new_text="② 새로 생긴 다른 내용"),
        _unit(
            "제56조의2⑤", old_text="",
            new_text="⑤ 제1항부터 제4항까지에 따른 정보시스템 장애관리계획의 수립ㆍ시행 및 "
                     "그 밖에 정보시스템의 장애 예방ㆍ대응 및 복구 등에 필요한 사항은 국회규칙, "
                     "대법원규칙, 헌법재판소규칙, 중앙선거관리위원회규칙 및 대통령령으로 정한다.",
        ),
    ]
    # 구②의 실제 old_text(밀려나기 전 원문) — moved_from 조회 대상.
    units[0] = ArticleUnit(**{**units[0].__dict__, "old_text":
        "② 제1항에 따른 정보시스템의 장애 예방 및 대응에 필요한 사항은 국회규칙, "
        "대법원규칙, 헌법재판소규칙, 중앙선거관리위원회규칙 및 대통령령으로 정한다."})

    mappings = [
        ArticleMapping("제56조의2", [
            PositionMapping("②", "⑤", "이동", "LLM판정",
                             note="장애 예방 및 대응에 필요한 사항이 재구성됨"),
        ]),
    ]

    result = apply_mappings(units, mappings)
    moved = next(u for u in result if u.location_label == "제56조의2⑤")

    assert moved.change_type == "이동후개정"
    assert moved.move_is_identical is False
    assert moved.diff_parts  # 실제 대조로 diff가 채워져야 요약/검증이 정상 작동한다


def test_genuinely_identical_move_still_trusted():
    """진짜로 번호만 밀리고 내용이 글자까지 같으면 여전히 규칙 기반으로
    처리해야 한다 — 이번 수정이 정상적인 이동까지 LLM으로 밀어내면 안 된다.

    ★★★ 실측 발견(2026-07-31, 공공기관 데이터베이스 표준화 지침 제16조⑤):
    "이동" 분기가 content_ratio 로 moved_old 를 찾아 유사도만 확인하고,
    정작 unit.old_text 를 그 moved_old 로 바꿔치기하는 걸 빠뜨렸다.
    그 결과 unit.old_text 는 여전히 "이 새 위치와 같은 번호였던 옛
    위치"의 엉뚱한 문장이 남아 있었다 — "내용 변경 없이 이동했다"는
    요약(사실은 맞음)과 "원문 보기"에 뜨는 개정 전 문장(실제로 비교한
    문장이 아님)이 서로 모순돼 보이는 버그였다."""
    units = [
        ArticleUnit(
            law_id="27947", law_name="(계약예규) 물품구매(제조)계약일반조건",
            location_label="제26조①7.", change_type="신설",
            old_text="7. 기타 계약조건을 위반하고 그 위반으로 인하여 계약의 목적을 "
                     "달성할 수 없다고 인정될 경우",
            new_text="", match_status="위치재배치의심",
        ),
        _unit(
            "제26조①8.",
            new_text="8. 기타 계약조건을 위반하고 그 위반으로 인하여 계약의 목적을 "
                     "달성할 수 없다고 인정될 경우",
        ),
    ]
    mappings = [
        ArticleMapping("제26조", [
            PositionMapping("①7.", "①8.", "이동", "LLM판정"),
        ]),
    ]

    result = apply_mappings(units, mappings)
    moved = next(u for u in result if u.location_label == "제26조①8.")

    assert moved.change_type == "이동"
    assert moved.move_is_identical is True
    assert moved.old_text == (
        "7. 기타 계약조건을 위반하고 그 위반으로 인하여 계약의 목적을 달성할 수 없다고 인정될 경우"
    )
