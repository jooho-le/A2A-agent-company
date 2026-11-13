# 79번 — 싱글·멀티 공통 규칙과 실패 경로 회귀

## 같은 규칙을 적용한 위치

`GuardedRuntime`·`GuardedRecord`·`GuardedLab`을 통해 멀티 네 역할과 Single의 네 관점 응답에 같은 v3 계약·원문 대조·기대값 연결·별도 의미 검토·인계 정책을 적용한다. Single을 여러 Agent 정체성으로 분리하지 않는다.

멀티는 Planner → Developer → QA·Security 병렬이다. 앞 응답이 차단되면 뒤 역할을 호출하지 않고 원인에 맞는 `SKIPPED`를 남긴다. Single도 응답을 받았다는 이유로 전체 작업 성공을 선언하지 않는다.

## 주요 검사

| 구분 | 확인 내용 |
| --- | --- |
| 출력 계약 | 역할별 Strict Schema, 외부 역할 필드·추가 속성·허위 실행 상태 거절 |
| 원문·기대값 | 의역한 요구, 추가 기대값, 없는 ID, 미정 조건의 검사, 연결 누락 거절 |
| 질문·미승인 가정 | READY 거절, INPUT_REQUIRED에 질문 요구, Developer 자동 호출 중단 |
| 의미 검토 | 원문/응답 인용·경로·해시 바인딩, 미검증·상충·거절 처리 |
| 변경·취소·오류 | 검토 후 응답 변경, 검토 취소, 인증 오류에서 실패 보존 |
| 예산 | 응답/검토 각 20회 상한, 사용한 Runtime 재사용 거절, 폴더 덮어쓰기 거절 |
| 기존 경로 | v2 자연어, A2A Receipt·metadata·타이밍·연결 재사용 유지 |
| 제품 계약 | 기존 승인 Requirement Scope·Planner 규칙 회귀 |

`tests/test_communication_guarded.py`는 모델 응답과 검토 응답을 합성 Transport로 제어한다. A2A HTTP·SQLite·실제 SDK는 실행하므로 서버 전달 경계까지 확인한다. 실제 OpenAI 결과는 별도의 80번이며 합성 응답 수를 실제 API 호출 수로 세지 않는다.

## 실행 명령

```bash
PYTHONPATH=src:tests .venv/bin/python \
  -W 'ignore:A VersionedTaskStore was configured without an event_stream:UserWarning' \
  -m unittest test_communication_guarded test_communication_calibration \
  test_communication_guarded_reporting test_communication_reporting \
  test_communication_peer_sessions test_communication_overhead \
  test_communication_timing test_communication_performance \
  test_communication_live test_communication_natural \
  test_communication_delivery test_a2a_delivery \
  test_requirement_scope test_planner_contract -q -f
```

실제 API 실행 전 묶음은 161개 통과였다. 예산 재사용과 검토 보강을 추가한 뒤 **최종 173개 모두 통과, 27.312초**를 확인했다. 위 명령으로 실행했으며, 중복 재실행 수를 총 검사 수에 더하지 않는다.

최종 검사에는 필드별 근거 누락·바뀐 인용·전체 판정과 필드 판정의 충돌, 모델이 개인정보 확대를 SUPPORTED라고 해도 보류하는 규칙, 명시적인 개인정보 제외와 미검토의 구분, 선택 케이스·누적 호출 상한, 검토기 대조의 기대 라벨 비전송, 실패 대조·미호출을 숨기지 않는 보고서 집계까지 포함한다.

변경 Python AST·문서 링크·`git diff --check`, 실제 API 원자료/실행 소스의 해시·이전 PDF 보존, 새 PDF의 페이지·표 넘침·원문 포함도 최종 확인했다. 이 173개는 전체 저장소의 모든 테스트나 실제 제품 Docker 검증을 뜻하지 않는다.

## 개발정의서 점검

- A2A 공식 상태·식별자·최초 metadata·Artifact Schema·동결 Configuration을 유지한다.
- 원래 요청·응답과 거절/미검증 결과를 보존하고 자동 보정·자동 재호출하지 않는다.
- 사용자의 미승인 제안과 모델의 검토를 승인 권한으로 취급하지 않는다.
- 제품 인증·Sandbox·MCP ACL·수정/Tool Retry 한도를 변경하지 않는다.
- 프론트·독립 평가 시험지·실제 제품 DB 구현을 수정하지 않는다.

다음은 [80번](80-guarded-live-revalidation.md)의 실제 API 재실험·보고서다.
