"""요약 보고서 웹페이지 — 최신 배치 결과 1개 + HWPX 다운로드.

lawtrack(감지·비교), summarizer(LLM 요약)와 같은 급의 최상위 패키지임.
DB(law_summary 테이블)에서 가장 최근 배치의 법령별 요약을 읽어 보여주고,
같은 배치가 만든 HWPX 보고서를 다운로드로 내려줌.

이 계층은 DB/파일을 새로 만들지 않음 — summarizer가 이미 만들어 둔
law_summary 테이블과 out/reports/*.hwpx 파일을 읽기만 함.
"""
