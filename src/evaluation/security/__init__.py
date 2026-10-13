# SCN-001(회원가입 시나리오)의 "보안 판정" 패키지이다. (D1 작업)
# 폴더 이름만으로 `from evaluation.security import assess`처럼 가져다 쓸 수 있도록, 밖에서 쓸 두 함수를 여기서 내보낸다.
#   assess   : 팀 시스템(dev)의 승인 절차가 호출하는 진입 함수(오류가 나면 None을 돌려줌)
#   evaluate : 같은 판정을 하되 오류를 숨기지 않는 함수(테스트·디버깅용)
from .assess import assess, evaluate

# __all__은 "이 패키지에서 밖으로 공개하는 이름"의 목록이다.
__all__ = ['assess', 'evaluate']
