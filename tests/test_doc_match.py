"""doc_match 테스트.

- 단위: 정규화, 사전 로드, 매칭(표기 변형·이중계상 방지), HWPX 파서(합성 픽스처)
- 통합(골드셋): 표준가이드 요약본 PDF가 tests/fixtures/ 에 있으면 실행.
  없으면 skip — PDF는 용량 문제로 repo 커밋 대신 로컬 배치 권장.
"""
import io
import zipfile
from pathlib import Path

import pytest

from doc_match.dictionary import load_from_seed
from doc_match.extract import extract_hwpx, extract_text
from doc_match.match import match_pages
from doc_match.normalize import norm
from doc_match.report import build_summary

SEED = Path(__file__).resolve().parents[1] / "database" / "seed_watchlist.sql"
# 파일 이름을 ASCII 로 둠(원본: 표준가이드요약본.pdf). 배포용 zip 을
#   UTF-8 플래그를 무시하는 압축 해제 도구로 풀면 한글 이름이 깨져, 이 골드셋
#   테스트가 "PDF 미배치"로 조용히 건너뛰어짐 — 받는 쪽에서는 통과한 것처럼
#   보이므로 눈치채기 어려움.
GOLD_PDF = Path(__file__).parent / "fixtures" / "goldset_guide_summary.pdf"


@pytest.fixture(scope="module")
def d():
    return load_from_seed(str(SEED))


# ---------- normalize ----------

@pytest.mark.parametrize("a,b", [
    ("개인정보 보호법", "개인정보보호법"),
    ("정부 입찰·계약 집행기준", "정부 입찰․계약 집행기준"),   # 가운뎃점 변형
    ("초ㆍ중등교육법", "초중등교육법"),
    ("(계약예규) 용역계약일반조건", "용역계약일반조건"),
    ("「소프트웨어 진흥법」", "소프트웨어진흥법"),
])
def test_norm_variants_collapse(a, b):
    assert norm(a) == norm(b)


# ---------- dictionary ----------

def test_seed_loads_102(d):
    assert len(d.entries) == 102
    # official/internal 이원 키: 구명칭으로도 조회됨
    assert norm("국가정보화 기본법") in d.alias
    assert norm("지능정보화 기본법") in d.alias
    assert d.alias[norm("국가정보화 기본법")] == d.alias[norm("지능정보화 기본법")]
    # 약칭
    assert d.alias[norm("국가계약법")] == d.alias[norm("국가를 당사자로 하는 계약에 관한 법률")]


# ---------- match ----------

def test_longest_match_no_double_count(d):
    pages = ["「개인정보 보호법 시행령」 제30조에 따라 조치한다."]
    r = match_pages(pages, d)
    officials = [d.entries[h.law_id].official for h in r.hits]
    assert officials == ["개인정보 보호법 시행령"]  # 법률 본체로 이중 계상되지 않음


def test_match_without_brackets(d):
    pages = ["<참고> 소프트웨어 진흥법 제20조를 준용한다"]
    r = match_pages(pages, d)
    assert any(d.entries[h.law_id].official == "소프트웨어 진흥법" for h in r.hits)


def test_out_of_watchlist_candidate_with_suffix_filter(d):
    pages = ["「지방계약법」 및 「과업내용변경 관리내역서」를 참조."]
    r = match_pages(pages, d)
    names = [c.raw for c in r.candidates]
    assert "지방계약법" in names          # 법령류 접미 → 후보 채택
    assert "과업내용변경 관리내역서" not in names  # 서식명 → 필터링


# ---------- hwpx ----------

def test_hwpx_synthetic_fixture(tmp_path):
    xml = (
        '<?xml version="1.0"?><hml xmlns:hp="x">'
        "<hp:p><hp:t>본 사업은 「소프트웨어 진흥법」에 따른다.</hp:t></hp:p>"
        "<hp:p><hp:t charPrIDRef=\"1\">개인정보 보호법 시행령 준수.</hp:t></hp:p>"
        "</hml>"
    )
    p = tmp_path / "t.hwpx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Contents/section0.xml", xml)
        z.writestr("Contents/content.hpf", "")
    pages = extract_hwpx(str(p))
    assert len(pages) == 1
    assert "소프트웨어 진흥법" in pages[0]
    assert "개인정보 보호법 시행령" in pages[0]


# ---------- 통합 골드셋 ----------

@pytest.mark.skipif(not GOLD_PDF.exists(), reason="골드셋 PDF 미배치")
def test_goldset_guide_summary(d):
    pages = extract_text(str(GOLD_PDF))
    summary = build_summary(match_pages(pages, d), d)
    # 실측 기준: watchlist 26건 인용
    assert summary["watchlist_matched"] >= 24
    matched_names = {m["official_name"] for m in summary["matched"]}
    for name in ["개인정보 보호법", "소프트웨어 진흥법",
                 "(계약예규) 용역계약일반조건",
                 "행정기관 및 공공기관 정보시스템 구축·운영 지침"]:
        assert name in matched_names
    # 감시 대상 외: 수동 대조에서 확인된 항목들이 후보로 잡혀야 함
    cand_keys = {norm(c["name"]) for c in summary["out_of_watchlist_candidates"]}
    for name in ["지방계약법", "고용보험법", "전자정부지원사업 관리지침",
                 "보건복지부 정보화추진규정", "국가 정보보안 기본지침"]:
        assert norm(name) in cand_keys
