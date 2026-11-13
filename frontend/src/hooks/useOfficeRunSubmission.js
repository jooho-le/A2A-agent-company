import { useEffect, useRef, useState } from 'react'
import { createAgentRun, getAgentRunScenarios } from '../api/agentRuns'
import { approvedRunSubmission, createRunSubmission, dispatchMessage } from '../api/agentRunSubmission'
import { hasStoredAccessToken, loginRequired } from '../api/productRunRequest'

// 파란색 요청창의 제출 상태만 관리합니다. Run 관측은 Dashboard의 기존 Hook이 담당합니다.
export default function useOfficeRunSubmission(approval, onCreated) {
  const submission = useRef(null)
  const pending = useRef(false)
  const [state, setState] = useState({ busy: false, locked: false, error: '', result: null, name: '', requestText: '' })
  let ready = false
  try { approvedRunSubmission(approval); ready = true } catch { /* 원본 설정은 화면에 노출하지 않습니다. */ }

  useEffect(() => {
    const runner = createRunSubmission({ listScenarios: getAgentRunScenarios, postRun: createAgentRun, onCreated })
    submission.current = runner
    return () => { runner.dispose(); submission.current = null }
  }, [onCreated])

  async function submit(name, requestText) {
    // UI의 disabled 여부와 별개로, 제출 직전에 확인하여 익명 Run 접수를 막습니다.
    if (!hasStoredAccessToken()) {
      setState(current => ({ ...current, error: loginRequired().message }))
      return false
    }
    const runner = submission.current
    if (!runner || pending.current || runner.blocked() || !name.trim() || !requestText.trim()) return false
    pending.current = true
    setState(current => ({ ...current, busy: true, error: '' }))
    try {
      // API에는 projectName 필드가 없으므로 사용자 입력을 requestText에 함께 전달합니다.
      // 명시적인 업무 시작 클릭이 실행 요청이며 별도 자동 제출은 없습니다.
      const result = await runner.submit({ approval, confirmed: true,
        requestText: `프로젝트: ${name.trim()}\n\n요구사항:\n${requestText.trim()}` })
      if (submission.current === runner) setState({ busy: false, locked: true, error: '', result, name: name.trim(), requestText: requestText.trim() })
    } catch (error) {
      if (submission.current === runner) setState(current => ({ ...current, busy: false, locked: runner.blocked(), error: error.message }))
    } finally { pending.current = false }
    // 실패·성공 모두 입력을 보존하고 목업 프로젝트를 만들지 않습니다.
    return false
  }

  const hasToken = hasStoredAccessToken()
  return { ...state, submit, disabled: !hasToken || !ready || state.busy || state.locked,
    notice: !hasToken ? '업무를 시작하려면 대표님 계정 메뉴에서 로그인해 주세요.'
      : !ready ? '등록된 시나리오와 승인된 공개 실행 설정이 준비되지 않아 업무를 시작할 수 없습니다.'
      : state.result ? dispatchMessage(state.result.dispatchStatus)
        : '업무 시작을 누르면 승인된 설정으로 실제 에이전트가 실행됩니다. API 비용이 발생할 수 있습니다.' }
}
