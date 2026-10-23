# 평가 모듈의 여러 파일이 함께 쓰는 작은 상수와 도우미 함수를 모아 둔 파일이다.
# 같은 코드를 파일마다 복사해 두면 한쪽만 고치는 실수가 생기기 때문에, 여기 한 곳에만 둔다.
from datetime import datetime  # 날짜·시각을 다루는 파이썬 기본 도구

# 비교하는 두 가지 구조의 이름이다. Run Configuration과 평가 보고서의 architecture 값으로 쓴다.
#   SINGLE_AGENT = 에이전트 1개가 모든 일을 하는 구조
#   MULTI_AGENT  = 역할별 에이전트 4개(기획·개발·QA·보안)가 나눠서 하는 구조
# 괄호 ( ) 로 묶은 값은 "튜플"이라고 하며, 한번 만들면 바꿀 수 없는 목록이다.
ARCHITECTURES = ('SINGLE_AGENT', 'MULTI_AGENT')


def aware_time(value, name):
    # "2026-10-14T12:00:00+09:00" 같은 글자를 파이썬 시각(datetime)으로 바꿔 돌려준다.
    # 끝의 "+09:00"은 시간대(한국은 세계 표준시보다 9시간 빠름)이다.
    # 시간대가 없으면 서로 다른 나라의 시각을 잘못 비교할 수 있어서 거부한다.
    # ("aware" = 시간대를 알고 있는 시각이라는 뜻)
    # name은 오류가 났을 때 "어떤 값이 잘못됐는지" 알려 주려고 받는 이름이다.
    if not isinstance(value, str):
        # 글자가 아니면(예: 숫자, None) 바로 오류를 낸다.
        raise ValueError(f'{name} must be an ISO timestamp')
    # 파이썬 3.10의 fromisoformat은 끝이 "Z"(세계 표준시를 뜻하는 표기)인 글자를 읽지 못한다.
    # 그래서 "Z"를 같은 뜻의 "+00:00"으로 바꾼 뒤에 읽는다.
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        # utcoffset()이 None이면 시간대 정보가 없다는 뜻이다.
        raise ValueError(f'{name} must include a timezone')
    return parsed
