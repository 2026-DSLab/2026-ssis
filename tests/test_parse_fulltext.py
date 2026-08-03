"""parse/fulltext.py flatten_searchable 테스트. 케이스는 실측 사례 기반."""

from lawtrack.parse.fulltext import (
    ArticleUnit,
    ClauseNode,
    ItemNode,
    SubItemNode,
    flatten_searchable,
    parse_admrul_units,
    parse_articles,
)


class TestFlattenSearchablePreambleNotLost:
    """실측(전자정부법 제56조의2 항①, 제2조제11호): 자식(호/목)이 있는
    부모(항/호)라도 부모 자신의 본문(전제문)이 검색 대상에서 빠지면 안 된다.
    예전엔 자식이 있으면 부모 유닛 생성 자체를 건너뛰어, 자식 목록
    앞부분(전제문) 안에서 바뀐 내용은 영원히 위치확정에 실패했다."""

    def test_clause_with_items_keeps_its_own_unit(self):
        """실측: 전자정부법 제56조의2 항① — '중앙사무관장기관의 장은 …
        통보하여야 한다.'는 항내용 필드 자체에 있고, 뒤따르는 호 1~5와는
        별개의 텍스트다."""
        clause = ClauseNode(
            no="①",
            text="① 중앙사무관장기관의 장은 …통보하여야 한다. <개정 2025.1.7>",
            change_type="개정",
            change_dates="2025.1.7",
            items=(
                ItemNode(no="1.", branch="", text="정보시스템 장애 예방…추진방향"),
                ItemNode(no="2.", branch="", text="정보시스템의 중요도…사항"),
            ),
        )
        article = ArticleUnit(
            code="56", branch="2", label="제56조의2", title="정보시스템 장애 예방",
            changed=True, content="제56조의2(정보시스템 장애 예방)", clauses=(clause,),
        )
        units = flatten_searchable([article])

        clause_units = [u for u in units if u.clause_no == "①" and u.item_label == ""]
        assert len(clause_units) == 1
        assert "통보하여야 한다" in clause_units[0].text

        item_units = [u for u in units if u.item_label in ("1.", "2.")]
        assert len(item_units) == 2

    def test_item_with_subitems_keeps_its_own_unit(self):
        """실측: 전자정부법 제2조제11호 — '"정보자원"이란 …말한다. 다만…
        한정한다.'는 호내용 필드 자체에 있고, 뒤따르는 목 가~바와는
        별개의 텍스트다."""
        item = ItemNode(
            no="11.", branch="",
            text="11. \"정보자원\"이란 …다만, 이용하는 경우에는 나목부터 라목까지에 한정한다.",
            subitems=(
                SubItemNode(label="가.", text="행정정보"),
                SubItemNode(label="나.", text="정보시스템"),
            ),
        )
        clause = ClauseNode(no="", text="", change_type="", change_dates="", items=(item,))
        article = ArticleUnit(
            code="2", branch="", label="제2조", title="정의", changed=True,
            content="제2조(정의)", clauses=(clause,),
        )
        units = flatten_searchable([article])

        item_units = [u for u in units if u.item_label == "11." and u.subitem_label == ""]
        assert len(item_units) == 1
        assert "정보자원" in item_units[0].text

        subitem_units = [u for u in units if u.subitem_label in ("가.", "나.")]
        assert len(subitem_units) == 2

    def test_clause_without_items_unaffected(self):
        """자식이 없는 항은 예전처럼 하나의 유닛만 생성한다 (회귀 방지)."""
        clause = ClauseNode(no="①", text="① 단순한 항 내용", change_type="", change_dates="")
        article = ArticleUnit(
            code="1", branch="", label="제1조", title="목적", changed=False,
            content="제1조(목적)", clauses=(clause,),
        )
        units = flatten_searchable([article])
        assert len(units) == 1
        assert units[0].clause_no == "①"

    def test_article_with_headerless_item_container_keeps_own_intro(self):
        """★★ 실측(2026-07-16, 공공기관의 정보공개에 관한 법률 제22조):
        항 없이 호가 조문에 바로 붙는 구조("항" JSON 키가 항번호/항내용
        없이 "호" 배열만 담은 빈 컨테이너)가 있다. art.clauses 가 비어
        있지 않아(빈 ClauseNode 하나) "항 없는 조문" 분기를 안 타는데,
        조문 자신의 도입부 문장은 그 어떤 항/호 필드에도 없고 오직
        art.content 에만 있다 — 조문→항 레벨에서도 동일한 '자식이 있으면
        부모 텍스트가 사라지는' 문제가 있었다."""
        item1 = ItemNode(no="1.", branch="", text="1.  정보공개에 관한 정책 수립")
        item2 = ItemNode(no="2.", branch="", text="2.  정보공개에 관한 기준 수립")
        headerless_clause = ClauseNode(no="", text="", change_type="", change_dates="", items=(item1, item2))
        article = ArticleUnit(
            code="22", branch="", label="제22조", title="정보공개위원회의 설치",
            changed=True,
            content="제22조(정보공개위원회의 설치) 다음 각 호의 사항을 심의ㆍ조정하기 "
                    "위하여 행정안전부장관 소속으로 정보공개위원회를 둔다.",
            clauses=(headerless_clause,),
        )
        units = flatten_searchable([article])

        intro_units = [u for u in units if u.clause_no == "" and u.item_label == "" and u.text]
        assert len(intro_units) == 1
        assert "다음 각 호의 사항을" in intro_units[0].text

        item_units = [u for u in units if u.item_label in ("1.", "2.")]
        assert len(item_units) == 2


