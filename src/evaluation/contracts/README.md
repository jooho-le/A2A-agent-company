# 공통 보고서 규격

이 폴더는 에이전트 보고서와 실행 근거의 JSON 구조를 검사하는 스키마를 담습니다.
`reporting.py`의 `validate_contract()`가 파일을 읽어 필수 필드, 자료형, 허용값을 검증합니다.
네트워크에서 스키마를 내려받지 않고 패키지에 포함된 파일만 사용합니다.

## 파일별 역할

| 파일 | 검사 대상 |
|---|---|
| `execution_manifest.schema.json` | 검사 대상 코드와 실행환경의 식별 정보 |
| `qa_report.schema.json` | 기능 검사 보고서의 공통 정보와 테스트별 결과 |
| `security_report.schema.json` | 보안 검사 보고서의 요구사항별 결과와 발견사항 |
| `tool_execution_evidence.schema.json` | 도구 실행의 식별 정보, 증거 참조, 시도 기록 |
| `independent_evaluation.schema.json` | 4번이 만드는 독립 평가 보고서(`INDEPENDENT_EVALUATION`). 팀 공통 규격이 아니라 평가 모듈 전용이며 `build_evaluation_report()`가 반환 전에 검증 |
| `__init__.py` | 파이썬에서 이 폴더를 패키지 자원으로 읽기 위한 모듈 |

## JSON 파일에 주석이 없는 이유

일반 JSON 문법은 `#`, `//`, `/* ... */` 형태의 주석을 지원하지 않습니다.
이러한 주석을 넣으면 스키마를 읽는 `json.loads()`에서 오류가 발생합니다.

JSON Schema에는 설명용 `description`이나 `$comment` 속성을 넣을 수 있지만,
이 폴더의 파일은 팀 공통 규격의 사본이므로 원본과 동일하게 유지합니다.
파일 안에 설명 속성을 추가하는 대신 이 문서에서 필드와 검사 조건을 설명합니다.
원본에 이미 있는 영문 제목·설명 속성도 사본의 일치 여부를 유지하기 위해 그대로 둡니다.

## 코드·환경 정보

`execution_manifest.schema.json`은 다음 정보를 요구합니다.

| 필드 | 의미 |
|---|---|
| `repositoryId` | 검사 대상 저장소 식별자 |
| `codeVersion` | 코드 후보 버전. 최초 1부터 최대 4까지 허용 |
| `projectArtifactId` | 검사 대상 소스 산출물의 프로젝트 식별자 |
| `gitObjectFormat` | Git 객체 해시 형식. `sha1` 또는 `sha256` |
| `commitHash` | 검사 대상 커밋의 전체 해시 |
| `treeHash` | 해당 코드 파일 구성을 식별하는 트리 해시 |
| `snapshotSha256` | 고정된 코드 묶음의 SHA-256 해시 |
| `containerImageDigest` | 실행 컨테이너 이미지의 해시 |
| `dependencyLockHash` | 의존성 잠금 정보의 해시 |

Git 해시는 지정한 형식에 따라 40자리 또는 64자리여야 합니다.
스냅샷 해시는 소문자 16진수 64자리이며, 컨테이너·의존성 해시는 `sha256:` 접두사를 사용합니다.
형식이 맞는다는 사실만으로 실제 파일이나 실행환경이 일치한다고 보장하지는 않습니다.

## 기능·보안 보고서

두 보고서는 공통으로 다음 정보를 사용합니다.

| 필드 묶음 | 의미 |
|---|---|
| `artifactId`, `artifactType`, `artifactVersion`, `previousArtifactId` | 보고서 산출물의 식별·종류·버전·이전 버전 연결 |
| `runId`, `workflowStepId` | 전체 개발 실행과 해당 작업 단계 |
| `a2aTaskId`, `a2aArtifactId` | 실제 에이전트 작업과 그 작업의 산출물 |
| `createdBy`, `createdAt` | 보고서 생성 역할과 시각 |
| `requirementIds` | 보고서가 다루는 요구사항 식별자 목록 |
| `codeVersion`, `executionManifest` | 검사 대상 코드 버전과 실행환경 |
| `artifactUri` | 보고서 접근 위치. 선택 필드 |

기능 보고서는 `tests`에 검사 식별자, 요구사항 식별자, 제목, 판정 결과 등을 담습니다.
보안 보고서는 `requirementResults`에 요구사항별 결과를, `findings`에 보안 발견사항을 담습니다.
검사 결과는 `PASS`, `FAIL`, `UNVERIFIED`로 구분합니다.
기대값·실제값·검사 위치와 도구 실행 근거도 각 보고서 규격에 따라 기록할 수 있습니다.

## 도구 실행 근거

| 필드 | 의미 |
|---|---|
| `toolName` | 실행한 도구 이름 |
| `executionId` | 해당 논리 도구 실행의 식별자 |
| `executionManifest` | 실행 대상 코드와 환경 |
| `evidenceRef` | 실행 근거의 접근 위치 |
| `attempts` | 최초 호출과 재시도 기록. 1~3개 허용 |

각 시도에는 `attempt`, `outcome`, `evidenceRef`가 필요합니다.
시도 번호는 0~2이며, 오류 종류·재시도 안전 여부·소요 시간은 추가로 기록할 수 있습니다.

스키마는 필드와 값의 형식을 검사합니다. 시도 번호가 연속되는지, 재시도가 안전한지,
도구가 보고서 역할과 맞는지, 실행 정보가 같은지는 `reporting.py`가 추가로 검사합니다.
도구 실행의 통과는 제품 기능의 통과와 다릅니다. 테스트가 정상 실행되어 결함을 발견하면
도구 실행은 통과이고 해당 제품 검사는 실패일 수 있습니다.

## 독립 평가와의 관계

이 스키마들은 `reporting.py`가 생성하는 에이전트 호환 보고서에 적용됩니다.
`independent.py`의 `INDEPENDENT_EVALUATION` 보고서 전용 스키마는 아닙니다.
독립 평가 보고서는 해당 모듈에서 입력값과 요구사항 범위 등을 검사합니다.

스키마 검사만으로 실제 회원가입 기능, 증거의 진위, 산출물의 등록 여부를 검증할 수는 없습니다.

## 원본과 갱신 기준

- 원본 저장소: `jooho-le/A2A-agent-company`
- 기준 브랜치와 커밋: `dev`, `2b4fc6037e1a7d8296040c01404c56e8a2a0f22e`
- 원본 위치: 저장소 루트의 `schemas/project/`

이 파일들은 위 커밋에서 수정 없이 복사한 호환성 검사용 사본입니다.
팀 공통 규격의 별도 원본으로 취급하지 않습니다.
공통 규격을 갱신할 때는 사본과 보고서 변환 코드, 호환성 테스트를 함께 확인해야 합니다.
