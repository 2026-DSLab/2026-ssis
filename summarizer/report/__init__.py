"""HWPX 보고서 생성.

요약 결과(ContractSummary)를 HWPX 문서로 만든다. HWP 계열(HWPX 포함)
형식으로 제공하라는 계획서 4번 요구를 충족한다. HWPX 는 열린 표준(XML+ZIP)
이라 파이썬으로 생성·검증이 가능하고, 한글에서 그대로 열린다.

    builder.py   ContractSummary → HWPX 문서
    verify.py    생성된 HWPX 를 다시 읽어 원본 텍스트와 대조(누락/오염 검출)
"""

from summarizer.report.builder import build_report
from summarizer.report.verify import verify_report

__all__ = ["build_report", "verify_report"]
