# FAIL 후 독립 검사 계속 — suite 0.7.0 검증

2026-11-15. 관련 회귀검사 **2,299개 통과, 실패 0, 오류 0, 건너뜀 0**. 평가 코드 전체, 보호 시험 전달·실행·보고서, Host 설정 연동, 런타임 설정, MCP 계약과 로컬 Docker 평가를 확인했다. 두 묶음은 1,440개와 859개이며, 중단한 이전 실행이나 중복된 집중 검사는 합산하지 않았다. 최종 실행 중 Python 파일 532개의 내용은 변경되지 않았다.

이전 suite 0.6.0의 저장소 전체 5,735개 검증 기록과 구분한다. 이번 기록은 0.7.0 변경에 대한 위 범위의 회귀검사다. 실제 싱글·멀티 비교 실험이나 모델 API 호출은 실행하지 않았다.

## 변경된 동작

- FAIL을 상위 검사에 기록하면서 독립 세부 항목은 계속 검사한다. 이후 PASS가 앞선 FAIL을 덮어쓰지 않는다.
- 응답 코드 실패 후에도 DB 증가량·기존 행 변경을 검사한다. 이메일 정규화 실패 후에도 DB UNIQUE를 확인한다.
- 한 계정의 준비·해시 확보 실패가 다른 계정의 검사를 막지 않는다. 응답이 오류여도 확보된 해시가 있으면 관찰하되 준비 단계의 실패는 유지한다.
- 메모리 조건 위반이나 Argon2i 사용처럼 정책이 틀렸어도 해석 가능한 m·t·p·salt·비밀번호 검증을 계속한다.
- 실제로 해석할 수 없는 해시의 종속 항목은 `HASH_FORMAT_REQUIRED`, 값이 없는 항목은 `HASH_VALUES_REQUIRED`로 미실행 처리한다. 관찰 오류·기록 불신은 기존대로 미검증이다.
- 기존 9개 공식 검사·7개 제품 요구사항·점수 산식은 유지한다. 관찰량과 입력 소비가 달라질 수 있으므로 suite 버전을 0.7.0으로 올리고 이전 입력 재현 기록과 구분한다.

## 생성한 예제

검사용 회원가입 예제 코드를 로컬에서 평가한 상세표다. 실제 에이전트 개발 결과가 아니며, 구조와 실행 이미지 표시는 테스트 대역의 메타데이터다. 아래 개수는 SEC-SIGNUP-002의 준비 조건을 포함한 16개 항목에 해당한다. 각 파일에는 전체 9개 검사의 상세표가 있다.

| 예제 | PASS | FAIL | 미실행 | 미검증 |
|---|---:|---:|---:|---:|
| [메모리 부족](examples/continue-after-failure/weak-memory.md) | 14 | 2 | 0 | 0 |
| [해석 불가능한 해시](examples/continue-after-failure/invalid-hash.md) | 3 | 2 | 11 | 0 |
| [Argon2i 사용](examples/continue-after-failure/wrong-algorithm.md) | 15 | 1 | 0 | 0 |

## 호환성과 증거

Python 3.10.2와 3.13.16에서 보호 파일 8개의 전달 내용이 같고 AST가 원본과 같음을 확인했다. 원래 토큰을 보존하는 공백 압축을 사용하며 Host의 기존 64 KiB 제한과 4 KiB 여유분 검사는 유지했다. 로컬 unit 설정은 61,334바이트다. 보호 범위·실험 설정·시험지 문서의 지문을 갱신했다.

최종 시험지 SHA-256: `cd7a246d2309ac7e3e281355be270a9ea8866e837da03058f8caaf10ef8f121f`.

- [최종 실행 요약](/private/tmp/capstone-continue-details-20261115-2uyq8wjb/completion.json)
- [검사 수집 목록](/private/tmp/capstone-continue-details-20261115-2uyq8wjb/collection.log)
- [첫 번째 묶음 결과](/private/tmp/capstone-continue-details-20261115-2uyq8wjb/shard-0.xml) / [두 번째 묶음 결과](/private/tmp/capstone-continue-details-20261115-2uyq8wjb/shard-1.xml)
- [보고서 상태와 동작 정의](check_details.md)
