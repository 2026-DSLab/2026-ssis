-- PostgreSQL 스키마.
--
-- ★ MySQL → PostgreSQL 전환(2026-08-03): 데이터베이스 자체(law_tracking_db)는
-- 이 스크립트가 만들지 않는다 — Postgres는 CREATE DATABASE에 IF NOT EXISTS를
-- 지원하지 않고, 같은 트랜잭션 안에서 DB를 만들고 그 DB로 접속을 옮겨갈 수도
-- 없다(MySQL의 USE 같은 게 없다). 배포 시 먼저
--     createdb -U postgres -E UTF8 law_tracking_db
-- (또는 CREATE DATABASE law_tracking_db ENCODING 'UTF8';) 로 DB를 만든 뒤,
-- 그 DB에 접속한 상태에서 이 파일을 실행한다:
--     psql -U postgres -d law_tracking_db -f database/schema.sql

-- ★ 설계(2026-08-03 DB 간소화): 예전엔 laws/administrative_rules 두
-- 테이블로 나뉘어 있었다 — 컬럼 구성이 이름만 다를 뿐 완전히 같아서(법령
-- 명/ID/일련번호/전문JSON/타임스탬프), repo.py에도 거의 동일한 CRUD 코드가
-- 두 벌 있었다. kind 구분 컬럼('law'/'admrul') 하나로 합쳤다.
CREATE TABLE IF NOT EXISTS documents (
    kind VARCHAR(10) NOT NULL,
    doc_id VARCHAR(50) NOT NULL,
    doc_serial_no VARCHAR(50) NOT NULL,
    doc_name VARCHAR(255) NOT NULL,
    full_text JSONB NOT NULL,
    db_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, doc_id, doc_serial_no)
);

COMMENT ON COLUMN documents.kind IS '법령/행정규칙 구분 — ''law'' | ''admrul''. watchlist.law_type(법률/시행령/행정규칙 등 세부 종류)과는 다른, 이 테이블 내부 전용 이분값';
COMMENT ON COLUMN documents.doc_id IS '법령 ID 또는 행정규칙 ID';
COMMENT ON COLUMN documents.doc_serial_no IS '법령 일련번호 또는 행정규칙 일련번호';
COMMENT ON COLUMN documents.doc_name IS '법령명 또는 행정규칙명';
COMMENT ON COLUMN documents.full_text IS '전문 JSON — 법제처 API 원본 그대로(진실의 원천, 재파싱 가능하도록 절대 가공하지 않음)';
COMMENT ON COLUMN documents.db_timestamp IS 'DB 추가 또는 수정 시각';

CREATE TABLE IF NOT EXISTS watchlist (
    law_id VARCHAR(50) PRIMARY KEY,
    law_type VARCHAR(50) NOT NULL,
    official_name VARCHAR(255) NOT NULL,
    internal_name VARCHAR(255),
    dept_codes VARCHAR(255),
    status VARCHAR(50) NOT NULL,
    successor_law_id VARCHAR(50),
    scheduled_date DATE,
    last_serial_no VARCHAR(50),
    last_checked_at TIMESTAMP
);

COMMENT ON COLUMN watchlist.law_id IS '법령/행정규칙 ID';
COMMENT ON COLUMN watchlist.law_type IS '법령종류 (법률, 시행령 등)';
COMMENT ON COLUMN watchlist.official_name IS '법령명';
COMMENT ON COLUMN watchlist.internal_name IS '내부관리명';
COMMENT ON COLUMN watchlist.dept_codes IS '소관부처 코드(콤마구분)';
COMMENT ON COLUMN watchlist.status IS '상태 (현행, 시행전 등)';
COMMENT ON COLUMN watchlist.successor_law_id IS '후속(통합) 법령 ID';
COMMENT ON COLUMN watchlist.scheduled_date IS '시행예정일';
COMMENT ON COLUMN watchlist.last_serial_no IS '마지막 확인 일련번호';
COMMENT ON COLUMN watchlist.last_checked_at IS '마지막 확인 일시';

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    law_id VARCHAR(50) NOT NULL,
    old_serial_no VARCHAR(50),
    new_serial_no VARCHAR(50) NOT NULL,
    promulgation_no VARCHAR(100),
    revision_type VARCHAR(50),
    revision_reason TEXT,
    unchanged_clauses JSONB,
    comparison_available BOOLEAN NOT NULL DEFAULT TRUE,
    enforce_date DATE,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON COLUMN change_log.revision_reason IS '법제처 공식 개정이유(제개정이유) — LLM팀이 추론할 필요 없게 원문 그대로 보관';
