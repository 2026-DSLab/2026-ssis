"""api/fulltext.py 본문조회 결과 파싱 테스트. 케이스는 실측 사례 기반."""

from lawtrack.api.fulltext import _build_admrul_result, _build_law_result


class TestNestedRevisionReasonExtraction:
    """★★ 실측(2026-07-16, 공공기관의 정보공개에 관한 법률): "제개정이유"/
    "개정문" 키는 그 자체가 문자열이 아니라 한 겹 더 감싸인
    {"제개정이유내용": [[...줄들...]]} 구조다. 예전엔 이 사실을 몰라
    _dig_any(root, ("제개정이유", "제개정이유내용"))가 "제개정이유"
    (딕셔너리) 자체를 text_of()에 넘겨 항상 빈 문자열이 나왔다."""

    def test_law_revision_reason_flattened_from_nested_lines(self):
        data = {
            "법령": {
                "기본정보": {"법령ID": "1357", "법령명_한글": "공공기관의 정보공개에 관한 법률"},
                "조문": {},
                "제개정이유": {
                    "제개정이유내용": [
                        ["[일괄개정]", "◇ 개정이유 및 주요내용", "  국무총리 소속을 행정안전부장관 소속으로 변경함.", "<법제처 제공>"]
                    ]
                },
                "개정문": {
                    "개정문내용": [["⊙법률 제19408호(2023.5.16)", "제1조 개정한다."]]
                },
            }
        }
        result = _build_law_result(data, "251019")
        assert "국무총리 소속을 행정안전부장관 소속으로 변경함." in result.revision_reason
        assert "제1조 개정한다." in result.revision_text

    def test_admrul_revision_reason_flattened_from_nested_lines(self):
        data = {
            "AdmRulService": {
                "행정규칙기본정보": {"행정규칙ID": "27946", "행정규칙명": "협상에 의한 계약체결기준"},
                "조문내용": [],
                "제개정이유": {"제개정이유내용": [["예정가격 산정기준을 명확히 하기 위함."]]},
            }
        }
        result = _build_admrul_result(data, "2100000272436")
        assert "예정가격 산정기준을 명확히 하기 위함." in result.revision_reason

    def test_missing_revision_reason_falls_back_to_empty(self):
        """필드 자체가 없으면 안전하게 빈 문자열 (회귀 방지)."""
        data = {"법령": {"기본정보": {}, "조문": {}}}
        result = _build_law_result(data, "1")
        assert result.revision_reason == ""
        assert result.revision_text == ""

    def test_plain_string_form_still_works(self):
        """혹시 평문 문자열로 오는 경우도 대비 (회귀 방지)."""
        data = {"법령": {"기본정보": {}, "조문": {}, "제개정이유": "평문 개정이유"}}
        result = _build_law_result(data, "1")
        assert result.revision_reason == "평문 개정이유"
