import { useEffect, useRef, useState } from 'react'
import { hasStoredAccessToken, loginRequired } from '../api/productRunRequest'
import { loadOfficeApproval, lookupOfficeWork, submitOfficeWork } from '../api/officeService'

const pendingKey = 'ai-company.pending-office-submission'
const empty = { busy: false, locked: false, error: '', result: null, name: '', requestText: '', uncertain: false }

export default function useOfficeRunSubmission(_legacyApproval, onCreated) {
  const [state, setState] = useState(empty)
  const [configuration, setConfiguration] = useState({ value: null, loading: false, error: '' })
  const [revision, refreshApproval] = useState(0)
  const pending = useRef(false)
  const current = useRef(0)
  const requestId = useRef(null)
  const mounted = useRef(true)
  const hasToken = hasStoredAccessToken()

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; current.current++ }
  }, [])
  useEffect(() => {
    if (!hasToken) return
    const controller = new AbortController()
    setConfiguration({ value: null, loading: true, error: '' })
    loadOfficeApproval(controller.signal).then(value => {
      if (!controller.signal.aborted) setConfiguration({ value, loading: false, error: '' })
    }).catch(error => {
      if (!controller.signal.aborted) setConfiguration({ value: null, loading: false, error: error.message })
    })
    try {
      const key = sessionStorage.getItem(pendingKey)
      if (key) { requestId.current = key; setState(value => ({ ...value, locked: true, uncertain: true })) }
    } catch { /* 저장소를 사용할 수 없으면 현재 화면에서만 중복을 차단합니다. */ }
    return () => controller.abort()
  }, [hasToken, revision])

  function remember(key) {
    requestId.current = key
    try { key ? sessionStorage.setItem(pendingKey, key) : sessionStorage.removeItem(pendingKey) } catch { /* 메모리 중복 차단은 유지 */ }
  }
  function accept(result, name, requestText) {
    const uncertain = ['UNKNOWN', 'PENDING'].includes(result.submissionStatus)
    setState({ busy: false, locked: result.submissionStatus !== 'REJECTED', error: result.submissionStatus === 'REJECTED'
      ? '실행 서버가 요청을 거절했습니다. 설정과 요청 내용을 확인해 주세요.' : '', result, name, requestText, uncertain })
    if (!uncertain) remember(null)
    if (result.submissionStatus === 'CREATED') onCreated({ ...result, projectName: name, requestText })
  }
  async function submit(name, requestText) {
    if (!hasStoredAccessToken()) { setState(value => ({ ...value, error: loginRequired().message })); return false }
    if (pending.current || state.locked || !configuration.value || !name.trim() || !requestText.trim()) return false
    pending.current = true
    const sequence = ++current.current
    const key = crypto.randomUUID()
    remember(key)
    setState(value => ({ ...value, busy: true, error: '', name: name.trim(), requestText: requestText.trim() }))
    try {
      const result = await submitOfficeWork({ ...configuration.value, projectName: name.trim(), requestText: requestText.trim() }, key)
      if (mounted.current && sequence === current.current) accept(result, name.trim(), requestText.trim())
    } catch (error) {
      if (mounted.current && sequence === current.current) {
        const uncertain = ![400, 401, 403, 404, 409, 413, 415, 422, 429].includes(error.status)
        if (!uncertain) remember(null)
        setState(value => ({ ...value, busy: false, locked: uncertain, uncertain, error: uncertain
          ? '접수 결과를 확인하지 못했습니다. 다시 생성하지 말고 접수 상태를 확인해 주세요.' : error.message }))
      }
    } finally { pending.current = false }
    return false
  }
  async function recover() {
    if (pending.current || !requestId.current) return
    pending.current = true
    const sequence = ++current.current
    setState(value => ({ ...value, busy: true, error: '' }))
    try {
      const result = await lookupOfficeWork(requestId.current)
      if (mounted.current && sequence === current.current) accept(result, state.name, state.requestText)
    } catch (error) {
      if (mounted.current && sequence === current.current) setState(value => ({ ...value, busy: false, error: error.message }))
    } finally { pending.current = false }
  }
  function reset() {
    if (pending.current || state.uncertain) return false
    remember(null); current.current++; setState(empty)
    return true
  }
  return { ...state, submit, recover, reset, refreshApproval: () => refreshApproval(value => value + 1),
    configurationLoading: configuration.loading, configurationError: configuration.error,
    disabled: !hasToken || !configuration.value || state.busy || state.locked,
    notice: !hasToken ? '업무를 맡기려면 로그인해 주세요.'
      : state.uncertain ? '접수 결과를 확인 중입니다. 중복 생성은 차단되어 있습니다.'
        : configuration.loading ? '실행 서버의 준비 상태를 확인하고 있어요.'
          : configuration.error || '현재 회원가입 기능 개발을 지원합니다. 업무 시작을 누르면 실제 에이전트가 실행됩니다.' }
}
