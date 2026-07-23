"""기존 MySQL DB를 현재 코드의 스키마로 안전하게 갱신.

사용법 (프로젝트 루트):
    python scripts\migrate_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lawtrack.config import configure_utf8_console, load_settings  # noqa: E402
from lawtrack.db.conn import Database  # noqa: E402
from lawtrack.db.migrate import apply_migrations  # noqa: E402


def main() -> int:
    configure_utf8_console()
    settings = load_settings()
    db = Database(settings.db, pool_size=1)

    print("DB 마이그레이션 확인 중...")
    applied = apply_migrations(db)
    if applied:
        for item in applied:
            print(f"  적용: {item}")
    else:
        print("  이미 최신 상태입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
