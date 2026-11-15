# SCN-001 시범 실험 실행 절차

작성: 2026-11-15 · 4번(강석민). 대상: Single/Multi × Ver1/Ver2 시범(조합마다 1회, 계획 [Ver1](../../examples/evaluation/scn001-pilot.ver1.plan.json)·[Ver2](../../examples/evaluation/scn001-pilot.ver2.plan.json)).
모델 `gpt-4.1-mini-2025-04-14`·temperature 0, 시범 한도(모델·도구 호출 200, 출력 8,192, 응답 120초, 60분)는 계획에 고정돼 있다.

## 1. 지금 상태

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.environment status
```

2026-11-15 준비 완료(dev `3147711` 병합 뒤에도 두 계획 모두 사전점검 `ready=true`, 실제 브라우저 실행을 포함한 37개 항목 모두 PASS):

| 항목 | 값 |
|---|---|
| 공통 실행 이미지(Host 샌드박스 = D5 채점) | `capstone-sandbox:scn001`, `sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a` |
| lock(이미지·시작 커밋 공통) | wheel 33개, `sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0` |
| 시작 커밋 | `.data/starting-source/scn001`, `e6e7158c356ff0be5ffa12ec59894d2217f61ca8` |
| 도구 설정 | `configs/experiment/ver1·ver2/owned-tools.local.json`(Ver1만 보호 범위 2개) |
| 하드웨어 프로필 | `MacBookPro18,1-Apple-M1-Pro-10cpu-16gib/docker-29.1.3-linux/aarch64-10cpu-7.7gib/darwin` |
| 보호 시험 | 0.7.0, 공식 최종 SHA-256 `cd7a246d2309ac7e3e281355be270a9ea8866e837da03058f8caaf10ef8f121f` |

로컬 PyCharm 실행 구성에 API 키와 Multi 4개·Single 1개 내부 토큰을 등록했다. 키 값은 출력하지 않았으며 원격 API의 유효성·접근 권한은 아직 확인하지 않았다. 임시 데이터 저장소에서 Single/Multi × Ver1/Ver2의 Host 기동·정상 응답·토큰 인증·종료를 확인했고, 이때 모델의 외부 전송을 차단해 API 호출은 0회였다. dev `3147711`부터 Host API에 서버 전용 토큰 `A2A_HOST_API_TOKEN`도 필요하다(3절). 남은 것은 키·토큰을 넣은 Host 기동, 유료 시범 Run과 그 실제 생성물의 비교다. 브라우저 문제는 아래와 같이 해결했다.

### 알려진 문제: 브라우저 도구

dev 샌드박스는 권한을 모두 빼고(`--cap-drop ALL`) 기본 seccomp를 강제한다. 이 조건에서 이전 실행기의 `chromium_sandbox=True`는 `No usable sandbox!`로 실패했다. 같은 이미지·같은 제한에서 `chromium_sandbox=False`로는 정상 실행됨을 확인했다. QA 에이전트에게는 단위 시험과 함께 브라우저 시험 대상도 제시된다.

해결(2026-11-15, 사용자 승인): dev 실행기를 `chromium_sandbox=False`로 바꿨다. 컨테이너 격리는 그대로이며 근거는 [dev 변경 공유](dev_changes_2026-11-14.md)에 있다(주호 확인 필요). 사전점검에 실제 브라우저 실행 항목(`hostBrowser.<구조>`)을 더했고 두 계획 모두 통과한다.

## 2. 실행 환경 만들기 (다운로드 승인 후)

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.environment all --allow-network
```

받는 것(모두 git에 올리지 않는 `docker/sandbox/prepared/`와 로컬 Docker 이미지에만 남음):

| 출처 | 내용 | 크기(예상) |
|---|---|---|
| PyPI | Linux arm64·CPython 3.13 wheel 약 30개(Playwright 1.65.2 53MB 포함) | 약 65MB |
| Playwright CDN | Chromium headless shell(linux arm64) | 약 100MB |
| Debian 13 저장소 | Chromium 실행 라이브러리(Playwright가 정한 목록) | 약 40~70MB |

단계별로 하려면 `wheels` → `browsers` → `image` → `configure` 순서로 같은 명령을 쓴다. 실제 준비에서 확인한 점:

- Playwright 드라이버(내장 node)는 `libatomic1`이 필요하다. 준비 컨테이너와 베이스 이미지에 항상 먼저 넣는다.
- Playwright 1.65의 `install-deps --dry-run`은 xvfb·mesa·LLVM·글꼴까지 109개를 나열한다. 베이스에는 Chromium 직접 라이브러리 21개와 기본 글꼴만 넣고, 실제 브라우저 실행 파일을 `ldd`로 확인한다.
- 로컬에서 만든 베이스는 레지스트리에 없어서 `FROM 이름@sha256:…`를 BuildKit이 Docker Hub에서 찾는다. 그 참조 문자열을 키로 한 `--build-context`로 방금 만든 로컬 이미지에 연결한다(dev 레시피는 그대로).
- 실제 크기: wheel 62MB, Chromium headless shell·ffmpeg 287MB(압축 해제 기준), 최종 이미지 약 329MB.

`configure`는 네트워크 없이 아래를 만든다.

- `configs/owned-tools.local.json`: 이미지 ID·lock 해시·하드웨어 프로필·도구 한도(빌드 60초, 단위·브라우저 90초, 보안 60초, 호출 한도 120초)·Bandit 규칙(B105~107, B303, B324)
- `configs/experiment/ver1·ver2/owned-tools.local.json`: Ver1만 순환 보호 범위 두 개 포함
- `configs/owned-source.local.json` + `.data/starting-source/scn001`: 같은 lock 바이트를 담은 공통 시작 커밋
- 두 계획의 `image`·`hardwareProfile`·`source.commitHash`, 그리고 사전점검(`preflight`) 결과

사전점검이 두 계획 모두 `ready=true`가 아니면 멈추고 사유를 고친다.

## 3. 키·토큰

현재 PyCharm의 `Multi Host`·`Single Host` 실행 구성은 로컬에 키·토큰이 등록된 **설정 점검용**이며 `--start`가 없다. API 키를 문서·커밋에 넣지 않는다. PyCharm 구성의 환경변수는 아래 터미널 명령에 자동 전달되지 않으므로, CLI로 실행할 때는 해당 터미널에도 키·토큰을 별도로 주입한다. 같은 Host가 사용하는 송신·수신 토큰은 서로 같아야 하며 가동 중 임의로 교체하지 않는다.

```bash
export A2A_PLATFORM_LLM_API_KEY='<OpenAI API 키>'
```

```bash
for name in PLANNER DEVELOPER QA SECURITY SINGLE_AGENT; do export "A2A_PLATFORM_${name}_TOKEN=$(openssl rand -hex 32)"; done
```

dev 83번(2026-11-15 병합)부터 실제 Host API는 서버 전용 Bearer 토큰 `A2A_HOST_API_TOKEN`을 요구한다. 32자 이상이며 위 Agent 토큰·API 키와 달라야 한다. 같은 값을 **Host 두 개와 측정 실행기(`experiment_run`·`experiment fetch`) 터미널 모두**에 넣는다. 실행기는 이 값을 기록·결과 파일에 남기지 않는다.

```bash
export A2A_HOST_API_TOKEN="$(openssl rand -hex 32)"
```

Run 생성에는 중복 방지 키(`Idempotency-Key`)가 필수다. 실행기는 제출 의도를 `runs.jsonl`에 남길 때 키를 만들어 함께 기록하고 그 키로 보낸다. 응답을 못 받으면 `--reconcile-submission`으로 같은 키의 접수 기록을 조회해 이어 쓰고, 접수 기록이 없다고 확인될 때만 같은 키·같은 본문으로 다시 보낸다(Host가 중복 Run을 막음).

## 4. Ver1 실행

Host 두 개를 각각 다른 터미널에서 띄운다(Multi 8000, Single 8010).

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --runtime configs/owned-runtime.local.json --tools configs/experiment/ver1/owned-tools.local.json --source configs/owned-source.local.json --qa-cases configs/experiment/ver1/owned-protected-cases.local.json --price-book configs/owned-price-book.scn001.json --start
```

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --architecture SINGLE_AGENT --runtime configs/single-runtime.local.json --tools configs/experiment/ver1/owned-tools.local.json --source configs/owned-source.local.json --qa-cases configs/experiment/ver1/owned-protected-cases.local.json --price-book configs/owned-price-book.scn001.json --start
```