COMMENT ON COLUMN change_log.unchanged_clauses IS '{"제34조": ["①","②","③"]} 형태 — 이번 개정에서 안 바뀐 항(항제개정유형 필드 기준, 법령만). LLM팀이 "이 조문의 나머지 항은 현행 유지"임을 추론하지 않아도 되게 함';
COMMENT ON COLUMN change_log.comparison_available IS '신구법 대비 가능 여부. FALSE면 article_diff에 이 (law_id,new_serial_no)의 행이 하나도 없다는 뜻과 정확히 같지만, 그 부재만으로는 "신구법없음"과 "대비했는데 실제로 0건 변경"을 구분할 수 없어(둘 다 article_diff 0행) 별도 컬럼으로 명시적으로 남긴다 — contract/export.py의 no_comparison 리포팅이 이 컬럼에 의존함';

CREATE TABLE IF NOT EXISTS article_diff (
    law_id VARCHAR(50) NOT NULL,
    law_serial_no VARCHAR(50) NOT NULL,
    article_code VARCHAR(50) NOT NULL,
    article_label VARCHAR(100),
    clause_no VARCHAR(50) NOT NULL DEFAULT '',
    item_label VARCHAR(50) NOT NULL DEFAULT '',
    subitem_label VARCHAR(50) NOT NULL DEFAULT '',
    enforce_date DATE,
    change_type VARCHAR(50),
    old_text TEXT,
    new_text TEXT,
    match_status VARCHAR(50),
    match_detail JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_article_diff UNIQUE (law_id, law_serial_no, article_code, clause_no, item_label, subitem_label, enforce_date)
);

CREATE TABLE IF NOT EXISTS law_summary (
    law_id VARCHAR(50) NOT NULL,
    new_serial_no VARCHAR(50) NOT NULL,
    law_name VARCHAR(255) NOT NULL,
    law_type VARCHAR(50),
    enforce_date DATE,
    revision_type VARCHAR(50),
    source_url TEXT,
    headline TEXT,
    overview TEXT,
    body TEXT,
    caveats JSONB,
    article_summaries JSONB,
    mappings JSONB,
    verifier_issues JSONB,
    llm_provider VARCHAR(50),
    llm_model VARCHAR(100),
    batch_date DATE,
    source_file VARCHAR(255),
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (law_id, new_serial_no)
);

CREATE INDEX IF NOT EXISTS idx_law_summary_batch ON law_summary (batch_date);

