import assert from 'node:assert/strict'
import test from 'node:test'
import { getAgentRunArtifacts, getAgentRunIssues } from '../src/api/agentRuns.js'
import { popupArtifacts, popupIssues, safePopupText } from '../src/api/agentRunResults.js'

const runId = '00000000-0000-4000-8000-000000000001'
const artifact = (artifactType, extra = {}) => ({ artifactId: 'report-1', artifactType, runId,
  workflowStepId: 'step-1', createdBy: 'QA', codeVersion: 1, artifactVersion: 1, ...extra })

test('실제 Artifact 목록 GET에서 Report 본문을 읽고 설정·환경·원본 로그는 제거한다', async t => {
  let requested
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    requested = { path, options }
    return Response.json({ runId, artifacts: [artifact('RUN_CONFIGURATION', { configuration: { token: 'never-store' } }),
      artifact('QA_REPORT', { executionManifest: { environment: 'never-store' }, stdout: 'never-store',
        tests: [{ outcome: 'FAIL', title: '중복 입력', details: '중복 값 입력 시 서버 오류가 발생했습니다.', toolEvidence: { raw: 'never-store' } }] })] })
  })
  const data = await getAgentRunArtifacts(runId)
  assert.equal(requested.path, `/api/v1/runs/${runId}/artifacts`)
  assert.equal(requested.options.method, 'GET')
  assert.equal(data.artifacts.length, 1)
  assert.equal(data.artifacts[0].tests[0].outcome, 'FAIL')
  assert.match(data.artifacts[0].tests[0].details, /중복 값/)
  assert.doesNotMatch(JSON.stringify(data), /never-store|executionManifest|toolEvidence|configuration|stdout/)
})

test('Issue GET은 실제 연결 필드와 짧은 설명만 반환한다', async t => {
  t.mock.method(globalThis, 'fetch', async path => {
    assert.equal(path, `/api/v1/runs/${runId}/issues`)
    return Response.json({ runId, issues: [{ issueId: 'issue-1', runId, codeVersion: 1, reporter: 'QA',
      reportArtifactId: 'qa-1', sourceArtifactId: 'source-1', title: '입력 검사 오류', description: '필수 입력 검사가 실패했습니다.',
      actualResult: 'never-store', evidenceRefs: ['never-store'], fixWorkflowStepId: 'fix-2', newCodeVersion: 2 }] })
  })
  const data = await getAgentRunIssues(runId)
  assert.equal(data.issues[0].fixWorkflowStepId, 'fix-2')
  assert.match(data.issues[0].description, /필수 입력/)
  assert.doesNotMatch(JSON.stringify(data), /never-store|evidenceRefs|actualResult/)
})

for (const loader of [getAgentRunArtifacts, getAgentRunIssues]) {
  test(`${loader.name}: 다른 Run·잘못된 목록·HTTP 실패를 성공으로 취급하지 않는다`, async t => {
    for (const body of [{ runId: 'other', artifacts: [], issues: [] }, { runId },
      { runId, artifacts: [{ runId: 'other' }], issues: [{ runId: 'other' }] }]) {
      t.mock.method(globalThis, 'fetch', async () => Response.json(body))
      await assert.rejects(loader(runId), { name: 'AgentRunApiError' })
      t.mock.restoreAll()
    }
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'missing' }, { status: 404 }))
    await assert.rejects(loader(runId), { status: 404 })
  })
  test(`${loader.name}: AbortSignal을 GET에 전달하고 취소를 유지한다`, async t => {
    const controller = new AbortController()
    t.mock.method(globalThis, 'fetch', async (path, options) => {
      assert.equal(options.signal, controller.signal)
      controller.abort()
      throw new DOMException('취소', 'AbortError')
    })
    await assert.rejects(loader(runId, { signal: controller.signal }), { name: 'AbortError' })
  })
}

test('알려진 Planner 계약일 때만 요구사항 설명을 사용한다', () => {
  const project = payload => popupArtifacts([artifact('REQUIREMENT', { requirementIds: ['r1'], payload })])[0]
  assert.equal(project({ schemaVersion: 1, requirements: [{ description: '예약 시간을 분석했습니다.' }] }).requirementSummary, '예약 시간을 분석했습니다.')
  assert.equal(project({ summary: '추측하면 안 되는 필드' }).requirementSummary, '')
})

test('Build·Change·Security 실제 필드를 보존하고 가상의 status·overallResult를 만들지 않는다', () => {
  const rows = popupArtifacts([artifact('BUILD_REPORT', { executionOutcome: 'UNVERIFIED', exitCode: -1, failureKind: 'INFRASTRUCTURE', diagnostics: [{ message: '실행 환경 연결 실패' }] }),
    artifact('CHANGE_REPORT', { summary: '입력 검사를 수정했습니다.', fileChanges: [{ path: 'app.js' }] }),
    artifact('SECURITY_REPORT', { requirementResults: [{ outcome: 'FAIL', details: '접근 제한 검사 실패' }], findings: [{ disposition: 'SUSPECTED', severity: 'HIGH', title: '접근 제한', description: '확인 필요' }] })])
  assert.equal(rows[0].executionOutcome, 'UNVERIFIED')
  assert.equal(rows[1].changedFileCount, 1)
  assert.equal(rows[2].findings[0].disposition, 'SUSPECTED')
  assert.ok(rows.every(row => !Object.hasOwn(row, 'status') && !Object.hasOwn(row, 'overallResult')))
})

test('비밀 값·민감 로그는 길이 제한 전에 걸러내며 HTML은 실행 가능한 DOM으로 만들지 않는다', () => {
  for (const text of ['api_key=example-secret', 'Authorization: Bearer private', 'token="private"', 'password:private',
    '비밀번호: 예시', '-----BEGIN RSA PRIVATE KEY-----', 'Traceback (most recent call last)', 'eyJabc.def.ghi',
    `${'가'.repeat(300)} access_key=private`]) {
    assert.equal(safePopupText(text), '민감한 내용은 생략했습니다.')
    const issues = popupIssues([{ description: text }])
    assert.equal(issues[0].description, '민감한 내용은 생략했습니다.')
  }
  assert.equal(safePopupText('검사 결과\n두 번째 줄'), '검사 결과 두 번째 줄')
  assert.doesNotMatch(safePopupText('주소 https://user:private@example.com/path'), /private/)
  assert.ok(safePopupText('긴 문장'.repeat(100)).length <= 150)
})