class TestArticleCodeUniquePerBranch:
    """★★ 실측(2026-07-16, contract/export.py 실데이터 검증 — 전자정부법
    제56조~제56조의6): article_code가 조문가지번호를 빼고 순수 조번호만
    담아서(예: "56"), 제56조/제56조의2/…/제56조의6이 전부 같은
    article_code를 공유했다. article_diff의 UNIQUE KEY가 article_code+
    clause_no 등으로만 구성되고 article_label은 ON DUPLICATE KEY UPDATE
    대상이 아니어서, 서로 다른 조문의 같은 항번호(둘 다 "①")가 충돌해
    라벨은 먼저 들어간 값 그대로, 본문은 나중 값으로 뒤섞이는 심각한
    버그가 있었다."""

    def test_articles_sharing_base_number_get_distinct_codes(self):
        clause = ClauseNode(no="①", text="① 내용", change_type="", change_dates="")
        art2 = ArticleUnit(
            code="56", branch="2", label="제56조의2", title="A", changed=True,
            content="제56조의2(A)", clauses=(clause,),
        )
        art5 = ArticleUnit(
            code="56", branch="5", label="제56조의5", title="B", changed=True,
            content="제56조의5(B)", clauses=(clause,),
        )
        units = flatten_searchable([art2, art5])
        codes = {u.article_code for u in units}
        assert len(codes) == 2, "서로 다른 조문의 article_code가 겹치면 안 됨"

    def test_base_article_and_branch_article_get_distinct_codes(self):
        """제56조(가지번호 없음) 자체도 제56조의2와 구분돼야 한다."""
        clause = ClauseNode(no="①", text="① 내용", change_type="", change_dates="")
        base = ArticleUnit(
            code="56", branch="", label="제56조", title="A", changed=True,
            content="제56조(A)", clauses=(clause,),
        )
        branch2 = ArticleUnit(
            code="56", branch="2", label="제56조의2", title="B", changed=True,
            content="제56조의2(B)", clauses=(clause,),
        )
        units = flatten_searchable([base, branch2])
        codes = {u.article_code for u in units}
        assert len(codes) == 2


