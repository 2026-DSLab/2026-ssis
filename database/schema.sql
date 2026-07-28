CREATE DATABASE IF NOT EXISTS law_tracking_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE law_tracking_db;

CREATE TABLE IF NOT EXISTS laws (
    law_name VARCHAR(255) NOT NULL COMMENT '법령명',
    law_id VARCHAR(50) NOT NULL COMMENT '법령 ID',
    law_serial_no VARCHAR(50) NOT NULL COMMENT '법령 일련번호',
    law_full_text JSON NOT NULL COMMENT '법령 전문 JSON — 법제처 API 원본 그대로(진실의 원천, 재파싱 가능하도록 절대 가공하지 않음)',
    law_articles_parsed JSON COMMENT '조/항/호/목 구조로 파싱한 결과(parse_articles 출력 캐시). law_full_text에서 파생된 값이므로 파서 로직이 바뀌면 재생성 대상 — law_full_text가 진실의 원천, 이 컬럼은 조회 편의용',
    db_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'DB 추가 또는 수정 시각',
    PRIMARY KEY (law_id, law_serial_no)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS administrative_rules (
    administrative_rule_name VARCHAR(255) NOT NULL COMMENT '행정규칙명',
    administrative_rule_id VARCHAR(50) NOT NULL COMMENT '행정규칙 ID',
    administrative_rule_serial_no VARCHAR(50) NOT NULL COMMENT '행정규칙 일련번호',
    administrative_rule_full_text JSON NOT NULL COMMENT '행정규칙 전문 JSON — 법제처 API 원본 그대로(진실의 원천)',
    administrative_rule_articles_parsed JSON COMMENT '조/항/호/목 위치별로 파싱한 결과(parse_admrul_units 출력 캐시, 행정규칙은 원문이 평문이라 법령과 달리 위치+텍스트의 평평한 목록 형태). administrative_rule_full_text에서 파생된 값 — 파서 로직이 바뀌면 재생성 대상',
    db_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'DB 추가 또는 수정 시각',
    PRIMARY KEY (
        administrative_rule_id,
        administrative_rule_serial_no
    )
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS watchlist (
    law_id VARCHAR(50) PRIMARY KEY COMMENT '법령/행정규칙 ID',
    law_type VARCHAR(50) NOT NULL COMMENT '법령종류 (법률, 시행령 등)',
    official_name VARCHAR(255) NOT NULL COMMENT '법령명',
    internal_name VARCHAR(255) COMMENT '내부관리명',
    previous_names JSON COMMENT '이전 제명 이력',
    dept_codes VARCHAR(255) COMMENT '소관부처 코드(콤마구분)',
    status VARCHAR(50) NOT NULL COMMENT '상태 (현행, 시행전 등)',
    successor_law_id VARCHAR(50) COMMENT '후속(통합) 법령 ID',
    scheduled_date DATE COMMENT '시행예정일',
    last_serial_no VARCHAR(50) COMMENT '마지막 확인 일련번호',
    last_checked_at DATETIME COMMENT '마지막 확인 일시'
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS change_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    law_id VARCHAR(50) NOT NULL,
    old_serial_no VARCHAR(50),
    new_serial_no VARCHAR(50) NOT NULL,
    promulgation_no VARCHAR(100),
    revision_type VARCHAR(50),
    revision_reason TEXT COMMENT '법제처 공식 개정이유(제개정이유) — LLM팀이 추론할 필요 없게 원문 그대로 보관',
    unchanged_clauses JSON COMMENT '{"제34조": ["①","②","③"]} 형태 — 이번 개정에서 안 바뀐 항(항제개정유형 필드 기준, 법령만). LLM팀이 "이 조문의 나머지 항은 현행 유지"임을 추론하지 않아도 되게 함',
    comparison_available BOOLEAN NOT NULL DEFAULT TRUE COMMENT '신구법 대비 가능 여부. FALSE면 article_diff에 이 (law_id,new_serial_no)의 행이 하나도 없다는 뜻과 정확히 같지만, 그 부재만으로는 "신구법없음"과 "대비했는데 실제로 0건 변경"을 구분할 수 없어(둘 다 article_diff 0행) 별도 컬럼으로 명시적으로 남긴다 — contract/export.py의 no_comparison 리포팅이 이 컬럼에 의존함',
    enforce_date DATE,
    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

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
    match_detail JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_article_diff (law_id, law_serial_no, article_code, clause_no, item_label, subitem_label, enforce_date)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS law_summary (
    law_id VARCHAR(50) NOT NULL COMMENT '법령/행정규칙 ID',
    new_serial_no VARCHAR(50) NOT NULL COMMENT '요약 대상 개정분의 일련번호 — change_log.new_serial_no 와 같은 값',
    law_name VARCHAR(255) NOT NULL COMMENT '요약 시점의 법령명 — watchlist 가 나중에 제명 변경으로 갱신되어도 "이 요약이 무엇에 대한 것이었나"가 남게 사본으로 보관',
    law_type VARCHAR(50) COMMENT '법률, 시행령, 행정규칙 등',
    enforce_date DATE COMMENT '시행일',
    revision_type VARCHAR(50) COMMENT '일부개정, 타법개정 등',
    source_url TEXT COMMENT '법제처 원문 링크 — 담당자가 요약을 못 믿을 때 바로 원문으로 갈 수 있게',
    headline TEXT COMMENT 'LLM 이 쓴 한 줄 요약(목록 화면용)',
    overview MEDIUMTEXT COMMENT 'LLM 이 쓴 개정 취지 문단. 이 컬럼만이 순수 LLM 생성물이며, 사실 검증(summarizer/verifier.py)의 대상',
    body MEDIUMTEXT COMMENT 'overview + 코드가 붙인 조문별 변경 목록. 보고서 본문과 같은 내용',
    caveats JSON COMMENT '["조문 요약 실패: 제3조①", ...] — 이 요약을 읽는 사람이 알아야 할 신뢰도 경고. 코드가 판정 상태에서 결정론적으로 만든 것이며 LLM 이 쓴 문장이 아니다',
    article_summaries JSON COMMENT '조문별 요약 전체(원문 old/new 포함). 보고서의 조문별 변경표가 이것으로 만들어진다',
    mappings JSON COMMENT '조문별 구↔신 위치 대응 판정. "①이 ②로 이동"이라고 쓴 근거이자, 나중에 판정이 의심스러울 때 추적하는 기록',
    verifier_issues JSON COMMENT '사실 대조 검증(코드)이 찾은 문제. severity=high 는 요약이 원문과 다르다는 뜻이라 담당자가 원문을 봐야 한다',
    llm_provider VARCHAR(50) COMMENT '요약을 만든 프로바이더 (openai, anthropic 등)',
    llm_model VARCHAR(100) COMMENT '요약을 만든 모델. 모델을 바꾼 뒤 품질이 달라졌을 때 어느 판본인지 구분하려면 반드시 필요하다',
    batch_date DATE COMMENT '이 요약을 만든 배치의 기준일',
    source_file VARCHAR(255) COMMENT '요약의 입력이 된 계약 JSON 파일명 — 재현·추적용',
    error TEXT COMMENT 'LLM 호출 실패 사유. 실패를 조용히 빼지 않는다는 계약 원칙과 같다 — 실패한 요약도 행으로 남긴다',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (law_id, new_serial_no),
    KEY idx_law_summary_batch (batch_date)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
COMMENT = 'LLM 요약 결과. 키가 (law_id, new_serial_no)인 이유: 요약의 정체성은 "어느 법의 어느 개정분에 대한 요약인가"이지 "언제 만들었나"가 아니다. 같은 개정분을 다시 요약하면 덮어쓴다 — 요약은 계약 JSON에서 언제든 다시 만들 수 있는 파생물이라 판본을 쌓아두면 "어느 게 맞는 요약인가"를 매번 따져야 하고, 실제로 참조되는 것은 항상 최신 1건이기 때문이다. 언제/무엇으로 만들었는지는 batch_date/llm_model 컬럼에 남는다. 이 설계는 article_diff(재계산 시 해당 범위를 지우고 다시 채움)와 같은 원칙이다.';