측정 Run 제출·대기·인계(질문·보류로 멈춘 Run은 재개하지 않고 끝으로 집계):

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.experiment_run all --plan examples/evaluation/scn001-pilot.ver1.plan.json --out experiments/scn001-pilot
```

## 5. Ver2 실행

Ver1 Host를 끄고, 같은 명령에서 `--tools configs/experiment/ver2/owned-tools.local.json`으로 바꾸고 `--qa-cases`를 뺀 뒤 다시 띄운다. 계획만 Ver2로 바꿔 같은 `--out`에 실행한다.

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.experiment_run all --plan examples/evaluation/scn001-pilot.ver2.plan.json --out experiments/scn001-pilot
```

## 6. 채점·비교

인계 폴더(`experiments/scn001-pilot/NNN-…`)마다 최종 채점과 Agent 평가표를 만든다. `--image`는 계획의 이미지 ID와 같아야 한다(Host와 같은 이미지에서 채점). 채점 전에 실행 원장 `runs.jsonl`의 `repetition`을 확인하고, **같은 반복의 Single/Multi × Ver1/Ver2 네 조합에 같은 `--pair-id`**를 준다. 시범 네 Run은 모두 `scn001-pilot-r1`이다. 공식 반복 실험은 등록한 반복에 따라 `…-r1`부터 `…-r5`까지 구분한다. 결과를 보고 짝을 다시 고르지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.workflow --export experiments/scn001-pilot/<인계 폴더> --results experiments/scn001-pilot/results --image <계획의 image> --exam pilot --pair-id scn001-pilot-r1
```

결과 폴더는 네 조합을 함께 보관한다. 비교할 때 반대 축을 고정한다. `--group-by architecture --loop-policy IN_LOOP_AND_FINAL`은 Ver1의 Single/Multi 비교이고, `--group-by loopPolicy --architecture MULTI_AGENT`는 Multi의 Ver1/Ver2 비교다. 필터를 쓰지 않은 전체 2×2 자료는 서로 다른 조건이 섞였으므로 비교 조건 검사에서 통과하지 않는다.

먼저 선택할 각 비교의 도구·한도와 **실제 Run에 고정된 정책**을 확인한다. 아래 코드는 계획·보관 결과·Host DB를 읽고 네 개의 parity 보고서만 쓴다. Host 시작이나 모델 호출을 하지 않는다. Host DB가 없거나 정책이 달라졌으면 PASS로 처리되지 않는다.

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from pathlib import Path
from evaluation.parity import check, check_versions

root = Path.cwd()
results = root / 'experiments/scn001-pilot/results'
out = root / 'experiments/scn001-pilot/comparisons'
out.mkdir(parents=True, exist_ok=True)
plans = {}
for version in (1, 2):
    path = root / f'examples/evaluation/scn001-pilot.ver{version}.plan.json'
    plans[version] = (path.parent, json.loads(path.read_text()))

def files(version, architecture):
    base, plan = plans[version]
    entry = plan['hostConfigurations'][architecture]
    return {target: (base / entry[source]).resolve() if entry.get(source) else None
            for target, source in [('runtime', 'runtime'), ('tools', 'tools'), ('source', 'source'),
                                   ('cases', 'protectedCases'), ('priceBook', 'priceBook')]}

runs = []
for folder in sorted(results.iterdir()):
    path = folder / 'bundle.json'
    if folder.name.startswith('.') or not path.is_file():
        continue
    bundle = json.loads(path.read_text())
    config = bundle['configuration']['configuration']
    architecture = config['architecture']
    version = 1 if config.get('protectedTestSuiteRef') else 2
    runtime_path = files(version, architecture)['runtime']
    runtime = json.loads(runtime_path.read_text())
    database = (runtime_path.parent / runtime['orchestrator']['databasePath']).resolve()
    runs.append({'runId': bundle['run']['runId'], 'architecture': architecture,
                 'loopPolicy': 'IN_LOOP_AND_FINAL' if version == 1 else 'FINAL_ONLY',
                 'hostDatabase': str(database)})

for version, policy in [(1, 'IN_LOOP_AND_FINAL'), (2, 'FINAL_ONLY')]:
    report = check(files(version, 'SINGLE_AGENT'), files(version, 'MULTI_AGENT'),
                   runs=[row for row in runs if row['loopPolicy'] == policy])
    (out / f'parity-ver{version}-architecture.json').write_text(json.dumps(report, indent=2))
for architecture in ('SINGLE_AGENT', 'MULTI_AGENT'):
    report = check_versions(files(1, architecture), files(2, architecture), architecture=architecture,
                            runs=[row for row in runs if row['architecture'] == architecture])
    (out / f'parity-{architecture.lower()}-versions.json').write_text(json.dumps(report, indent=2))
PY
```

