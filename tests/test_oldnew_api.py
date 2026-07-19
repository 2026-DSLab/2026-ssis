"""api/oldnew.py fetch_admrul_oldnew 회귀 테스트. 케이스는 실측 사례 기반."""

from unittest.mock import MagicMock

from lawtrack.api.client import ApiResponse
from lawtrack.api.oldnew import fetch_admrul_oldnew


class TestAdmrulNoComparisonJsonForm:
    """★★ 실측(2026-07-16, 하도급거래공정화 지침·중소기업자간 경쟁제품
    직접생산 확인기준): "신구법 없음" 메시지가 비-JSON 텍스트가 아니라
    {"Law": "일치하는 신구법 없습니다."} 형태의 유효한 JSON으로도 온다.
    이전엔 resp.data가 None일 때만 마커를 확인해서, JSON으로 온 경우
    available=True로 잘못 판정하고 old_texts/new_texts가 빈 채로
    "비교 가능한데 변경사항 없음"처럼 보이는 결과가 나왔다."""

    def test_json_wrapped_no_comparison_marker(self):
        client = MagicMock()
        client.service.return_value = ApiResponse(
            url="", status_code=200, content_type="application/json",
            text='{"Law": "일치하는 신구법 없습니다."}',
            data={"Law": "일치하는 신구법 없습니다."},
        )
        result = fetch_admrul_oldnew(client, "2100000251404")
        assert result.available is False
        assert result.reason == "no_comparison_admrul_json"

    def test_plain_text_no_comparison_still_works(self):
        """회귀 방지: 기존에 되던 비-JSON 텍스트 케이스."""
        client = MagicMock()
        client.service.return_value = ApiResponse(
            url="", status_code=200, content_type="text/plain",
            text="<Law>일치하는 신구법 없습니다. </Law>",
            data=None,
        )
        result = fetch_admrul_oldnew(client, "2100000251404")
        assert result.available is False
        assert result.reason == "no_comparison_admrul_text"

    def test_real_comparison_data_still_works(self):
        """회귀 방지: 진짜 비교 데이터가 있는 정상 케이스."""
        client = MagicMock()
        client.service.return_value = ApiResponse(
            url="", status_code=200, content_type="application/json",
            text="",
            data={
                "AdmRulOldAndNewService": {
                    "구조문_기본정보": {}, "신조문_기본정보": {},
                    "구조문목록": {"조문": [{"no": "1", "content": "구내용"}]},
                    "신조문목록": {"조문": [{"no": "1", "content": "신내용"}]},
                }
            },
        )
        result = fetch_admrul_oldnew(client, "2100000251404")
        assert result.available is True
        assert result.old_texts == ["구내용"]
        assert result.new_texts == ["신내용"]


class TestAdmrulNoComparisonFieldForm:
    """★★★ 실측(2026-07-18, (계약예규) 공동계약운용요령·중소 소프트웨어사업자의
    사업 참여 지원에 관한 지침): "신구법 없음"이 텍스트 마커가 아니라 법령과
    똑같은 "신구법존재여부": "N" 필드로도 온다(구조문목록/신조문목록 키 자체가
    없음). 이 필드를 확인 안 하면 available=True에 old_texts/new_texts가 빈
    채로 나가 "비교했는데 진짜 0건 변경"처럼 조용히 오판된다 — 실제로는
    "비교 자체가 불가능"인 것과 의미가 다르다."""

    def test_field_based_no_comparison_recognized(self):
        client = MagicMock()
        client.service.return_value = ApiResponse(
            url="", status_code=200, content_type="application/json",
            text="",
            data={
                "AdmRulOldAndNewService": {
                    "신구법존재여부": "N",
                    "구조문_기본정보": {
                        "현행여부": "Y", "행정규칙ID": "27945",
                        "행정규칙일련번호": "2100000275812",
                    },
                    "신조문_기본정보": {
                        "현행여부": "N", "행정규칙ID": "27945",
                        "행정규칙일련번호": "2100000272428",
                    },
                }
            },
        )
        result = fetch_admrul_oldnew(client, "2100000275812")
        assert result.available is False
        assert result.reason == "no_comparison_field"
        assert result.old_texts == []
        assert result.new_texts == []
