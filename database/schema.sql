CREATE DATABASE IF NOT EXISTS law_tracking_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE law_tracking_db;

CREATE TABLE IF NOT EXISTS laws (
    law_name VARCHAR(255) NOT NULL COMMENT '법령명',
    law_id VARCHAR(50) NOT NULL COMMENT '법령 ID',
    law_serial_no VARCHAR(50) NOT NULL COMMENT '법령 일련번호',
    law_full_text JSON NOT NULL COMMENT '법령 전문 JSON',
    db_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'DB 추가 또는 수정 시각',
    PRIMARY KEY (law_id, law_serial_no)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS administrative_rules (
    administrative_rule_name VARCHAR(255) NOT NULL COMMENT '행정규칙명',
    administrative_rule_id VARCHAR(50) NOT NULL COMMENT '행정규칙 ID',
    administrative_rule_serial_no VARCHAR(50) NOT NULL COMMENT '행정규칙 일련번호',
    administrative_rule_full_text JSON NOT NULL COMMENT '행정규칙 전문 JSON',
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
    UNIQUE KEY (law_id, law_serial_no, article_code, clause_no, item_label, subitem_label)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;