다음 네 명령은 원본 결과를 이동하지 않는다. `--planned`는 **선택한 조건에서 사전에 계획한 수**이며 성공하거나 수집된 결과 수가 아니다. 아래는 조합마다 1회인 시범의 분모다. 공식 조합마다 5회 실험에서는 각 값을 5로 바꾼다. `--run-journal`은 이미 시작했지만 export가 없는 Run과 제출 여부가 미확인인 슬롯까지 보존한다. 그런 슬롯이 남으면 시간·비용 전체 합과 공식 우열 결론은 미확인으로 유지한다.

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.experiment compare --results experiments/scn001-pilot/results --group-by architecture --loop-policy IN_LOOP_AND_FINAL --planned SINGLE_AGENT=1 --planned MULTI_AGENT=1 --run-journal experiments/scn001-pilot/runs.jsonl --parity experiments/scn001-pilot/comparisons/parity-ver1-architecture.json --out experiments/scn001-pilot/comparisons/ver1-architecture
PYTHONPATH=src .venv/bin/python -m evaluation.experiment compare --results experiments/scn001-pilot/results --group-by architecture --loop-policy FINAL_ONLY --planned SINGLE_AGENT=1 --planned MULTI_AGENT=1 --run-journal experiments/scn001-pilot/runs.jsonl --parity experiments/scn001-pilot/comparisons/parity-ver2-architecture.json --out experiments/scn001-pilot/comparisons/ver2-architecture
PYTHONPATH=src .venv/bin/python -m evaluation.experiment compare --results experiments/scn001-pilot/results --group-by loopPolicy --architecture SINGLE_AGENT --planned IN_LOOP_AND_FINAL=1 --planned FINAL_ONLY=1 --run-journal experiments/scn001-pilot/runs.jsonl --parity experiments/scn001-pilot/comparisons/parity-single_agent-versions.json --out experiments/scn001-pilot/comparisons/single-versions
PYTHONPATH=src .venv/bin/python -m evaluation.experiment compare --results experiments/scn001-pilot/results --group-by loopPolicy --architecture MULTI_AGENT --planned IN_LOOP_AND_FINAL=1 --planned FINAL_ONLY=1 --run-journal experiments/scn001-pilot/runs.jsonl --parity experiments/scn001-pilot/comparisons/parity-multi_agent-versions.json --out experiments/scn001-pilot/comparisons/multi-versions
```

각 비교의 `comparison.json`과 `comparison.md`에는 고정 조건·다른 조건으로 제외한 건수·계획 분모·미수집 실행 슬롯이 남는다. 설정 파일만 대조한 parity PASS로 공식 비교에 들어가지 않으며, 선택한 모든 Run의 고정 정책도 일치해야 한다.

비교 지표와 공식 한도 결정은 [평가 실행 설명](../../src/evaluation/README.md)의 `experiment compare`·`comparison_report` 절을 따른다. 시범 사용량으로 공식 한도를 정한 뒤 공식 실험(조합마다 5회, 20 Run)을 같은 절차로 돌린다.

## 주의

- Run 제출부터 유료 모델 호출이 일어난다. 시범 4 Run의 비용 상한은 따로 정하지 않았다(계획의 호출·토큰 한도가 Run별 상한).
- 같은 `--out`에서 앞 순서가 끝나지 않으면 다음 Run을 제출하지 않는다. 시간 초과는 Run을 취소하지 않으므로 같은 명령을 다시 실행해 이어 받는다.
- 브라우저 도구는 컨테이너 안 Chromium 샌드박스(`chromium_sandbox=True`)를 쓴다. 시범에서 브라우저 시험이 매번 미검증이면 Docker의 사용자 네임스페이스 설정을 먼저 확인한다.

## 세부 관찰 결과 보고서 (suite 0.7.0)

동일한 최종 시험 실행에서 모든 QA·Security 세부 항목의 기대값·실제값·PASS/FAIL/NOT_RUN/UNVERIFIED를 수집한다. `check_details.json`과 `check_details.md`로 별도 저장하며, 요약에는 링크를 남긴다. 공식 검사 9개와 요구사항 7개의 판정·점수는 유지한다. 자세한 동작과 범위는 [상세 결과 기록](check_details.md)을 참고한다. 새 시험지 지문으로 실행하며 구형 기록의 세부 결과를 추정해 채우지 않는다.