class TestParseAdmrulUnits:
    """★★ 실측(2026-07-16, 행정규칙 15건 실제 개정 시뮬레이션 검증):
    행정규칙 본문조회 응답은 법령과 구조가 전혀 다르다 — 조문/항/호가
    JSON 트리로 안 쪼개져 있고, "AdmRulService.조문내용"이라는 평문
    문자열 배열 하나뿐이며 한 조문 전체가 한 줄에 통째로 이어붙어 있다.
    예전엔 법령 전용 파서(parse_articles)를 그대로 써서 조문을 0건
    찾았고, 그 결과 검색 유닛이 하나도 없어 모든 위치확정이 100%
    실패했다(실측: 15개 중 5개 시뮬레이션에서 전부 succ=0)."""

    def test_flat_text_lines_split_into_units(self):
        raw = {
            "AdmRulService": {
                "조문내용": [
                    "제1장 총칙",  # 장 제목 — 조문이 아니므로 걸러져야 함
                    "제1조(목적) 이 예규는 계약조건을 정함을 목적으로 한다.",
                    "제5조(사용언어) ① 계약을 이행함에 있어서 사용하는 언어는 "
                    "한국어를 원칙으로 한다.② 계약담당공무원은 필요하다고 "
                    "인정하는 경우에는 외국어를 사용할 수 있다.",
                ]
            }
        }
        units = parse_admrul_units(raw)
        labels = {u.article_label for u in units}
        assert "제1조" in labels
        assert "제5조" in labels
        assert "제1장" not in labels  # 장 제목은 조문으로 안 잡힘

        clause_units = [u for u in units if u.article_label == "제5조"]
        clause_nos = {u.clause_no for u in clause_units}
        assert clause_nos == {"①", "②"}

    def test_nested_under_jomun_key(self):
        """실측(보안업무규정 시행규칙, law_id=9008822): 같은 배열이
        "AdmRulService.조문내용"이 아니라 "AdmRulService.조문.조문내용"
        한 겹 더 안에 오는 경우도 있다."""
        raw = {
            "AdmRulService": {
                "조문": {
                    "조문내용": [
                        "제1조(목적) 이 훈령은 보안업무의 시행에 필요한 사항을 규정한다.",
                    ]
                }
            }
        }
        units = parse_admrul_units(raw)
        assert len(units) == 1
        assert units[0].article_label == "제1조"

    def test_empty_when_no_jomun_content(self):
        raw = {"AdmRulService": {"별표": []}}
        assert parse_admrul_units(raw) == []

    def test_embedded_annotation_does_not_break_subitem_split(self):
        """★★ 실측(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준 law_id=34470
        제34조④): "<개정 2008.12.29.>" 같은 각주가 호/목 사이에 공백 없이
        섞여 있으면, 각주 속 날짜 조각("2008.")이 호 번호로 오인돼 item_label
        이 "2008." 처럼 깨지고 그 여파로 진짜 1호의 목(가나다) 분해가 통째로
        시작조차 안 됐다. strip_annotations 를 분해 전에 적용해 이 각주
        오염 문제는 해결한다.

        단, "…100분의 502. 물품의 제조…"처럼 앞 호의 숫자 내용과 다음 호
        번호가 공백 없이 바로 붙는 경우(1호와 2호 사이)는 다자리 숫자 보호
        규칙(_ITEM_RE 의 (?<!\\d))과 근본적으로 충돌해 일반 규칙으로는 안전하게
        구분할 수 없는 진짜 애매한 case라 여기서는 다루지 않는다."""
        raw = {
            "AdmRulService": {
                "조문내용": [
                    "제34조(적용범위) ④ 다음 각호에 따라 지급한다.<개정 2008.11.1, "
                    "개정 2026.4.1.>1. 공사가. 계약금액이 100억원이상인 경우 : "
                    "100분의 30 <개정 2008.12.29.>나. 계약금액이 20억원이상 "
                    "100억원 미만인 경우 : 100분의 40 <개정 2008.12.29.>다. "
                    "계약금액이 20억원 미만인 경우 : 100분의 50",
                ]
            }
        }
        units = parse_admrul_units(raw)
        item_labels = {u.item_label for u in units if u.clause_no == "④"}
        assert item_labels == {"", "1."}  # "" 는 1호 앞 전제문("다음 각호에…")
        assert "2008." not in item_labels

        subs = {u.subitem_label for u in units if u.clause_no == "④" and u.item_label == "1."}
        assert subs == {"", "가.", "나.", "다."}  # "" 는 "공사" 전제문


