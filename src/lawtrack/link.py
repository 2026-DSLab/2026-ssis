"""연쇄개정 그룹핑 — 같은 공포번호로 묶인 개정을 하나의 입법 이벤트로.

실측: 공포번호 하나(예: 35948)로 전자정부법 시행령·국민기초생활보장법
시행령·사회보장기본법 시행령·기초연금법 시행령 등 최소 5개+ 법령이
동시에 개정되었다. "2~3개 묶임" 이 아니라 대량 묶임을 전제로 설계한다.

이 매칭은 3단비교(thdCmp) API 보다 가볍고 정확하다 — 이미 change_log 에
쌓인 데이터로 조인 한 번이면 되고, thdCmp 처럼 별도 API 호출도, flat
list 안에서 국회규칙/대법원규칙까지 뒤섞인 구조를 다시 걸러내는 작업도
필요 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lawtrack.db.repo import ChangeLogRepo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkedGroup:
    """같은 공포번호로 묶인 개정 이벤트 그룹."""

    promulgation_no: str
    law_ids: tuple[str, ...]

    @property
    def is_chained(self) -> bool:
        """단독 개정이 아니라 여러 법이 함께 묶인 경우."""
        return len(self.law_ids) > 1


def group_by_promulgation(change_log_repo: ChangeLogRepo, promulgation_no: str) -> LinkedGroup:
    """이번에 감지된 개정과 같은 공포번호를 가진 다른 법을 조회.

    promulgation_no 가 빈 문자열이면(신구법 없음 등으로 공포번호를
    못 얻은 경우) 항상 단독 그룹을 반환한다.
    """
    if not promulgation_no:
        return LinkedGroup(promulgation_no="", law_ids=())

    rows = change_log_repo.find_by_promulgation(promulgation_no)
    law_ids = tuple(dict.fromkeys(r["law_id"] for r in rows))  # 순서 보존 중복 제거

    if len(law_ids) > 1:
        log.info(
            "연쇄개정 감지: 공포번호=%s, 함께 개정된 법 %d건: %s",
            promulgation_no, len(law_ids), law_ids,
        )
    return LinkedGroup(promulgation_no=promulgation_no, law_ids=law_ids)


def group_many(change_log_repo: ChangeLogRepo, promulgation_numbers: list[str]) -> dict[str, LinkedGroup]:
    """여러 공포번호를 한 번에 그룹핑. 중복 조회를 피하기 위한 편의 함수.

    빈 문자열은 애초에 그룹화 대상이 아니므로 제외한다.
    """
    result: dict[str, LinkedGroup] = {}
    for no in dict.fromkeys(n for n in promulgation_numbers if n):
        result[no] = group_by_promulgation(change_log_repo, no)
    return result