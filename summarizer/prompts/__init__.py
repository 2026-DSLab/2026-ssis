"""에이전트별 프롬프트.

프롬프트를 에이전트 코드에서 분리한 이유: 프롬프트는 가장 자주, 가장
작게 고치는 부분임. 배선 코드와 섞여 있으면 수정할 때마다 로직을
건드릴 위험이 생기고, diff 를 읽기도 어려움.
"""

from summarizer.prompts.article import build_article_prompt
from summarizer.prompts.law import LAW_SUMMARY_SCHEMA, build_law_prompt
from summarizer.prompts.mapping import MAPPING_SCHEMA, build_mapping_prompt

__all__ = [
    "build_article_prompt",
    "build_law_prompt",
    "build_mapping_prompt",
    "LAW_SUMMARY_SCHEMA",
    "MAPPING_SCHEMA",
]
