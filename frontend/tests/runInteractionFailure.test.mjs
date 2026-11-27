import assert from 'node:assert/strict'
import test from 'node:test'
import { getRunInteraction } from '../src/api/runInteraction.js'
import { runApiTransport } from '../src/api/productRunRequest.js'
import { createProductRunFetch } from '../src/api/productRunRequest.js'

const runId = '70000000-0000-4000-8000-000000000001'
const stepId = '70000000-0000-4000-8000-000000000002'
const failure = () => ({ workflowStepId: stepId, agentRole: 'SECURITY', message: '앞선 검사 실패',
  instruction: '수정·재검사 중이에요.', recovering: true, blocking: false, nextAction: 'WAIT_FOR_REVALIDATION',
  validationIssues: [{ fieldPath: '/testCases', reasonCode: 'CASE_DUPLICATE', reason: '검사 항목이 중복됐어요.' }] })
const payload = row => ({ runId, revision: 'a'.repeat(64), requests: [], canCancel: true, failures: [row] })

test('현재 작업의 재검사 상태와 원인 필드를 보존한다', async t => {
  const data = payload(failure())
  t.mock.method(runApiTransport, 'fetch', async () => Response.json(data))
  assert.deepEqual(await getRunInteraction(runId), data)
})

test('제공자 중단은 제품 실패와 구분하고 남은 작업 기록을 유지한다', async t => {
  const data = payload({ ...failure(), recovering: false, blocking: true, nextAction: 'CHECK_PROVIDER',
    providerRetriesUsed: 1, productFailure: false, workPreserved: true })
  t.mock.method(runApiTransport, 'fetch', async () => Response.json(data))
  assert.equal((await getRunInteraction(runId)).failures[0].workPreserved, true)
})

for (const damage of ['issues-shape', 'issue-reason', 'retry-count', 'state-type', 'unknown-action']) {
  test(`잘못된 실패 상태는 화면을 깨뜨리기 전에 거절한다: ${damage}`, async t => {
    const row = failure()
    if (damage === 'issues-shape') row.validationIssues = { length: 1 }
    if (damage === 'issue-reason') row.validationIssues[0].reason = { unsafe: true }
    if (damage === 'retry-count') row.providerRetriesUsed = -1
    if (damage === 'state-type') row.recovering = 'true'
    if (damage === 'unknown-action') row.nextAction = 'RESTART_WITHOUT_APPROVAL'
    t.mock.method(runApiTransport, 'fetch', async () => Response.json(payload(row)))
    await assert.rejects(getRunInteraction(runId), /실패 원인을 읽을 수 없어요/)
  })
}

test('연결 대상이 다른 이어서 수정 응답은 거절한다', async t => {
  const data = {...payload(failure()), continuation:{available:true,parentRunId:stepId,preservesHistory:true}}
  t.mock.method(runApiTransport,'fetch',async () => Response.json(data))
  await assert.rejects(getRunInteraction(runId),/실패 원인을 읽을 수 없어요/)
})

for (const confirmed of [false,true]) {
  test(`수정 충돌은 제품 서버가 미접수를 확정한 경우만 새 접수 허용: ${confirmed}`, async () => {
    const request=createProductRunFetch({productApiReady:true,runSubmissionEnabled:true,readAccessToken:()=> 'mock-token',
      fetchImpl:async () => Response.json({detail:'CONTINUATION_ALREADY_ACTIVE'}, {status:409,
        headers:confirmed ? {'X-Submission-Status':'NOT_ACCEPTED'} : {}})})
    await assert.rejects(request('/api/v1/runs',{method:'POST',confirmed:true}), error => {
      assert.equal(error.submissionNotAccepted,confirmed)
      assert.equal(error.code,'CONTINUATION_ALREADY_ACTIVE')
      return true
    })
  })
}