COMMENT ON COLUMN law_summary.law_id IS '법령/행정규칙 ID';
COMMENT ON COLUMN law_summary.new_serial_no IS '요약 대상 개정분의 일련번호 — change_log.new_serial_no 와 같은 값';
COMMENT ON COLUMN law_summary.law_name IS '요약 시점의 법령명 — watchlist 가 나중에 제명 변경으로 갱신되어도 "이 요약이 무엇에 대한 것이었나"가 남게 사본으로 보관';
COMMENT ON COLUMN law_summary.law_type IS '법률, 시행령, 행정규칙 등';
COMMENT ON COLUMN law_summary.enforce_date IS '시행일';
COMMENT ON COLUMN law_summary.revision_type IS '일부개정, 타법개정 등';
COMMENT ON COLUMN law_summary.source_url IS '법제처 원문 링크 — 담당자가 요약을 못 믿을 때 바로 원문으로 갈 수 있게';
COMMENT ON COLUMN law_summary.headline IS 'LLM 이 쓴 한 줄 요약(목록 화면용)';
COMMENT ON COLUMN law_summary.overview IS 'LLM 이 쓴 개정 취지 문단. 이 컬럼만이 순수 LLM 생성물이며, 사실 검증(summarizer/verifier.py)의 대상';
COMMENT ON COLUMN law_summary.body IS 'overview + 코드가 붙인 조문별 변경 목록. 보고서 본문과 같은 내용';
COMMENT ON COLUMN law_summary.caveats IS '["조문 요약 실패: 제3조①", ...] — 이 요약을 읽는 사람이 알아야 할 신뢰도 경고. 코드가 판정 상태에서 결정론적으로 만든 것이며 LLM 이 쓴 문장이 아니다';
COMMENT ON COLUMN law_summary.article_summaries IS '조문별 요약 전체(원문 old/new 포함). 보고서의 조문별 변경표가 이것으로 만들어진다';
COMMENT ON COLUMN law_summary.mappings IS '조문별 구↔신 위치 대응 판정. "①이 ②로 이동"이라고 쓴 근거이자, 나중에 판정이 의심스러울 때 추적하는 기록';
COMMENT ON COLUMN law_summary.verifier_issues IS '사실 대조 검증(코드)이 찾은 문제. severity=high 는 요약이 원문과 다르다는 뜻이라 담당자가 원문을 봐야 한다';
COMMENT ON COLUMN law_summary.llm_provider IS '요약을 만든 프로바이더 (openai, anthropic 등)';
COMMENT ON COLUMN law_summary.llm_model IS '요약을 만든 모델. 모델을 바꾼 뒤 품질이 달라졌을 때 어느 판본인지 구분하려면 반드시 필요하다';
COMMENT ON COLUMN law_summary.batch_date IS '이 요약을 만든 배치의 기준일';
COMMENT ON COLUMN law_summary.source_file IS '요약의 입력이 된 계약 JSON 파일명 — 재현·추적용';
COMMENT ON COLUMN law_summary.error IS 'LLM 호출 실패 사유. 실패를 조용히 빼지 않는다는 계약 원칙과 같다 — 실패한 요약도 행으로 남긴다';

COMMENT ON TABLE law_summary IS 'LLM 요약 결과. 키가 (law_id, new_serial_no)인 이유: 요약의 정체성은 "어느 법의 어느 개정분에 대한 요약인가"이지 "언제 만들었나"가 아니다. 같은 개정분을 다시 요약하면 덮어쓴다 — 요약은 계약 JSON에서 언제든 다시 만들 수 있는 파생물이라 판본을 쌓아두면 "어느 게 맞는 요약인가"를 매번 따져야 하고, 실제로 참조되는 것은 항상 최신 1건이기 때문이다. 언제/무엇으로 만들었는지는 batch_date/llm_model 컬럼에 남는다. 이 설계는 article_diff(재계산 시 해당 범위를 지우고 다시 채움)와 같은 원칙이다.';

-- ---------------------------------------------------------------------------
-- MySQL의 "TIMESTAMP ... ON UPDATE CURRENT_TIMESTAMP"에 해당하는 자동 갱신.
-- Postgres에는 같은 컬럼 속성이 없어 트리거로 구현한다.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION touch_db_timestamp() RETURNS TRIGGER AS $$
BEGIN
    NEW.db_timestamp = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_touch ON documents;
CREATE TRIGGER trg_documents_touch BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION touch_db_timestamp();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_law_summary_touch ON law_summary;
CREATE TRIGGER trg_law_summary_touch BEFORE UPDATE ON law_summary
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