def _raw_article_with_sibling_mok(extra_item: dict | None = None) -> dict:
    """실측(2026-07-31, 국가를 당사자로 하는 계약에 관한 법률 시행령
    제26조①) 구조의 축소 재현: 목이 각 호 안에 있지 않고, 항의 형제로
    호 개수만큼의 목 그룹이 하나의 배열에 통째로 붙는다. 목번호가 '가.'
    로 리셋되는 지점이 새 호의 시작이다(실측에서는 호 5개·목 40개)."""
    hos = [
        {"호번호": "1.", "호내용": "1. 첫째 사유"},
        {"호번호": "2.", "호내용": "2. 둘째 사유"},
    ]
    if extra_item:
        hos.append(extra_item)
    return {
        "법령": {
            "조문": {
                "조문단위": [
                    {
                        "조문번호": "26",
                        "조문가지번호": "",
                        "조문내용": "제26조(수의계약에 의할 수 있는 경우)",
                        "조문제목": "수의계약에 의할 수 있는 경우",
                        "조문변경여부": "N",
                        "항": [
                            {
                                "항번호": "①",
                                "항내용": "① 법 제7조제1항 단서에 따라 수의계약을 할 수 있는 "
                                "경우는 다음 각 호와 같다.",
                                "호": hos,
                                "목": [
                                    {"목번호": "가.", "목내용": "가. 첫째의 가목"},
                                    {"목번호": "나.", "목내용": "나. 첫째의 나목"},
                                    {"목번호": "가.", "목내용": "가. 둘째의 가목"},
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }


class TestSiblingMokRecoveredIntoItems:
    """실측(2026-07-31, 국가를 당사자로 하는 계약에 관한 법률 시행령
    제26조①, redo5.py 3번째 재처리 케이스): 목이 호 안이 아니라 항의
    형제로 통째로(호 5개분 목 40개가 배열 하나에) 붙어 나오는 문서가
    있었다 — 각 호 객체엔 "목" 키 자체가 없었다. 그 결과 "지정받은 제품"
    같은 실제 개정 문구가 검색 유닛(units)에 전혀 안 잡혀 위치확정이
    100% 실패(0건실패)했다. 목번호가 '가.'로 리셋되는 지점을 호 경계로
    보고 순서대로 이어붙이면 복구된다."""

    def test_sibling_mok_split_and_attached_to_matching_item(self):
        articles = parse_articles(_raw_article_with_sibling_mok())
        assert len(articles) == 1
        clause = articles[0].clauses[0]
        assert len(clause.items) == 2
        item1, item2 = clause.items
        assert [s.text for s in item1.subitems] == ["가. 첫째의 가목", "나. 첫째의 나목"]
        assert [s.text for s in item2.subitems] == ["가. 둘째의 가목"]

    def test_sibling_mok_not_attached_when_group_count_mismatches_item_count(self):
        """그룹 수와 호 개수가 안 맞아 모호하면 잘못된 호에 붙이지 않고
        그대로 둔다 — 조용한 오귀속보다 0건실패가 낫다는 이 프로젝트의
        원칙 그대로다."""
        raw = _raw_article_with_sibling_mok(extra_item={"호번호": "3.", "호내용": "3. 셋째 사유"})
        articles = parse_articles(raw)
        clause = articles[0].clauses[0]
        assert len(clause.items) == 3
        assert all(len(it.subitems) == 0 for it in clause.items)

    def test_normal_nested_mok_unaffected(self):
        """목이 정상적으로 각 호 안에 이미 있는 경우(원래 동작)는 그대로
        보존된다 — 회귀 방지."""
        raw = {
            "법령": {"조문": {"조문단위": [{
                "조문번호": "2", "조문가지번호": "",
                "조문내용": "제2조(정의)", "조문제목": "정의", "조문변경여부": "N",
                "항": [{
                    "항번호": "", "항내용": "",
                    "호": [{
                        "호번호": "11.", "호내용": "11. 정보자원",
                        "목": [{"목번호": "가.", "목내용": "가. 행정정보"}],
                    }],
                }],
            }]}}
        }
        articles = parse_articles(raw)
        clause = articles[0].clauses[0]
        assert len(clause.items) == 1
        assert [s.text for s in clause.items[0].subitems] == ["가. 행정정보"]
