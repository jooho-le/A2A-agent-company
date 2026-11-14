import { act, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from '../../src/App.jsx'
import useOfficeRunSubmission from '../../src/hooks/useOfficeRunSubmission.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
sessionStorage.removeItem('access_token')
window.history.replaceState(null, '', '#/home')
let failLogin = true, loginCalls = 0, unexpectedCalls = 0, probe
// Mock 로그인 이외의 모든 요청은 실패시킵니다. 실제 fetch fallback은 없습니다.
globalThis.fetch = async (url) => {
  if (url !== 'http://127.0.0.1:8000/api/auth/login') {
    unexpectedCalls++
    throw new Error('Unexpected request')
  }
  loginCalls++
  return Response.json(failLogin ? { detail: 'Mock 로그인 실패' } : { access_token: 'mock-login-only', token_type: 'bearer' },
    { status: failLogin ? 401 : 200 })
}
const check = (condition, message) => { if (!condition) throw new Error(message) }
const button = (label, selector = 'button') => [...document.querySelectorAll(selector)].find(item => item.textContent === label)
async function click(element) { check(element, 'button exists'); await act(async () => element.click()) }
async function fill(selector, value) {
  const input = document.querySelector(selector)
  check(input, `input exists: ${selector}`)
  const prototype = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function loginSubmit() {
  await act(async () => document.querySelector('.login-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
}
const host = document.createElement('div')
document.body.append(host)
const root = createRoot(host)
const approval = { approved: true, scenarioId: '70000000-0000-4000-8000-000000000001', configuration: {
  architecture: 'MULTI_AGENT', startingCommitHash: 'a'.repeat(40), startingSnapshotSha256: 'b'.repeat(64),
  model: { provider: 'openai', modelId: 'mock-model', temperature: 0 },
  limits: { maxFixAttempts: 3, maxToolRetries: 2, runtimeBudgetMs: 10000 },
  environment: { containerImageDigest: `sha256:${'c'.repeat(64)}`, dependencyLockHash: `sha256:${'d'.repeat(64)}`,
    hardwareProfile: 'mock-only', networkPolicy: 'DENY', allowedHosts: [] },
} }
const onCreated = () => { throw new Error('Anonymous submission created a Run') }
function Probe() { probe = useOfficeRunSubmission(approval, onCreated); return null }

async function verify() {
  const passed = []
  await act(async () => root.render(<StrictMode><App /></StrictMode>))
  check(document.querySelector('[data-testid=office]') && !document.querySelector('#login-title'), 'guest office stays visible')
  await fill('#request-name', '보존할 프로젝트')
  await fill('#request-text', '보존할 업무 요구사항')
  await click(document.querySelector('.owner-profile'))
  check(!button('로그아웃'), 'guest account does not claim authentication')
  await click(button('로그인', '#atelier-account-menu button'))
  check(document.querySelector('#login-title') && !document.querySelector('.atelier-dashboard'), 'existing login opened')
  await click(button('회원가입'))
  check(document.querySelector('#signup-title'), 'existing signup reused')
  await click(button('로그인'))
  await click(button('오피스로 돌아가기'))
  check(document.querySelector('#request-name').value === '보존할 프로젝트', 'draft name survives login/back')
  check(document.querySelector('#request-text').value === '보존할 업무 요구사항', 'draft text survives login/back')
  passed.push('비로그인 오피스 → 계정 로그인·회원가입, 취소 복귀와 입력 보존')

  await click(document.querySelector('.owner-profile'))
  await click(button('로그인', '#atelier-account-menu button'))
  await fill('#login-email', 'mock@example.test')
  await fill('#login-password', 'mock-password-only')
  await loginSubmit()
  check(document.querySelector('.auth-message').textContent.includes('Mock 로그인 실패'), 'login failure stays on existing screen')
  check(!sessionStorage.getItem('access_token'), 'failed login has no token')
  failLogin = false
  await loginSubmit()
  check(document.querySelector('[data-testid=office]') && !document.querySelector('#login-title'), 'successful login returns to office')
  check(sessionStorage.getItem('access_token') === 'mock-login-only', 'existing token storage used')
  check(document.querySelector('#request-name').value === '보존할 프로젝트', 'name survives login success')
  check(document.querySelector('#request-text').value === '보존할 업무 요구사항', 'request survives login success')
  check(document.querySelector('#request-form button[type=submit]').disabled, 'login does not bypass configuration approval')
  await click(document.querySelector('.owner-profile'))
  check(button('다시 로그인') && button('로그아웃'), 'reauthentication remains available')
  await click(button('로그아웃'))
  check(!sessionStorage.getItem('access_token') && document.querySelector('[data-testid=office]'), 'logout keeps public office')
  passed.push('Mock 로그인 실패 유지·성공 오피스 복귀, 입력 보존·승인 차단 유지')

  check(document.querySelector('#request-form button[type=submit]').disabled, 'guest submit disabled')
  check(document.querySelector('.composer-feedback').textContent.includes('로그인'), 'guest login guidance')
  await act(async () => document.querySelector('#request-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  await click(document.querySelector('[data-view=projects]'))
  check(document.querySelector('#login-title'), 'project management requires login entry')
  await act(async () => root.render(<Probe />))
  check(probe.disabled, 'missing token disables approved submission')
  await act(async () => probe.submit('업무', '요구사항'))
  check(probe.error.includes('로그인'), 'direct hook submission also blocked')
  check(unexpectedCalls === 0 && loginCalls === 2, 'no Run/scenario/Host requests')
  passed.push('JWT 없는 UI·직접 제출 차단, 로그인 안내 및 프로젝트 보호')
  await act(async () => root.unmount())
  return passed
}

verify().then(passed => {
  document.body.innerHTML = ''
  const result = document.createElement('pre'); result.id = 'test-result'
  result.textContent = JSON.stringify({ passed }); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'
  result.textContent = JSON.stringify({ failed: error.message }); document.body.append(result)
})
