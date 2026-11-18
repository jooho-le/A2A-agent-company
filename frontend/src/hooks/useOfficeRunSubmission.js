import { useEffect, useRef, useState } from 'react'
import { hasStoredAccessToken, loginRequired } from '../api/productRunRequest'
import { loadOfficeApproval, lookupOfficeWork, submitOfficeWork } from '../api/officeService'
import { pendingOfficeSubmissionKey } from './pendingOfficeSubmission.js'
import { dispatchMessage, runUuid } from '../api/agentRunSubmission.js'

const empty = { busy: false, locked: false, error: '', result: null, name: '', requestText: '', uncertain: false,
  authenticationRequired: false, message: '' }

export default function useOfficeRunSubmission(_legacyApproval, onCreated) {
  const [state, setState] = useState(empty)
  const [configuration, setConfiguration] = useState({ value: null, loading: false, error: '' })
  const [revision, refreshApproval] = useState(0)
  const pending = useRef(false)
  const blocked = useRef(false)
  const requestId = useRef(null)
  const lifecycle = useRef({ mounted: false, sequence: 0 })
  const hasToken = hasStoredAccessToken()
  const pendingKey = pendingOfficeSubmissionKey()

  useEffect(() => {
    const instance = lifecycle.current
    instance.mounted = true
    return () => { instance.mounted = false; instance.sequence++ }
  }, [])
  useEffect(() => {
    if (!hasToken) return
    const controller = new AbortController()
    setConfiguration({ value: null, loading: true, error: '' })
    loadOfficeApproval(controller.signal).then(value => {
      if (!controller.signal.aborted) setConfiguration({ value, loading: false, error: '' })
    }).catch(error => {
      if (!controller.signal.aborted) setConfiguration({ value: null, loading: false, error: error.message,
        authenticationRequired: error.status === 401 })
    })
    try {
      let key = pendingKey && sessionStorage.getItem(pendingKey)
      if (pendingKey && !runUuid.test(key)) {
        const legacyKey = 'ai-company.pending-office-submission'
        const legacy = sessionStorage.getItem(legacyKey)
        if (runUuid.test(legacy)) {
          // 이전 버전의 접수 식별자를 잃지 않습니다. 실제 소유권은 GET으로 검증합니다.
          key = legacy
          try {
            sessionStorage.setItem(pendingKey, legacy)
            sessionStorage.removeItem(legacyKey)
          } catch { /* 저장 실패 시에도 기존 ID와 메모리 중복 차단을 보존합니다. */ }
        }
      }
      if (key && runUuid.test(key)) { requestId.current = key; blocked.current = true; setState(value => ({ ...value, locked: true, uncertain: true })) }
      else if (key) sessionStorage.removeItem(pendingKey)
    } catch { /* 저장소를 사용할 수 없으면 현재 화면에서만 중복을 차단합니다. */ }
    return () => controller.abort()
  }, [hasToken, pendingKey, revision])

  function remember(key) {
    requestId.current = key
    try {
      if (pendingKey) key ? sessionStorage.setItem(pendingKey, key) : sessionStorage.removeItem(pendingKey)
    } catch { /* 메모리 중복 차단은 유지 */ }
  }
  function accept(result, name, requestText) {
    const uncertain = ['UNKNOWN', 'PENDING'].includes(result.submissionStatus)
    blocked.current = result.submissionStatus !== 'REJECTED'
    setState({ ...empty, locked: blocked.current, error: result.submissionStatus === 'REJECTED'
      ? '실행 서버가 요청을 거절했습니다. 설정과 요청 내용을 확인해 주세요.' : '', result, name, requestText, uncertain,
      message: result.submissionStatus === 'CREATED'
        ? (result.httpStatus === 200 ? '기존 Run을 재사용합니다. ' : '새 Run을 생성했습니다. ') + dispatchMessage(result.dispatchStatus) : '' })
    if (!uncertain) remember(null)
    if (result.submissionStatus === 'CREATED') onCreated({ ...result, projectName: name, requestText })
  }
  async function submit(name, requestText, { projectId } = {}) {
    if (!hasStoredAccessToken()) { setState(value => ({ ...value, error: loginRequired().message, authenticationRequired: true })); return false }
    if (pending.current || blocked.current || state.locked || !configuration.value || !name.trim() || !requestText.trim()) return false
    pending.current = true
    const sequence = ++lifecycle.current.sequence
    const key = crypto.randomUUID()
    remember(key)
    setState(value => ({ ...value, busy: true, error: '', name: name.trim(), requestText: requestText.trim() }))
    try {
      const result = await submitOfficeWork({ ...configuration.value, ...(projectId ? { projectId } : {}),
        projectName: name.trim(), requestText: requestText.trim() }, key, { confirmed: true })
      if (lifecycle.current.mounted && sequence === lifecycle.current.sequence) accept(result, name.trim(), requestText.trim())
    } catch (error) {
      if (lifecycle.current.mounted && sequence === lifecycle.current.sequence) {
        const changed = error.code === 'CONFIGURATION_CHANGED'
        const localRefusal = ['RUN_SUBMISSION_DISABLED', 'EXPLICIT_SUBMISSION_REQUIRED', 'INVALID_ARGUMENT', 'PRODUCT_API_NOT_READY'].includes(error.code)
        const uncertain = !error.submissionNotAccepted && !changed && !localRefusal
          && ![400, 401, 403, 404, 413, 415, 422, 429].includes(error.status)
        blocked.current = uncertain
        if (!uncertain) remember(null)
        if (error.submissionNotAccepted || changed || localRefusal || error.status === 401) {
          setConfiguration({ value: null, loading: false, error: error.message, authenticationRequired: error.status === 401 })
        }
        setState(value => ({ ...value, busy: false, locked: uncertain, uncertain, authenticationRequired: error.status === 401,
          error: uncertain ? error.message + ' 다시 생성하지 말고 접수 상태를 확인해 주세요.' : error.message }))
      }
    } finally { pending.current = false }
    return false
  }
  async function recover() {
    if (pending.current || !requestId.current) return
    pending.current = true
    const sequence = ++lifecycle.current.sequence
    setState(value => ({ ...value, busy: true, error: '' }))
    try {
      const result = await lookupOfficeWork(requestId.current)
      if (lifecycle.current.mounted && sequence === lifecycle.current.sequence) accept(result, state.name, state.requestText)
    } catch (error) {
      if (lifecycle.current.mounted && sequence === lifecycle.current.sequence) setState(value => ({ ...value, busy: false,
        error: error.message, authenticationRequired: error.status === 401 }))
    } finally { pending.current = false }
  }
  function reset() {
    if (pending.current || state.uncertain) return false
    remember(null); blocked.current = false; lifecycle.current.sequence++; setState(empty)
    return true
  }
  return { ...state, submit, recover, reset, refreshApproval: () => refreshApproval(value => value + 1),
    authenticationRequired: state.authenticationRequired || configuration.authenticationRequired,
    configurationLoading: configuration.loading, configurationError: configuration.error,
    disabled: !hasToken || !configuration.value || state.busy || state.locked,
    notice: !hasToken ? '업무를 맡기려면 로그인해 주세요.'
      : state.uncertain ? '접수 결과가 불확실합니다. 접수 상태 확인으로 복구해 주세요. 중복 생성은 차단되어 있습니다.'
        : configuration.loading ? '실행 서버의 준비 상태를 확인하고 있어요.'
          : configuration.error || '현재 회원가입 기능 개발을 지원합니다. 업무 시작을 누르면 실제 에이전트가 실행됩니다.' }
}
