import assert from 'node:assert/strict'
import test from 'node:test'
import { pendingOfficeSubmissionKey } from '../src/hooks/pendingOfficeSubmission.js'

test('미확인 접수 키는 계정별로 나누고 JWT·이메일을 저장 키에 복사하지 않는다', t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  let token
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: () => token } })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  })
  const jwt = sub => 'test.' + Buffer.from(JSON.stringify({ sub, email: 'private@example.test' })).toString('base64url') + '.test'
  token = jwt('1')
  const first = pendingOfficeSubmissionKey()
  assert.equal(first, 'ai-company.pending-office-submission.1')
  token = jwt('2')
  assert.notEqual(pendingOfficeSubmissionKey(), first)
  token = jwt('1')
  assert.equal(pendingOfficeSubmissionKey(), first)
  for (const value of [null, '', 'bad-token', jwt('../1'), jwt(1), jwt(null)]) {
    token = value
    assert.equal(pendingOfficeSubmissionKey(), null)
  }
})
