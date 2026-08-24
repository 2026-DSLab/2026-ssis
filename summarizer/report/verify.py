"""HWPX 보고서 검증 — 생성된 문서를 다시 읽어 원본과 대조함.

왜 필요한가:
    HWPX 는 XML+ZIP 이라, 요약 텍스트를 넣을 때 특수문자(&, <, ①, ㆍ)나
    긴 문단에서 글자가 빠지거나 태그 조각이 새어들 수 있음. 요약 검증
    (요약이 원문과 맞나)과 별개로, '요약이 문서에 온전히 들어갔나'를
    확인함.

    방법은 요약 검증과 같음 — LLM 없이 코드 대조. 생성한 HWPX 를 다시 열어
    본문 텍스트를 뽑고, 보고서에 실었어야 할 원본 텍스트가 다 들어갔는지
    봄. 빠진 조각이 있으면 어느 것인지 보고함.
"""

from __future__ import annotations

import re
from pathlib import Path

from hwpx import HwpxDocument

from summarizer.models import ContractSummary


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _expected_fragments(contract: ContractSummary) -> list[str]:
    """보고서에 반드시 들어가야 할 텍스트 조각들.

    새 보고서는 overview(취지)와 조문 요약(표)을 싣음. 그 '내용'이 문서에
    온전히 들어갔는지 봄. 장식(─, ■ 기호)은 검사 대상이 아님.
    """
    frags: list[str] = []
    for law in contract.laws:
        frags.append(law.law_name)
        if law.headline:
            frags.append(law.headline)
        if law.overview:
            frags.append(law.overview)
        # caveats는 더 이상 문서에 쓰지 않음(builder.py의
        # law_detail() 참고) — 여기서도 "빠졌다"고 잘못 보고하지 않도록
        # 기대 목록에서 제외함.
        for s in law.article_summaries:
            if not s.error and (s.summary or "").strip():
                frags.append(s.summary)
    return frags


def verify_report(contract: ContractSummary, hwpx_path: str | Path) -> list[str]:
    """생성된 HWPX 를 원본과 대조함. 빠진 조각 목록을 돌려줌(없으면 빈 목록)."""
    doc = HwpxDocument.open(str(hwpx_path))
    extracted = _norm(doc.export_text())

    missing: list[str] = []
    for frag in _expected_fragments(contract):
        if _norm(frag) and _norm(frag) not in extracted:
            missing.append(frag)
    return missing
