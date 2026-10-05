# 평가 모듈 (4번 Evaluation)

4번 담당 강석민의 평가 모듈입니다. 첫 대상은 회원가입 MVP(SCN-001)이고, 공통 계약 기준은 dev `2b4fc60`입니다.
평가 명세는 0.3이며, 이 모듈은 루트 `pyproject.toml`의 공통 패키지에 포함됩니다.

## 이 모듈이 하는 일

개발 순환(Planner → Developer → QA/Security Agent → 수정)이 끝난 뒤 나온 결과물을
보호 테스트로 채점하고, 그 결과를 믿을 수 있는지 검사해 보고서로 만듭니다.
Multi-Agent와 Single-Agent 결과물을 같은 기준으로 채점해 비교하는 것이 목적입니다.
채점 결과는 Agent에게 돌려주지 않습니다(명세 4.1절, 팀 확정 대기).

## 파일 구성

```text
src/evaluation/
  aggregation.py    검사 계획과 결과를 맞춰 상태별 개수·통과율·실패/미검증 목록 계산
  manifest.py       검사 대상 코드·환경(Manifest) 형식 검사, 기준과 같을 때만 집계
  independent.py    독립 평가 보고서(INDEPENDENT_EVALUATION) 생성  ← 기본 경로
  reporting.py      1번이 받는 QA_REPORT / SECURITY_REPORT 변환      ← 에이전트 호환 보조 경로
  report_demo.py    공통 보고서 합성 예시 8개
  __main__.py       집계 합성 예시
  contracts/        dev 2b4fc60 공통 스키마 사본 (설명은 contracts/README.md)
tests/evaluation/   평가 모듈 자체 테스트 113개
checks/             Python·가상환경·HTTP 라이브러리 확인 2개 (기본 pytest 대상 아님)
scripts/check_dev_reports.py   dev 보고서 모델이 합성 보고서를 받는지 확인
```

각 파일 맨 위와 함수마다 짧은 한국어 설명이 있습니다. 처음 읽는다면
`aggregation.py` → `manifest.py` → `independent.py` 순서를 권합니다.

## 실행

저장소 루트에서 공통 환경을 한 번 설치합니다. Python 3.10 이상과 uv를 사용하고,
PyCharm 인터프리터는 루트 `.venv/bin/python`입니다.

```bash
uv sync --frozen
.venv/bin/python -m pytest                     # 전체(평가 + dev)
.venv/bin/python -m pytest tests/evaluation    # 평가 테스트만
.venv/bin/python -m pytest checks              # 가상환경·라이브러리 확인
.venv/bin/python -m evaluation                 # 집계 합성 예시
.venv/bin/python -m evaluation.report_demo     # 보고서 합성 예시
.venv/bin/python scripts/check_dev_reports.py .venv/bin/python
```

## 기본 경로: 독립 평가 보고서

`evaluation.independent.build_evaluation_report`를 사용합니다. A2A Task ID가 필요 없습니다.

| 인자 | 내용 |
|---|---|
| `evaluation_id` | 이번 채점 실행의 UUIDv4. `source_run_id`와 달라야 함 |
| `source_run_id` | 채점 대상 개발 실행(Run)의 UUIDv4 |
| `architecture` | `SINGLE_AGENT` 또는 `MULTI_AGENT` |
| `suite_id`, `suite_version`, `suite_sha256` | 보호 테스트 묶음의 이름·버전·내용 해시(호출자가 계산) |
| `created_at` | 시간대 포함 ISO 시각. 예: `2026-10-06T12:00:00+09:00` |
| `required_requirement_ids` | 이번 평가가 다뤄야 하는 요구사항 UUID 목록 |
| `expected`, `observed` | 기준 Manifest와 결과가 실제로 나온 Manifest |
| `plan`, `results` | `CheckPlan`·`CheckResult` 목록 |
| `bindings` | `{검사 UUID: EvaluationBinding(requirement_id, perspective, title)}` |
| `sensitive_values` | 진단 설명에서 가릴 문자열(테스트용 비밀번호, 저장된 해시 등) |

출력에는 검사별 결과·증거·사유, 요구사항별 요약, QA/SECURITY 관점별 요약,
검사 통과율과 요구사항 통과율이 따로 들어갑니다. 한 요구사항에 검사를 여러 개 연결할 수 있고,
검사가 없는 관점은 `null`입니다. `evaluationVerdict`는 Orchestrator의 `finalVerdict`를 덮어쓰지 않습니다.
필수 검사에 실패와 미검증이 섞이면 요약은 `UNVERIFIED`이고, 확인된 실패 ID는 따로 남습니다.
실패·미검증 ID 목록은 필수만 담고, 선택 검사는 `optionalConfirmedFailureIds`·`optionalUnverifiedTestIds`에 담깁니다.
보고서 예시는 명세 11.3절에 있습니다.

`CheckResult`에는 `expected_result`, `actual_result`, `reason`, `normalized_location`, `error_code`를
선택적으로 넣을 수 있습니다. 이 진단 정보는 두 보고서 경로 모두에 전달됩니다.

## 보조 경로: 에이전트 호환 보고서

`evaluation.reporting.build_report`는 1번 Orchestrator가 받는 dev 형식의 QA_REPORT / SECURITY_REPORT를 만듭니다.
실제 A2A Task·Artifact ID가 담긴 `context`, 필수 검사만의 계획, `TestBinding` 대응표,
PASS·FAIL 판정마다 도구 실행 근거(`tool_evidence`)가 필요합니다. SECURITY는 `findings`를 빈 목록이라도
명시해야 하고, 요구사항당 결과 1개만 받습니다. NOT_APPLICABLE은 거부합니다.
도구 실행 PASS는 "도구가 끝까지 돌았다"는 뜻이라 제품 검사 FAIL과 함께 올 수 있습니다.

## 하지 않는 일과 주의사항

- 테스트·MCP 도구를 실행하거나 A2A로 전송하지 않습니다.
- 증거 URI를 열거나 ID가 등록됐는지 조회하지 않습니다. suite 해시도 직접 계산하지 않습니다.
- 민감값 치환은 알려 준 문자열을 진단 필드에서만 바꿉니다. 제목·suite 이름·findings·toolEvidence·context는
  호출자가 미리 정리해야 합니다.
- 알려진 제약과 결정이 필요한 항목은 [개발진행사항](../../docs/evaluation/development_progress.md) 4절에 있습니다.

## 문서

- [환경 설정](../../docs/evaluation/environment_setup.md): 설치·실행·문제 확인
- [명세서](../../docs/evaluation/evaluation_spec.md): 평가 기준·책임·협의 항목
- [개발진행사항](../../docs/evaluation/development_progress.md): 현재 상태·검토 결과·다음 작업

공통 스키마 사본은 원본을 대체하지 않습니다. dev 계약이 바뀌면 사본과 호환 검사를 함께 갱신합니다.
`seokmin` 브랜치에서 개발하며 환경·DB·작업공간·비밀값·빌드 산출물은 Git에서 제외합니다.
