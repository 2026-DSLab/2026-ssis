"""버전 이력 조회 — 전문 비교 페이지(webapp)가 쓰는 "N단 비교" 지원.

★ 배경(2026-08-05, 담당자 요구사항): 감지된 변경 조문만이 아니라 법령/
행정규칙의 "전문"을 개정 전/개정 후로 나란히 보여주고, 필요하면 그 전
버전(전전)까지 보여달라는 요청. documents 테이블은 지금까지 실제로
감지·저장된 버전만 갖고 있어 대부분의 법(102건 중 100건)은 "현재"
버전 하나뿐이다.

★ 실측 확인(2026-08-05, 전자정부법/조달청 내자구매업무 처리규정으로
직접 라이브 API 호출해 검증): oldAndNew API 의 MST/ID 파라미터는 "현재
버전"에 국한되지 않는다 — 과거 일련번호를 그대로 넣어도 "그 버전 vs
그 버전의 직전 버전"을 정확히 돌려준다. 즉 한 번에 한 단계씩,
원하는 만큼 거슬러 올라갈 수 있다(법령/행정규칙 둘 다 동일하게 동작
확인함). 체인의 끝(최초 제정본)에 도달하면 신구법존재여부="N"으로
응답한다 — 오류가 아니라 정상적인 종료 신호로 처리한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lawtrack.api.client import LawApiClient
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
    "2100000272436" 같은 내부 번호보다 날짜를 알아보기 쉬워한다."""
    promulgation_date: str = ""
    revision_type: str = ""


def kind_of(law_type: str) -> str:
    """watchlist.law_type("법률"/"시행령"/"행정규칙" 등) → documents.kind("law"/"admrul").

    ★ src/lawtrack/detect.py 의 ADMRUL_LAW_TYPE 판정과 반드시 같아야
    한다 — 다르게 판정하면 documents 테이블에서 같은 법을 서로 다른
    kind로 엇갈려 저장해 캐시가 영영 안 맞는 사고가 난다. 그래서 새로
    정의하지 않고 그 상수를 그대로 재사용한다.
    """
    return ADMRUL_KIND if law_type == ADMRUL_LAW_TYPE else LAW_KIND


def get_previous_serial(client: LawApiClient, kind: str, serial_no: str) -> str | None:
    """serial_no 버전의 직전 버전 일련번호. 최초 제정본이면(더 이전 버전이
    없으면) None — 오류가 아니라 정상적인 체인의 끝이다."""
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

    ★ full_text는 그대로 documents에 캐싱한다 — 한 번 받아두면 이 버전은
    다시는 안 바뀌므로(과거 버전은 불변), 다음 방문부터는 API를 다시
    부를 필요가 없다.
    """
    cached = repo.fetch(kind, doc_id, serial_no)
    if cached is not None:
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
    """현재 버전에서 시작해 최대 depth개 버전을 과거 방향으로 모은다.

    반환은 오래된 버전 → 최신 버전 순서(화면에 왼쪽부터 놓기 좋게).
    최초 제정본에 닿으면 depth보다 짧게 끝난다 — 호출부가 "이전 버전
    없음"으로 처리해야 한다.

    ★ 설계(2026-08-05, 사용자 요청): 화면에 일련번호 대신 시행일/공포일을
    보여달라는 요청 — oldAndNew 응답 한 번(구조문_기본정보/신조문_기본정보)
    에 이미 두 버전(직전·현재)의 날짜가 같이 온다. 체인을 걸을 때 이미
    부르고 있는 그 호출에서 날짜까지 같이 챙기면 되므로, 날짜만 얻으려고
    API를 더 부를 필요가 없다.
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
        full_text = get_or_fetch_full_text(
            client, repo, kind=kind, doc_id=doc_id, doc_name=doc_name, serial_no=s,
        )
        info = version_info.get(s)
        chain.append(VersionText(
            serial_no=s, full_text=full_text,
            enforce_date=info.enforce_date if info else "",
            promulgation_date=info.promulgation_date if info else "",
            revision_type=info.revision_type if info else "",
        ))
    return chain
