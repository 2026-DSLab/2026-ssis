"""법령 개정 요약 멀티 에이전트 파이프라인.

2026-ssis 의 out/*.json (LLM팀 전달용 계약 산출물)을 입력으로 받아
법령 단위 요약을 만든다.

    single_*.json  — 법령/행정규칙 1건의 개정 내용
    weekly_*.json  — 그 주에 바뀐 것 전부

두 파일은 같은 WeeklyContract 스키마를 쓰므로 파이프라인은 동일하다.
(single 은 amendment_groups 가 1개짜리인 weekly 일 뿐이다.)

구성:
    config.py    설정 — API 키, 모델, 동시성
    loader.py    계약 JSON 로드 + 조문 단위 정규화 (결정론적, LLM 미개입)
    llm.py       LLM 호출 경계 — 실제 Anthropic 호출부
    prompts/     에이전트별 프롬프트
    agents/      1단계(조문) / 2단계(법령) 에이전트
    pipeline.py  오케스트레이션
    sinks.py     결과 저장

실행:
    ANTHROPIC_API_KEY 를 .env 에 넣고
    python -m summarizer out/single_*.json
"""

from __future__ import annotations

import sys
from pathlib import Path

# lawtrack.contract.schema (팀 간 '계약' 정의)를 재사용하기 위한 경로 등록.
# summarizer 는 2026-ssis 최상위에 있고 lawtrack 은 src/ 아래에 있어서,
# 이 한 줄이 없으면 `python -m summarizer` 가 lawtrack 을 못 찾는다.
# 계약 스키마를 여기에 복제하지 않으려는 것 — 복제하면 반드시 드리프트한다.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

__all__ = ["__version__"]
__version__ = "0.1.0"
