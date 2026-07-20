"""조문 문장 분해 (가드 ④).

문제 (실측):
    oldAndNew  : "① 텍스트 ② 텍스트 ③ 텍스트"   ← 한 덩어리 문자열
    lawService : <항번호>①</항번호><항내용>텍스트</항내용>
                 <항번호>②</항번호><항내용>텍스트</항내용>   ← 태그로 분리

    => oldAndNew 에서 통짜로 가져온 문장을 그대로 lawService 전문에서
       검색하면 절대 안 나온다.

확인된 발생 법령:
    - 전자정부법   : 한 번에 입력 시 실패. ①② 로 나눠 검색해야 함
    - 도로교통법   : oldAndNew 한 줄 vs lawService 태그 분리
    - 별정우체국법 : 항ㆍ호ㆍ목 기호 기준 정규식 분해 필요
    - 실종아동법   : <P> 문장 전체로 검색 시 실패
                     (조문 내용이 한 문장에 붙어있는 경우 발생)

해결:
    항(①②③) / 호(1. 2.) / 목(가. 나.) 기호를 경계로 문장을 조각내고,
    조각별로 검색한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Level(str, Enum):
    """조문 계층."""

    CLAUSE = "항"      # ① ② ③
    ITEM = "호"        # 1. 2. 3.  /  1의2.
    SUBITEM = "목"     # 가. 나. 다.
    NONE = "없음"      # 기호 없는 평문


# ---------------------------------------------------------------------------
# 기호 정의
# ---------------------------------------------------------------------------

#: 항 기호 ①(U+2460) ~ ⑳(U+2473). 법령은 보통 ⑮ 이내이나 여유를 둔다.
CIRCLED = "".join(chr(c) for c in range(0x2460, 0x2474))

#: 호: "1." "12." "1의2." — 줄 시작 또는 공백 뒤에서만 인식한다.
#: 주의: "제27조" 의 "27" 같은 조문 참조를 호로 오인하면 안 되므로
#:       뒤에 반드시 마침표가 오는 경우만 잡는다.
_ITEM_PAT = r"\d+(?:의\d+)?\."

#: 목: "가." "나." … "카." (한글 자모 순)
_SUBITEM_CHARS = "가나다라마바사아자차카타파하"
_SUBITEM_PAT = rf"[{_SUBITEM_CHARS}](?:의\d+)?\."

_CLAUSE_RE = re.compile(rf"(?=[{CIRCLED}])")
#: 실측 발견: oldAndNew 블록에서 문장이 "...하여야 한다.1. 정보시스템..." 처럼
#: 마침표 바로 뒤에 공백 없이 호 번호가 붙는 경우가 있다(전자정부법 제56조의2
#: 실측 확인). 반면 아래 두 경우는 호 경계로 오인하면 안 된다:
#:   - "2.1% 이상" 같은 소수점 표기 (직전 2글자가 "숫자+마침표")
#:   - "12의2." 같은 가지번호 표기의 뒷부분 (직전 2글자가 "숫자+의")
#: ★ 실측 확인(2026-07-16): (?<!\d) 단독 차단을 없애봤더니 "12." 같은
#: 두 자리 이상 숫자 자체가 "1" + "2." 로 쪼개지는 회귀가 생겼다(아동복지법
#: "12의2." 실측 케이스, split_all 목 분해 케이스 등 다수 실패). 즉 이
#: 1글자 lookbehind는 다자리 숫자 보호에 필수라서 되돌린다. "…100분의
#: 502. 물품의 제조…"처럼 앞 호의 숫자 내용과 다음 호 번호가 공백 없이
#: 바로 붙는 표(table) 형식 조문은 일반 규칙으로 안전하게 구분할 수
#: 없는 진짜 애매한 case로 보고, admrul 쪽 각주 제거(strip_annotations)
#: 만으로 해결 가능한 부분만 고친다.
#: ★★ 실측 발견(2026-07-16, 조달청 협상에 의한 계약 제안서평가 세부기준
#: 제9조①): "…「지방자치단체 입찰시 낙찰자 결정기준」 제7장 제3절의
#: 4.(제안서의 평가)에 따른…" 처럼 다른 문서의 장/절/관/편 하위번호를
#: 가리키는 참조 표현("N절의 4.")이 이 조문 자체의 호 번호로 오인되어,
#: 바뀐 단어 하나("기획재정부"→"재정경제부")뿐인 안 쪼개져도 될 문장이
#: 둘로 쪼개지고 old_text가 양쪽에 중복 삽입되는 결과를 냈다. "장/절/관/편"
#: 은 법령 문서구조 표준 단위(편>장>절>관>조)라 "○의" 바로 뒤에 숫자+마침표가
#: 오면 거의 항상 이런 외부참조이지 이 조문의 실제 호 목록 시작이 아니다.
_SECTION_UNITS = "장절관편"
_SECTION_REF_GUARD = "".join(
    f"(?<!{w}의)(?<!{w}의 )" for w in _SECTION_UNITS
)
_ITEM_RE = re.compile(
    rf"(?:(?<=^)|(?:(?<!\d)(?<!\d\.)(?<!\d의){_SECTION_REF_GUARD}))(?={_ITEM_PAT})"
)
#: 실측 발견(전자정부법 제56조의2 항①): 목 기호 "가나다라마바사아자차카타파하"는
#: 한글 문장 종결형("포함한다.", "같다." 등)의 마지막 글자와 우연히 겹친다.
#: 반대로 실측(전자정부법 제2조제11호): 실제 목 마커도 앞 목의 내용에
#: 공백 없이 바로 붙는 경우가 있다("…행정정보나. 정보시스템다. …", 즉
#: "나." 바로 앞이 한글 음절 "보"). 그래서 앞 글자가 한글이냐 아니냐로는
#: 참/거짓을 구분할 수 없다 — 대신 뒤쪽에서 실제로 "가나다라…" 순서로
#: 증가하는 마커 나열이 만들어지는지(_valid_subitem_run)로 판정한다.
#: 이 정규식 자체는 후보 위치를 전부 찾기만 하고, 필터링은 split_by_subitem
#: 에서 한다.
_SUBITEM_RE = re.compile(rf"(?={_SUBITEM_PAT})")

_CLAUSE_HEAD_RE = re.compile(rf"^([{CIRCLED}])")
_ITEM_HEAD_RE = re.compile(rf"^({_ITEM_PAT})")
_SUBITEM_HEAD_RE = re.compile(rf"^({_SUBITEM_PAT})")

#: 조문 제목 "제6조의2(기준 중위소득의 산정)" 형태
_ARTICLE_HEAD_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*(?:\(([^)]*)\))?")

#: 변경 없음 표시. 이 조각들은 검색 대상이 아니다.
_SKIP_MARKERS = ("(생 략)", "(생략)", "(현행과 같음)", "(현행과같음)")


@dataclass(frozen=True)
class Fragment:
    """분해된 조각 하나."""

    level: Level
    marker: str | None   # "①", "12의2.", "가." / 없으면 None
    text: str            # 기호를 제외한 본문
    raw: str             # 기호 포함 원문

    @property
    def is_skippable(self) -> bool:
        """(생 략) / (현행과 같음) 처럼 내용이 없는 조각."""
        compact = self.text.replace(" ", "")
        return any(m.replace(" ", "") in compact for m in _SKIP_MARKERS)

    @property
    def searchable(self) -> bool:
        """검색 대상으로 쓸 만한 조각인가."""
        return bool(self.text.strip()) and not self.is_skippable


# ---------------------------------------------------------------------------
# 분해
# ---------------------------------------------------------------------------

def split_by_clause(text: str) -> list[Fragment]:
    """항 기호(①②③) 기준 분해."""
    return _split(text, _CLAUSE_RE, _CLAUSE_HEAD_RE, Level.CLAUSE)


def split_by_item(text: str) -> list[Fragment]:
    """호 기호(1. 2.) 기준 분해.

    ★★ 실측 발견(2026-07-18, 정보보호 및 개인정보보호 관리체계 인증 등에
    관한 고시 제23조③2.): "별표 7의2 가목(1.1.2. 항목 제외) 및 나목"처럼
    괄호 안에 다른 문서(별표)의 세부 항목 번호를 가리키는 참조 표현이
    있으면, 그 안의 "1."이 진짜 호 경계로 오인되어 호2가 괄호 중간에서
    잘리고, 괄호 안 내용이 엉뚱하게 별도의 가짜 "호 1."로 떨어져 나갔다.
    괄호 밖 경계 탐지는 그대로 두고, 괄호 안 숫자만 탐지에서 안 보이게
    가려서(mask_parens) 이 오인을 막는다."""
    return _split(text, _ITEM_RE, _ITEM_HEAD_RE, Level.ITEM, mask_parens=True)


def _subitem_letter_index(marker: str) -> int:
    """'가.' '가의2.' 같은 마커에서 자모 순서상 위치를 구한다. 못 찾으면 -1."""
    base = marker.rstrip(".")
    if "의" in base:
        base = base.split("의", 1)[0]
    return _SUBITEM_CHARS.find(base[0]) if base else -1


def split_by_subitem(text: str) -> list[Fragment]:
    """목 기호(가. 나.) 기준 분해.

    실측(전자정부법 제2조제11호): 목 마커가 앞 목의 내용 끝에 공백 없이
    바로 붙기도 한다("…행정정보나. 정보시스템다. …"). 반면 한글 문장
    종결형("포함한다.")도 우연히 목 마커와 같은 글자를 갖는다. 둘 다
    "바로 앞이 한글이냐"로는 구분이 안 되므로, 대신 뒤쪽에서 실제로
    "가→나→다→…" 순서로 엄격히 증가하는 마커 나열이 나오는지 검증한다.
    그런 나열이 없으면(예: 우연히 하나 걸린 "다.") 분해하지 않는다.
    가장 마지막 "가."를 목록의 시작으로 보고, 그 앞은 전부 본문(전제문)
    으로 합쳐 하나의 조각으로 되돌린다.
    """
    raw = _split(text, _SUBITEM_RE, _SUBITEM_HEAD_RE, Level.SUBITEM)

    start = None
    for i, frag in enumerate(raw):
        if frag.marker and _subitem_letter_index(frag.marker) == 0:  # '가'
            start = i  # 마지막 '가.' 위치를 목록 시작으로 삼는다

    if start is None:
        return []

    tail = raw[start:]
    if len(tail) < 2:
        return []  # "가." 하나만 발견 — 진짜 목록이라기엔 근거 부족
    indices = [_subitem_letter_index(f.marker) for f in tail]
    if any(idx == -1 for idx in indices) or not all(b > a for a, b in zip(indices, indices[1:])):
        return []

    head = raw[:start]
    if not head:
        return tail

    preamble_raw = " ".join(f.raw for f in head).strip()
    return [Fragment(Level.NONE, None, preamble_raw, preamble_raw), *tail]


#: 괄호 안(중첩 없음 가정) 내용을 찾는 패턴 — mask_parens 용.
_PAREN_RE = re.compile(r"\([^()]*\)")


def _mask_parens_digits(text: str) -> str:
    """괄호 안의 숫자만 자릿수를 유지한 채 '#'로 가린 텍스트를 만든다.

    반환값은 오직 "경계가 어디인지" 찾는 용도로만 쓰고, 실제 조각의
    내용(Fragment.text/raw)은 항상 원본 text에서 그대로 잘라 쓴다 —
    길이를 그대로 유지해야(숫자 1글자 → '#' 1글자) 원본 text와 위치가
    1:1로 대응해 슬라이싱이 어긋나지 않는다.
    """
    return _PAREN_RE.sub(lambda m: re.sub(r"\d", "#", m.group(0)), text)


def _split(
    text: str, split_re, head_re, level: Level, *, mask_parens: bool = False,
) -> list[Fragment]:
    if not text or not text.strip():
        return []

    if mask_parens:
        # ★ 괄호 안 숫자 참조가 경계로 오인되지 않도록, 경계 탐지는
        # 마스킹된 텍스트로 하되 실제 자르기는 원본 text에서 한다
        # (마스킹은 길이를 보존하므로 위치가 그대로 대응된다).
        scan_text = _mask_parens_digits(text)
        positions = sorted({m.start() for m in split_re.finditer(scan_text)})
        if not positions or positions[0] != 0:
            positions = [0, *positions]
        parts = []
        for i, start in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(text)
            parts.append(text[start:end])
        parts = [p for p in parts if p.strip()]
    else:
        parts = [p for p in split_re.split(text) if p.strip()]

    out: list[Fragment] = []
    for part in parts:
        stripped = part.strip()
        m = head_re.match(stripped)
        if m:
            marker = m.group(1)
            body = stripped[m.end():].strip()
            out.append(Fragment(level, marker, body, stripped))
        else:
            out.append(Fragment(Level.NONE, None, stripped, stripped))
    return out


def split_all(text: str) -> list[Fragment]:
    """항 → 호 → 목 순으로 계층 분해하여 최소 단위 조각들을 반환.

    검색 시에는 가장 작은 단위부터 시도하는 것이 매칭 확률이 높다.
    단, 조각이 너무 짧아지면 중복 매칭이 늘어나므로 가드 ⑤(번호 결합)로
    보완해야 한다.
    """
    if not text or not text.strip():
        return []

    result: list[Fragment] = []
    for clause in split_by_clause(text) or [Fragment(Level.NONE, None, text, text)]:
        items = split_by_item(clause.text)
        # 실측(전자정부법 제2조제11호): 블록 안에 호가 정확히 1개뿐이면
        # "분해할 필요 없음"으로 보고 예전엔 원문(clause)을 그대로 되돌렸는데,
        # 이러면 그 하나뿐인 호에 딸린 목 분해(아래 for문)가 통째로
        # 건너뛰어진다. 호 마커가 실제로 발견됐다면(items 가 비어있지
        # 않다면) 개수가 1개여도 목 분해를 계속 시도해야 한다.
        #
        # ★★ 실측 발견(2026-07-19, 환경개선비용 부담법 제20조① 등): 호
        # 마커가 전혀 없는 조각이라도 split_by_item()의 내부 _split()은
        # "매칭 안 된 나머지"를 marker=None 인 Fragment 하나로 감싸
        # 돌려주므로 items가 절대 빈 리스트가 되지 않는다 — 위 "if not
        # items" 는 사실상 죽은 코드였다. 그 결과 호가 없는 모든 항이
        # clause(마커 포함 raw)가 아니라 marker=None 인 item(마커가 이미
        # 빠진 clause.text 기반)으로 대체되어, 위치확정된 new_text에서
        # 항 기호(①②③…)가 조용히 사라졌다(old_text는 이 경로를 타지
        # 않아 멀쩡했으므로 old/new 비대칭으로 드러남). "items에 실제
        # 마커가 하나라도 있는가"로 진짜 분해 여부를 판정해야 한다.
        if not items or not any(it.marker for it in items):
            result.append(clause)
            continue
        for idx, item in enumerate(items):
            if idx == 0 and item.marker is None and clause.marker:
                # ★★ 실측 발견(2026-07-19, 전자정부법 제56조의2① 등): 호
                # 목록이 시작되기 전 전제문(preamble)도 marker=None 인
                # 별도 Fragment로 분리되는데, 이 조각 역시 clause.text
                # (마커 이미 제거됨) 기반이라 raw에 "①"이 없다 — 위
                # "호 없는 항" 케이스와 같은 유실이 전제문에서도 재현된다.
                # 전제문은 개념상 그 항(①)의 일부이므로 raw 표시에는 항
                # 마커를 되살려 붙인다(검색용 text는 그대로 두어 매칭
                # 로직에는 영향이 없게 한다).
                item = Fragment(item.level, item.marker, item.text, f"{clause.marker} {item.raw}")
            subs = split_by_subitem(item.text)
            if not subs:
                result.append(item)
            else:
                result.extend(subs)
    return result


def searchable_fragments(text: str) -> list[Fragment]:
    """검색에 쓸 조각만. (생 략) / (현행과 같음) 제외."""
    return [f for f in split_all(text) if f.searchable]


def split_to_item_level(text: str) -> list[tuple[str, str, Fragment]]:
    """항 → 호 까지만 분해한다(목은 내려가지 않는다).

    ★ 설계(2026-07-20, old_text 정밀화): db/repo.py가 old_text를 위치별로
    정밀 매칭할 때 쓴다. 목(가나다) 단위까지 내려가면 괄호 안 숫자 참조,
    한글 문장 종결어미 등 목 마커 오탐 위험이 이번 세션에서만 여러 번
    발견됐던 만큼(split_by_subitem 주석 참고), 상대적으로 훨씬 안정적인
    호 단위에서 멈춘다 — 목 단위 행은 자기가 속한 호 전체의 old_text를
    보게 되지만, 이건 새로운 종류의 애매함이 아니라 structural_expansions
    가 이미 쓰는 "그룹당 참고 old_text 1개" 규칙이 더 좁은 범위(조문
    전체가 아니라 호 하나)에 적용되는 것뿐이다.

    반환값: (항 마커 또는 "", 호 마커 또는 "", Fragment) 튜플 목록 —
    항 하나에 호가 여러 개 딸려도 각 조각이 어느 항 소속인지 식별
    가능해야 하므로 clause 마커를 함께 들고 다닌다.
    """
    if not text or not text.strip():
        return []
    out: list[tuple[str, str, Fragment]] = []
    for clause in split_by_clause(text) or [Fragment(Level.NONE, None, text, text)]:
        clause_marker = clause.marker or ""
        items = split_by_item(clause.text)
        if not items or not any(it.marker for it in items):
            out.append((clause_marker, "", clause))
            continue
        for idx, item in enumerate(items):
            if idx == 0 and item.marker is None and clause.marker:
                item = Fragment(item.level, item.marker, item.text, f"{clause.marker} {item.raw}")
            out.append((clause_marker, item.marker or "", item))
    return out


# ---------------------------------------------------------------------------
# 조문 번호
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleNo:
    """조문 번호.

    API 의 6자리 조문번호 인코딩:
        앞 4자리 = 조번호, 뒤 2자리 = 가지번호("의N", 없으면 00)
        예) '000602' → 제6조의2   /  '002100' → 제21조
    """

    number: int
    branch: int = 0

    @property
    def label(self) -> str:
        return f"제{self.number}조" + (f"의{self.branch}" if self.branch else "")

    @classmethod
    def from_code(cls, code: str) -> "ArticleNo":
        """6자리 코드 파싱. '000602' → 제6조의2"""
        code = (code or "").strip()
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"조문번호 코드가 6자리 숫자가 아님: {code!r}")
        return cls(number=int(code[:4]), branch=int(code[4:]))

    @classmethod
    def from_text(cls, text: str) -> "ArticleNo | None":
        """'제6조의2(기준 중위소득의 산정) …' 같은 문장에서 조번호 추출."""
        m = _ARTICLE_HEAD_RE.match((text or "").strip())
        if not m:
            return None
        return cls(number=int(m.group(1)), branch=int(m.group(2) or 0))

    def to_code(self) -> str:
        return f"{self.number:04d}{self.branch:02d}"


def article_title(text: str) -> str | None:
    """'제6조의2(기준 중위소득의 산정)' → '기준 중위소득의 산정'"""
    m = _ARTICLE_HEAD_RE.match((text or "").strip())
    return m.group(3) if m and m.group(3) else None


#: 스킵 표시("① (생략)" / "1.∼4.(현행과 같음)") 범위 파싱용.
#: ★ 실측 발견(2026-07-18, 보안업무규정 시행규칙 제56조①): "3.·4. (생 략)"
#: 처럼 "∼" 대신 가운뎃점(U+00B7 MIDDLE DOT)으로 인접한 두 호를 이은
#: 표기도 있다("1.·2." 도 같은 조문 ③에서 실측됨). 지금까지 관측된 건
#: 모두 바로 이웃한 두 번호(3·4, 1·2)뿐이라 범위로 펼쳐도 나열로 읽어도
#: 결과가 같다 — "·"가 언젠가 안 이웃한 번호를 잇는 경우가 나오면(예:
#: "2.·5.") 이 가정이 깨지므로 그때 다시 봐야 한다.
_SKIP_RANGE_RE = re.compile(
    rf"^\s*(?P<start>[{CIRCLED}]|{_ITEM_PAT})"
    rf"(?:\s*[~∼·]\s*(?P<end>[{CIRCLED}]|{_ITEM_PAT}))?"
    rf"\s*\(\s*(?:생\s*략|현행과\s*같음)\s*\)\s*$"
)


def parse_skip_range(text: str) -> list[str] | None:
    """스킵 표시 조각에서 "안 바뀐" 항/호 라벨 목록을 뽑는다.

    ★ 설계(2026-07-18, admrul-only unchanged 감지): 행정규칙은 법령과
    달리 항제개정유형 같은 공식 "안 바뀜" 태그가 없다. 대신 신구법
    비교 API 자체가 "1. ∼ 4. (생 략)" / "④ (현행과 같음)" 같은 스킵
    표시 관례로 "이 범위는 안 바뀌었다"를 이미 알려주고 있는데, 지금까지
    parse/oldnew.py classify()는 이 정보를 UNCHANGED로만 분류하고 버려
    왔다(extract_changes). 이 함수는 그 표시를 실제 라벨 목록으로
    되살린다. 스킵 표시가 아니면 None.
    """
    m = _SKIP_RANGE_RE.match((text or "").strip())
    if not m:
        return None
    return _expand_marker_range(m.group("start"), m.group("end") or m.group("start"))


def _expand_marker_range(start: str, end: str) -> list[str] | None:
    if start in CIRCLED or end in CIRCLED:
        if start not in CIRCLED or end not in CIRCLED:
            return None
        si, ei = CIRCLED.index(start), CIRCLED.index(end)
        if si > ei:
            return None
        return [CIRCLED[i] for i in range(si, ei + 1)]

    sm = re.match(r"^(\d+)(?:의(\d+))?\.$", start)
    em = re.match(r"^(\d+)(?:의(\d+))?\.$", end)
    if not sm or not em:
        return None
    if sm.group(2) or em.group(2):
        # ★ 가지번호(예: "6의2.") 낀 범위는 확장 규칙이 애매해 범위
        # 밖으로 둔다(2026-07-18 설계에서 명시적으로 out of scope).
        return None
    s, e = int(sm.group(1)), int(em.group(1))
    if s > e:
        return None
    return [f"{i}." for i in range(s, e + 1)]


#: 개정이력 각주("<개정 2011.5.13., 2015.9.21., ...>"), 이미지 태그
#: ("<img id="...">") 등 <...> 로 감싸인 메타주석. <P> 태그는 이미 별도로
#: 벗겨지므로 여기 도달하는 시점엔 절대 <P>일 수 없다.
_ANNOTATION_RE = re.compile(r"<[^<>]*>")

#: 실측 발견((계약예규) 협상에 의한 계약체결기준): oldAndNew 블록 경계에서
#: 각주가 "<개정 2020.9.24." 처럼 닫는 ">" 없이 잘리는 경우가 있다(다음
#: 블록으로 잘려 넘어간 것으로 추정). 이런 미완성 각주는 문자열 끝까지
#: 통째로 제거한다.
_TRAILING_UNCLOSED_ANNOTATION_RE = re.compile(r"<[^<>]*$")

#: 실측 발견((계약예규) 정부 입찰ㆍ계약 집행기준): "[본조신설 2018.3.20.]"
#: "[종전 제99조는 제100조로 이동…]" 처럼 대괄호로 감싸인 조문 이력
#: 각주도 있다 — parse/fulltext.py 가 이미 알고 있는 "조문참고자료" 필드와
#: 같은 종류의 메타데이터가 oldAndNew 평문 블록에는 그대로 섞여 나온다.
_BRACKET_ANNOTATION_RE = re.compile(r"\[[^\[\]]*\]")


def strip_annotations(text: str) -> str:
    """본문 중간에 섞인 <...> 메타주석을 제거한다.

    ★★ 실측 발견(2026-07-16, (계약예규) 예정가격작성기준): "<개정
    2011.5.13., 2015.9.21., 2025.5.1.>" 처럼 조문 문장 중간에 개정이력
    각주가 그대로 섞여 있는 경우가 있다. 이 안의 "5.13." "9.21." 같은
    날짜 조각이 호 번호 패턴(\\d+\\.)과 우연히 겹쳐, 목 분해와 똑같은
    문제(가짜 경계로 오분해)를 일으킨다 — 실제 현행 조문에는 이 각주
    자체가 없으므로 애초에 검색 대상에서 제거하는 게 근본 해법이다.
    "<img id="...">" 같은 이미지 태그도 같은 이유로 함께 제거된다.
    classify()가 이미 <신 설>/<삭제> 같은 전체-블록 마커를 판정에 쓴
    뒤이므로, 이 함수는 그 판정이 끝난 '검색용' 텍스트에만 적용한다
    (locate 단계 진입 직전).
    """
    t = _ANNOTATION_RE.sub(" ", text or "")
    t = _TRAILING_UNCLOSED_ANNOTATION_RE.sub(" ", t)
    return _BRACKET_ANNOTATION_RE.sub(" ", t)


def strip_article_head(text: str) -> str:
    """맨 앞의 '제N조(의M)(제목)' 헤더를 제거하고 본문만 남긴다.

    실측 발견: oldAndNew 블록이 "제6조의2(기준 중위소득의 산정) ① 기준
    중위소득은 …" 처럼 조문 제목으로 시작하는 경우가 있다. 이 제목
    부분은 실제 조문 '내용'이 아니라 헤더이므로, lawService 항내용
    필드에는 보통 포함되지 않는다. 그대로 두면 항상 0건 매칭되는
    무의미한 조각이 하나 더 생기므로, 위치 검색 전에 제거한다.
    """
    text = (text or "").strip()
    m = _ARTICLE_HEAD_RE.match(text)
    if not m:
        return text
    return text[m.end():].strip()