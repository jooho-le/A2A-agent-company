# A2A 기반 다중 AI 에이전트 협업형 가상 소프트웨어 회사

4번 담당 강석민의 Evaluation / QA / Security 개발 공간입니다.
첫 대상은 회원가입 MVP(SCN-001)이며, 현재 공통 보고서 연결 기준은 dev `2b4fc60`입니다.
평가 명세 버전은 0.2이며, 평가 모듈은 공통 프로젝트 패키지에 포함됩니다.

## 현재 구현

- 필수·선택 검사 집계, 누락·중복·증거 ID 확인
- 동일 코드·환경의 Manifest 비교와 공통 camelCase 입출력
- QA_REPORT / SECURITY_REPORT 데이터 생성, 고정된 dev JSON Schema 검증
- 내부 UUID → 외부 testId·요구사항 ID의 명시적 대응
- ERROR/NOT_RUN → UNVERIFIED 변환과 원래 상태·사유 보존
- Tool 실행 근거의 역할·Manifest·시도 순서·안전 재시도·제품 결과 정합성 검사
- 공통 프로젝트 환경에서 실행하는 평가 모듈과 합성 보고서 데모

실제 MCP 실행, Registry/증거 저장소 조회, 파일 해시·증거 진위 검증, A2A 전송,
가입 API/DB 검사와 비교 실험은 아직 연결하지 않았습니다. 최종 SUCCESS는 Orchestrator 책임입니다.

## 문서

- [환경 설정](../../docs/evaluation/environment_setup.md): PyCharm·설치·실행·문제 확인
- [명세서](../../docs/evaluation/evaluation_spec.md): 평가 기준·책임·승인 대기 정책
- [개발진행사항](../../docs/evaluation/development_progress.md): 구현 범위·변경·검증·다음 작업

## 실행

저장소 루트에서 공통 프로젝트 환경을 한 번 설치합니다.
Python 3.10 이상과 uv를 사용하며 PyCharm 인터프리터는 루트 `.venv/bin/python`입니다.

```bash
uv sync --frozen
.venv/bin/python -m pytest
.venv/bin/python -m evaluation
.venv/bin/python -m evaluation.report_demo
.venv/bin/python scripts/check_dev_reports.py .venv/bin/python
```

루트 `pyproject.toml`과 `uv.lock`으로 의존성을 함께 관리합니다. 별도 평가 설치는 필요 없습니다.

## 보고서 변환 사용 조건

`evaluation.reporting.build_report`는 이미 정규화된 결과를 받습니다.
실제 MCP 응답을 읽는 실행 어댑터나 HTTP 엔드포인트는 아닙니다.

- `context`: 실제 등록된 Run/Step/Artifact ID, A2A ID, 보고서 버전·생성 시각·요구사항 목록
- `expected`, `observed`: 신뢰된 실행 기준과 보고서 대상 Manifest. 불일치 시 전송용 보고서 생성 거부
- `plan`, `results`: 고정 필수 검사 계획과 개별 결과
- `bindings`: 각 내부 검사 UUID에 대응하는 외부 testId·requirementId·제목
- `tool_evidence`: 실제 실행기가 제공한 검사별 근거. PASS/FAIL에는 필수
- `findings`: Security 결과 목록. 발견사항이 없어도 빈 목록을 명시적으로 제공

원문·증거는 호출자가 마스킹해야 합니다. 이 함수는 비밀정보 제거·ID 등록 여부·증거 내용을 검증하지 않습니다.
생성 결과는 A2A Artifact의 JSON data payload이며 실제 A2A 포장·Task 연결은 후속 작업입니다.

N/A는 공통 계약 확정 전 변환을 거부합니다. 선택 검사는 필수 보고서에 섞지 않습니다.
Security는 현재 요구사항당 사전 집계된 결과 1개만 지원하며, 여러 검사·증거를 조용히 합치지 않습니다.
Tool PASS는 도구 실행 완료를 뜻하므로 제품 결과 FAIL과 함께 올 수 있습니다.

## 검증과 구조

자체 테스트 91개 통과. dev의 실제 QA/Security 모델이 합성 보고서 8개를 수용한 것을 확인했습니다.
새 브랜치의 동일 환경에서 dev 테스트 172개와 평가 테스트 91개, 총 263개 통과를 확인했습니다.
이는 A2A/서비스 통합 테스트가 아닙니다.

```text
src/evaluation/aggregation.py       내부 검사 집계
src/evaluation/manifest.py          Manifest 형식·일치·입출력
src/evaluation/reporting.py         공통 보고서 데이터 변환과 검증
src/evaluation/report_demo.py       mock 보고서 예시
src/evaluation/contracts/           dev 고정 커밋의 공통 스키마 사본
pyproject.toml                  공통 패키지·개발 의존성
uv.lock                         공통 의존성 고정
tests/evaluation/               평가 자체 테스트
scripts/check_dev_reports.py   공통 Python으로 보고서 모델 호환 확인
```

공통 스키마 사본은 원본을 대체하지 않습니다. dev 계약이 바뀌면 사본과 호환 검사를 함께 갱신합니다.
`seokmin` 브랜치에서 개발하며 환경·DB·작업공간·비밀값·빌드 산출물은 Git에서 제외합니다.
