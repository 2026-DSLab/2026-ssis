"""달력 주(월요일~일요일) 경계 계산.

주간 배치(scripts/run_weekly.py)와 주간 리포트 화면(webapp/app.py)이 반드시
같은 창을 봐야 하므로, 그 창을 정하는 자리를 여기 하나로 둠. 한쪽만
고치면 배치가 담은 것과 화면이 보여주는 것이 어긋남.

왜 "지난 한 주"인가: 예전에는 이 화면이 law_summary.batch_date 가 가장 큰
배치를 통째로 보여줬음. 그러면 배치를 언제 돌렸느냐에 따라 창이 매번
달라져서, 3일 전 배치가 계속 그 주로 남거나(실측 역전 "1건인데 최근 5일은
3건"이 여기서 나왔음) 같은 주 안에서도 볼 때마다 건수가 바뀌었음. 달력
주로 자르면 창이 배치 실행 시각과 무관하게 고정되고, 이미 끝난 주라
건수가 더 늘지도 않음.

화면 이름이 한때 "이번 주"였는데, 보여주는 건 지난 한 주라 이름과 창이
어긋났음("이번 주보다 최근 5일이 많다"는 문의가 실제로 들어왔음). 지금은
"주간 리포트"임 — 이 함수가 정하는 창이 곧 그 이름의 뜻임.

기준 날짜는 시행일(enforce_date)임 — 배치가 계약 산출물을 조립할 때
쓰는 창(ArticleDiffRepo.fetch_period)과 같은 기준이라, 배치가 담은 것과
화면이 고르는 것이 같은 뜻이 됨.
"""

from __future__ import annotations

from datetime import date, timedelta


def last_full_week(today: date | None = None) -> tuple[date, date]:
    """오늘이 속한 주의 직전 달력 주(월요일, 일요일) 한 쌍.

    월요일 06:00 에 도는 주간 배치가 "지난주에 무슨 일이 있었는지"
    정리하는 것과 같은 창임 — 월요일에 열어도 이미 채워져 있고, 주
    후반에 열어도 같은 창을 봄.

    today 는 테스트에서 특정 요일을 고정하려고 넘기는 값이며, 실 호출은
    인자 없이 씀.
    """
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)
    return start, start + timedelta(days=6)
