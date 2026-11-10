import { useEffect, useRef, useState } from 'react'
import { createAgentRun, getAgentRunScenarios } from '../../api/agentRuns'
import { approvedRunSubmission, createRunSubmission, dispatchMessage } from '../../api/agentRunSubmission'

// 신뢰된 상위 연결부가 공급할 공개 승인값만 받습니다. JSON 업로드·키 입력·자동 승인 기능은 없습니다.
// 현재 앱은 approval을 공급하지 않으므로 실제 POST는 기본 차단 상태입니다.
export default function AgentRunCreate({ approval = null, onCreated }) {
  const submission = useRef(null)
  const [requestText, setRequestText] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [locked, setLocked] = useState(false)
  let approved = false
  try { approvedRunSubmission(approval); approved = true } catch { /* 미승인 설정은 표시하거나 전송하지 않습니다. */ }

  useEffect(() => {
    const runner = createRunSubmission({ listScenarios: getAgentRunScenarios, postRun: createAgentRun, onCreated })
    submission.current = runner
    return () => { runner.dispose(); submission.current = null }
  }, [onCreated])

  async function submit(event) {
    event.preventDefault()
    const runner = submission.current
    if (!approved || !confirmed || busy || result || !runner || runner.blocked()) return
    setBusy(true); setError('')
    try {
      const created = await runner.submit({ approval, requestText, confirmed })
      if (submission.current === runner) { setResult(created); setLocked(true) }
    } catch (failure) {
      if (submission.current === runner) { setError(failure.message); setLocked(runner.blocked()) }
    } finally {
      if (submission.current === runner) setBusy(false)
    }
  }

  return <details className="agent-run-monitor">
    <summary>실제 멀티 에이전트 Run 생성</summary>
    <p className="arm-intro">목업 업무 생성과 별개입니다. 승인된 설정으로 실제 실행을 요청하면 API 비용이 발생할 수 있습니다.</p>
    {!approved && <p className="arm-validation" role="status">승인된 공개 실행 설정이 제공되지 않아 실제 생성이 차단되어 있습니다.</p>}
    <form className="arm-form" onSubmit={submit}>
      <fieldset disabled={!approved || busy || locked}>
        <label className="field-label" htmlFor="actual-run-request">실제 업무 요청</label>
        <textarea id="actual-run-request" className="feedback-textarea" rows={3} maxLength={8000} required value={requestText}
          onChange={event => setRequestText(event.target.value)} placeholder="등록된 시나리오에서 수행할 업무를 입력하세요. 비밀정보는 입력하지 마세요." />
        <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> 승인된 설정으로 실제 Run 1건을 생성하겠습니다.</label>
        <button className="button" type="submit" disabled={!confirmed || !requestText.trim()}>{busy ? '생성 요청 중…' : '실제 Run 생성'}</button>
      </fieldset>
    </form>
    {error && <p className="arm-validation" role="alert">{error}</p>}
    {result && <p className="arm-note" role="status">{dispatchMessage(result.dispatchStatus)} Run ID: <code>{result.run.runId}</code></p>}
  </details>
}
