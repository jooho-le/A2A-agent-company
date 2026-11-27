import assert from 'node:assert/strict'
import test from 'node:test'
import { runLabel } from '../src/components/atelier/roleLabels.js'

test('종료된 작업이라도 실패·미검증을 성공으로 안내하지 않는다', () => {
  for (const verdict of ['FAIL', 'UNVERIFIED', 'HUMAN_REVIEW', null, 'UNKNOWN']) {
    assert.notEqual(runLabel({ status: 'FINISHED', verdict }), runLabel({ status: 'FINISHED', verdict: 'SUCCESS' }))
  }
  assert.match(runLabel({ status: 'FINISHED', verdict: 'FAIL' }), /통과하지 못/)
  assert.match(runLabel({ status: 'FINISHED', verdict: 'UNVERIFIED' }), /끝내지 못/)
})

test('진행 상태나 응답이 없으면 완료로 추측하지 않는다', () => {
  const complete = runLabel({ status: 'FINISHED', verdict: 'SUCCESS' })
  for (const status of ['IMPLEMENTING', 'VALIDATING', 'ABORTED', 'UNKNOWN', null]) {
    assert.notEqual(runLabel({ status, verdict: 'SUCCESS' }), complete)
  }
  assert.match(runLabel(null), /확인할 수 없/)
})

test('확인 대기를 실행 중이나 입력 누락으로 단정하지 않는다', () => {
  const label = runLabel({ status: 'WAITING_INPUT', verdict: null })
  assert.match(label, /확인.*멈췄/)
  assert.doesNotMatch(label, /완료|작업 중|입력.*부족/)
})
