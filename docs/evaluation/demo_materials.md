# 시연 자료 정리 방법 (12월 최종 발표용, 팀장 전달)

작성: 2026-10-14 · 갱신: 2026-10-30 · 4번(강석민). 팀장 카톡 요청: "시연의 실행 로그, 생성 파일, 화면 따로 정리해두기".
실제 Run이 아직 없어서 형식과 도구만 준비한 상태다. 아래 도구는 합성 폴더로 테스트했고 실제 시연 자료로 돌려 본 적은 없다.

## 폴더 구조

한 Run(또는 한 시연 영상)마다 폴더 하나를 만든다.

```text
<자료 폴더>/
  logs/      실행 로그(서버 로그, Orchestrator Trace 내보내기, 도구 실행 로그)
  source/    그 Run이 만든 소스(dev snapshot_export로 꺼낸 최종 Snapshot을 푼 파일). 여러 버전이면 code-1/, code-2/ 하위 폴더
  screens/   화면 캡처·시연 영상(.png, .mp4 등)
  reports/   QA·Security 보고서, 독립 평가 보고서(INDEPENDENT_EVALUATION). experiment score 결과 폴더의 output.json·summary.md를 복사
```

위 네 이름 외의 폴더·파일이 최상위에 있으면 목록 작성을 거부한다(섞여 들어가는 것을 막기 위해). macOS의 `.DS_Store`만 예외로 무시한다.

## 목록 만들기 (`src/evaluation/demo_index.py`)

```python
from evaluation.demo_index import build_demo_index
index = build_demo_index(
    '자료 폴더', run_id='<Run UUID>', created_at='2026-12-01T10:00:00+09:00',
    code_version=2, snapshot_sha256='<Snapshot 해시>',
    sensitive_values=['<시험에 쓴 비밀번호>'],
)
```

- 파일별 경로·크기·SHA-256·형식을 기록하고 내용은 복사하지 않는다. 같은 입력이면 같은 결과다.
- 비어 있는 분류는 `missingCategories`에 나온다. 로그가 없으면 "미노출 PASS"를 만들지 않는다.
- macOS가 폴더마다 저절로 만드는 `.DS_Store`는 시연 자료가 아니므로 목록에서 빼고, 최상위에 있어도 거부하지 않는다.
- 심볼릭 링크, 알 수 없는 최상위 항목, 폴더 밖으로 나가는 경로, 너무 큰 파일(256MiB 초과), **일반 파일이 아닌 것**(이름 있는 파이프(FIFO)·소켓·장치 파일)은 거부한다. 파이프를 열면 쓰는 쪽이 나타날 때까지 무기한 멈추기 때문에, 열기 전에 파일 종류를 확인하고 열 때도 기다리지 않는 방식으로 연다.
- 허용 크기(256MiB 이하)의 텍스트는 청크 단위로 끝까지 검색한다. 16MiB를 넘는 로그도 검색하며 청크 경계에 걸친 비밀값도 확인한다. 알려 준 비밀값이 있으면 어느 파일인지만 알리고 거부하며 값은 오류 메시지에 쓰지 않는다. 원래 글자뿐 아니라 JSON 안에서 바뀌어 적힌 모양(`\"`, `\uXXXX`), URL 인코딩(`%40`, `+`), HTML 표기(`&amp;`)도 찾는다. base64·압축·다른 문자 인코딩(UTF-16 등)으로 바뀐 값은 찾지 못한다.
- 각 파일의 `sensitiveScan`은 `{status: "CHECKED", reason: null}` 또는 `{status: "SKIPPED", reason: "BINARY_FORMAT" | "NO_SENSITIVE_VALUES"}`다. CHECKED는 호출자가 알려 준 비밀값 문자열을 검색했다는 뜻이며, 모든 비밀정보가 없다는 보장은 아니다. 검사할 비밀값을 전달하지 않으면 NO_SENSITIVE_VALUES다.
- 이미지·영상·PDF·ZIP·TAR·GZ 등 지원하지 않는 바이너리 형식은 BINARY_FORMAT으로 검색 생략을 표시한다. 화면 노출과 압축·문서 내부 내용은 따로 확인한다. 목록 생성 성공만으로 모든 파일의 비밀값 검사가 끝났다고 보지 않는다.

## 결과 요약 만들기 (`src/evaluation/summary.py`)

발표·회의에 쓸 요약은 `render_summary(독립 평가 보고서, scenario_contract=..., metrics=run_metrics 결과, run_bundle=..., trace=D5 결과의 trace, runner=D5 결과의 runner, regression=regression_from_reports 결과)`로 만든다. 함께 넣는 자료가 평가 보고서와 다른 Run·코드 버전·평가의 것이거나 출처가 없으면 요약을 만들지 않는다. 판정, 요구사항별·검사별 결과, 코드 버전별 "실패 → 수정 → 재검증" 흐름, 시간·비용·협업 지표가 Markdown 한 장으로 나온다. 에이전트가 쓴 자유 글(Issue 제목·설명)은 넣지 않고, 모르는 값은 "미확인"으로 적는다. 검증 종료 사건만으로는 PASS를 추정하지 않는다. 만든 요약은 `reports/`에 둔다.

## 지켜야 할 것

- 시연에 쓴 비밀번호는 실행 때 만든 시험용 값이어야 하고, 보고서·로그에 원문과 저장 해시를 남기지 않는다(dev 19번).
- 같은 Run·같은 Snapshot에서 나온 자료만 한 폴더에 둔다. 중간 버전(CODE-1, CODE-2)은 하위 폴더로 구분한다.
- 실패한 Run도 지우지 않는다. 합성 예제·mock 출력은 실제 시연 결과와 섞지 않는다.
- 비용은 `costUsd`가 `null`이면 "미확인"이라고 적고 0으로 쓰지 않는다. 시간은 사건 시각 차이(`run_metrics.py`)를 쓴다.

## 팀장님께 받을 것

- 시연 영상·화면 캡처 원본과 어느 Run의 것인지(Run ID).
- 시연한 실행 로그(Orchestrator `/events`, `/usage`, 서버 로그).
- 12월 발표에 쓸 장면(실패 → 수정 → 재검증 등)을 정하면 그 Run의 자료를 우선 정리한다.
