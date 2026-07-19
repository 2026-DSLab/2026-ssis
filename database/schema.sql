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