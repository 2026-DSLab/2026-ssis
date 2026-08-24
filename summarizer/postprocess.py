"""요약문 후처리 — LLM 이 낸 표면적 오류를 결정론적으로 바로잡음.

프롬프트로 "한자 쓰지 마"라고 부탁하는 것과 별개로, 출력을 코드로 한 번
더 훑음. LLM 은 확률적이라 프롬프트만으로는 100% 못 막음. 모델을
바꿔도 이 계층은 그대로 안전망으로 남음.
"""

from __future__ import annotations

import re

# 한중일 통합한자 영역.
_HANJA = re.compile(r"[一-鿿㐀-䶿]")

# 병기 한자: 한글 뒤 괄호 안의 한자 — 법조문에서 뜻을 분명히 하려는 정상
# 표기임. 예: "벌금형을 과(科)한다", "오륜(五輪)". 이건 건드리지 않음.
_PARENTHETICAL = re.compile(r"[가-힣]\s*[(（]\s*[一-鿿㐀-䶿]+\s*[)）]")

# LLM 이 한글 단어를 쓰다 첫 글자를 한자로 잘못 낸 흔한 경우.
# 실측(범죄피해자 요약): "具체적" → "구체적".
# 병기가 아닌 단독 한자만 대상이며, 애매하면 치환하지 않고 경고로 남김.
_COMMON_FIX = {
    "具체적": "구체적",
    "科학": "과학",
    "課": "과",
    "對": "대",
    "關": "관",
    "및": "및",  # noop 방지용 자리표시 — 실제 항목은 실측으로 늘린다
}


def _mask_parentheticals(text: str) -> tuple[str, list[str]]:
    """병기 한자를 임시 치환해 보호함. (보호된 텍스트, 원본조각들)"""
    saved: list[str] = []

    def _repl(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    return _PARENTHETICAL.sub(_repl, text), saved


def _unmask(text: str, saved: list[str]) -> str:
    for i, frag in enumerate(saved):
        text = text.replace(f"\x00{i}\x00", frag)
    return text


def fix_text(text: str) -> tuple[str, list[str]]:
    """한 문자열을 후처리함. (고친 텍스트, 남은 경고들) 반환.

    - 병기 한자(과(科) 등)는 보존.
    - 흔한 한자 오타는 한글로 치환.
    - 그래도 남은 단독 한자는 경고로 보고(치환하지 않음 — 잘못 바꾸느니
      사람이 확인하게 둠).
    """
    if not text:
        return text, []

    masked, saved = _mask_parentheticals(text)

    for wrong, right in _COMMON_FIX.items():
        if wrong == right:
            continue
        masked = masked.replace(wrong, right)

    warnings: list[str] = []
    for m in _HANJA.finditer(masked):
        i = m.start()
        ctx = masked[max(0, i - 8) : i + 8].replace("\x00", "")
        warnings.append(f"한자 '{m.group()}' 남음: ...{ctx}...")

    return _unmask(masked, saved), warnings


def clean_summary(headline: str, body: str) -> tuple[str, str, list[str]]:
    """headline/body 를 후처리하고 남은 경고를 모음."""
    hl, w1 = fix_text(headline)
    bd, w2 = fix_text(body)
    return hl, bd, w1 + w2
