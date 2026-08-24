"""버전 이력 조회 — 전문 비교 페이지(webapp)가 쓰는 "N단 비교" 지원.

배경: 감지된 변경 조문만이 아니라 법령/
행정규칙의 "전문"을 개정 전/개정 후로 나란히 보여주고, 필요하면 그 전
버전(전전)까지 보여달라는 요청. documents 테이블은 지금까지 실제로
감지·저장된 버전만 갖고 있어 대부분의 법(102건 중 100건)은 "현재"
버전 하나뿐임.

실측 확인(전자정부법/조달청 내자구매업무 처리규정으로
직접 라이브 API 호출해 검증): oldAndNew API 의 MST/ID 파라미터는 "현재
버전"에 국한되지 않는다 — 과거 일련번호를 그대로 넣어도 "그 버전 vs
그 버전의 직전 버전"을 정확히 돌려줌. 즉 한 번에 한 단계씩,
원하는 만큼 거슬러 올라갈 수 있음(법령/행정규칙 둘 다 동일하게 동작
확인함). 체인의 끝(최초 제정본)에 도달하면 신구법존재여부="N"으로
응답함 — 오류가 아니라 정상적인 종료 신호로 처리함.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.api.fulltext import fetch_admrul_fulltext, fetch_law_fulltext
from lawtrack.api.oldnew import VersionInfo, fetch_admrul_oldnew, fetch_law_oldnew
from lawtrack.db.repo import VersionRepo
from lawtrack.detect import ADMRUL_LAW_TYPE

log = logging.getLogger(__name__)

LAW_KIND = "law"
ADMRUL_KIND = "admrul"


@dataclass(frozen=True)
class VersionText:
    serial_no: str
    full_text: dict
    enforce_date: str = ""
    """시행일자(YYYYMMDD). 화면에 일련번호 대신 보여줄 값 — 담당자가
    "2100000272436" 같은 내부 번호보다 날짜를 알아보기 쉬워함."""
    promulgation_date: str = ""
    revision_type: str = ""


def kind_of(law_type: str) -> str:
    """watchlist.law_type("법률"/"시행령"/"행정규칙" 등) → documents.kind("law"/"admrul").

    src/lawtrack/detect.py 의 ADMRUL_LAW_TYPE 판정과 반드시 같아야
    함 — 다르게 판정하면 documents 테이블에서 같은 법을 서로 다른
    kind로 엇갈려 저장해 캐시가 영영 안 맞는 사고가 남. 그래서 새로
    정의하지 않고 그 상수를 그대로 재사용함.
    """
    return ADMRUL_KIND if law_type == ADMRUL_LAW_TYPE else LAW_KIND


def get_previous_serial(client: LawApiClient, kind: str, serial_no: str) -> str | None:
    """serial_no 버전의 직전 버전 일련번호. 최초 제정본이면(더 이전 버전이
    없으면) None — 오류가 아니라 정상적인 체인의 끝임."""
    fetch_oldnew = fetch_law_oldnew if kind == LAW_KIND else fetch_admrul_oldnew
    result = fetch_oldnew(client, serial_no)
    if not result.available or not result.old_version.serial_no:
        return None
    return result.old_version.serial_no


def get_or_fetch_full_text(
    client: LawApiClient, repo: VersionRepo, *, kind: str,
    doc_id: str, doc_name: str, serial_no: str,
) -> dict:
    """documents에 이미 있으면 그대로, 없으면 실 API로 받아와 저장 후 반환.

    full_text는 그대로 documents에 캐싱함 — 한 번 받아두면 이 버전은
    다시는 안 바뀌므로(과거 버전은 불변), 다음 방문부터는 API를 다시
    부를 필요가 없음.
    """
    # 실측 버그(전수검증 — 전문 비교에서 "전문 내용을 찾지
    #   못했습니다"만 뜨는 법 2건): documents 에 full_text 가 {} 인 빈
    #   껍데기 행이 있었음(국민기초생활보장법 281585, 조달청 내자구매업무
    #   처리규정 2100000280340 — 둘 다 2026-07-15 22:14 저장으로, 파이프라인
    #   첫 실행(7-16)보다 앞선 초기 세팅 때 들어간 자리표시자임).
    #   "행이 있으면 캐시 적중"으로만 보면 이 빈 값을 정상 캐시로 믿고
    #   그대로 돌려줘, 파서가 유닛 0개를 내고 화면이 빈 채로 남음.
    #   내용이 없는 캐시는 캐시가 아니므로 미적중으로 보고 다시 받아옴
    #   (_insert 가 upsert 라 받아오면 그 자리에서 빈 행이 덮어써짐).
    cached = repo.fetch(kind, doc_id, serial_no)
    if cached:
        return cached

    if kind == LAW_KIND:
        result = fetch_law_fulltext(client, serial_no)
        repo.insert_law(doc_name, doc_id, serial_no, result.raw)
    else:
        result = fetch_admrul_fulltext(client, serial_no)
        repo.insert_admrul(doc_name, doc_id, serial_no, result.raw)
    return result.raw


def build_version_chain(
    client: LawApiClient, repo: VersionRepo, *, kind: str,
    doc_id: str, doc_name: str, current_serial: str, depth: int,
) -> list[VersionText]:
    """현재 버전에서 시작해 최대 depth개 버전을 과거 방향으로 모음.

    반환은 오래된 버전 → 최신 버전 순서(화면에 왼쪽부터 놓기 좋게).
    최초 제정본에 닿으면 depth보다 짧게 끝남 — 호출부가 "이전 버전
    없음"으로 처리해야 함.

    설계: 화면에 일련번호 대신 시행일/공포일을
    보여달라는 요청 — oldAndNew 응답 한 번(구조문_기본정보/신조문_기본정보)
    에 이미 두 버전(직전·현재)의 날짜가 같이 옴. 체인을 걸을 때 이미
    부르고 있는 그 호출에서 날짜까지 같이 챙기면 되므로, 날짜만 얻으려고
    API를 더 부를 필요가 없음.
    """
    fetch_oldnew = fetch_law_oldnew if kind == LAW_KIND else fetch_admrul_oldnew

    version_info: dict[str, VersionInfo] = {}
    serials = [current_serial]
    serial = current_serial
    for _ in range(depth - 1):
        result = fetch_oldnew(client, serial)
        if result.new_version.serial_no:
            version_info[result.new_version.serial_no] = result.new_version
        if not result.available or not result.old_version.serial_no:
            break
        version_info[result.old_version.serial_no] = result.old_version
        serials.append(result.old_version.serial_no)
        serial = result.old_version.serial_no
    serials.reverse()

    chain = []
    for s in serials:
        # 실측 버그(전수검증 — (계약예규) 정부 입찰·계약
        #   집행기준에서 HTTP 500): 과거 버전 하나의 전문을 API가 비정상
        #   응답(46자짜리 껍데기)으로 돌려주면 LawApiFormatError 가 그대로
        #   올라와 페이지 전체가 죽었음. 한 버전을 못 받은 것과 "이 법은
        #   아예 못 본다"는 전혀 다른 얘기다 — 받아온 버전만으로도 비교는
        #   보여줄 수 있으므로, 실패한 버전은 체인에서 빼고 계속함.
        #   전부 실패하면 빈 체인이 되고, 호출부가 그때 안내를 띄움.
        try:
            full_text = get_or_fetch_full_text(
                client, repo, kind=kind, doc_id=doc_id, doc_name=doc_name, serial_no=s,
            )
        except LawApiError as exc:
            log.warning(
                "전문을 받지 못해 이 버전은 건너뜁니다 (%s %s, 일련번호 %s): %s",
                kind, doc_name, s, exc,
            )
            continue
        info = version_info.get(s)
        chain.append(VersionText(
            serial_no=s, full_text=full_text,
            enforce_date=info.enforce_date if info else "",
            promulgation_date=info.promulgation_date if info else "",
            revision_type=info.revision_type if info else "",
        ))
    return chain
