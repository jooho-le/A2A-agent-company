import { useEffect, useId, useRef, useState } from 'react'
import { humanAgents } from './humanAgents'
import { managementDisplay } from './office3dOrchestrator'
import { buildOfficeRunPopups } from './officeRunPopups'
import { roleLabel } from './roleLabels'
import { pendingOfficeSubmissionKey } from '../../hooks/pendingOfficeSubmission'
import { Icon } from './AtelierArtwork'
import './RunWorkPanel.css'

const order = [4, 0, 1, 2, 3]
const purposes = ['만들 기능과 순서를 정리해요.', '계획에 맞게 기능을 구현해요.',
  '요구한 대로 작동하는지 확인해요.', '정보 보호와 접근 권한을 점검해요.', '직원에게 일을 전달하고 전체 진행을 관리해요.']

function StopDialog({ onClose, onConfirm }) {
  const ref = useRef(null)
  const title = useId()
  useEffect(() => {
    const node = ref.current, previous = document.activeElement
    node.showModal()
    return () => { node.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  return <dialog className="run-stop-dialog" ref={ref} aria-labelledby={title} onCancel={onClose}>
    <h2 id={title}>이 작업을 중단할까요?</h2><p>현재 실행은 종료돼요. 이미 만든 결과는 남아요.</p>
    <p>다시 작업하려면 새 요청을 시작해야 해요.</p>
    <div><button type="button" className="button" onClick={onClose} autoFocus>계속 작업하기</button>
      <button type="button" className="button run-stop-confirm" onClick={onConfirm}>작업 중단</button></div>
  </dialog>
}

function ReplyEditor({ runId, role, target, controls }) {
  const owner = pendingOfficeSubmissionKey()
  const key = owner ? `${owner}.reply.${runId}.${role}` : null
  const [text, setText] = useState(() => {
    try { return key ? sessionStorage.getItem(key) || '' : '' } catch { return '' }
  })
  const id = useId()
  const canReply = target?.replyMode === 'ANSWER'
  const canAnnotate = !target && controls.data?.canAnnotate === true
  const pending = controls.replyPending || controls.annotatePending
  function change(value) {
    setText(value)
    try { if (key) value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key) } catch { /* Keep the draft in memory. */ }
  }
  return <form className="run-reply" onSubmit={event => { event.preventDefault(); if (!text.trim()) return
    if (canReply) controls.reply(text.trim(), target.workflowStepId)
    else if (canAnnotate) controls.annotate(text.trim()) }}>
    <label htmlFor={id}>{target ? '직원에게 알려주세요' : '작업에 설명 보충하기'}</label>
    <textarea id={id} value={text} onChange={event => change(event.target.value)} maxLength={canReply ? 8000 : 4000}
      placeholder="예: 요청한 API는 JSON으로 사용하려고 해요." disabled={pending} />
    {(canReply || canAnnotate) ? <button type="submit" className="button start" disabled={!text.trim() || pending || controls.cancelPending}>
      {pending ? '전달 상태 확인 중…' : canReply ? '답변하고 이어가기' : '추가 설명 전달'}<Icon name="arrow" /></button>
      : <p className="run-draft-hint">현재 단계에는 설명을 전달할 수 없어요. 확인 질문에 답하거나 새 작업을 시작해주세요.</p>}
    {canAnnotate && <p className="run-draft-hint">현재 요청 안의 보충 설명을 다음 직원 작업에 전달해요. 새 기능이나 범위 변경은 새 작업으로 시작해주세요.</p>}
  </form>
}

export default function RunWorkPanel({ runId, projectName, displayState, controls, selectedRole, onSelectRole, onResults, onNew }) {
  const [confirmStop, setConfirmStop] = useState(false)
  const [memoOpen, setMemoOpen] = useState(false)
  const data = controls.data
  const final = [displayState.run?.status, data?.status].some(status => ['FINISHED', 'ABORTED'].includes(status))
  const requests = !final && ['WAITING_INPUT', 'HUMAN_REVIEW'].includes(data?.status) ? data?.requests || [] : []
  const role = selectedRole || requests[0]?.agentRole || displayState.agents.find(row => row.displayState === 'working')?.agentRole || 'ORCHESTRATOR'
  const index = humanAgents.findIndex(person => person.id.toUpperCase() === role)
  const safeIndex = index < 0 ? 4 : index
  const person = humanAgents[safeIndex]
  const rows = [...displayState.agents, managementDisplay(displayState)]
  const popups = buildOfficeRunPopups(displayState, rows, { ready: false, current: [] })
  const row = rows[safeIndex], popup = popups[safeIndex]
  const target = requests.find(item => item.agentRole === role)
  const failures = data?.failures || []
  const failure = failures.find(item => item.agentRole === role) || (role === 'ORCHESTRATOR' ? failures[0] : null)
  const command = Object.values(controls.commands).sort((a, b) =>
    String(b.createdAt || '').localeCompare(String(a.createdAt || '')))[0]
  const receiptMessage = command?.status === 'SUCCEEDED' ? command.action === 'CANCEL' ? '작업 중단이 확인됐어요.'
    : command.action === 'ANNOTATE' ? '추가 설명을 접수했어요. 아래에서 전달 상태를 확인해주세요.' : '답변 전달 처리가 끝났어요. 최신 작업 내용을 확인해주세요.'
    : command?.status === 'REJECTED' ? final ? '이 작업은 이미 종료됐어요. 답변을 이어 보낼 수 없어요.' : '진행 상태가 바뀌었어요. 최신 내용을 확인하고 다시 선택해주세요.'
      : command?.status === 'UNKNOWN' ? command.errorCode ? '서버가 처리를 확신할 수 없어 관리자 확인이 필요해요. 같은 요청을 다시 보내지 않아요.'
        : '처리 결과를 확인해야 해요. 같은 요청을 다시 보내지 않고 확인 중이에요.'
        : command ? command.action === 'CANCEL' ? '직원들의 중단 확인을 기다리고 있어요…'
          : command.action === 'ANNOTATE' ? '추가 설명의 접수 상태를 확인하고 있어요…' : '답변을 전달하고 이어서 진행하고 있어요…' : ''

  return <section className="run-work-panel" aria-label="직원 작업 내용과 작업 제어">
    <header><div>{projectName && <h3>{projectName}</h3>}</div>
      <div className="run-header-actions"><button className="run-refresh" type="button" onClick={controls.refresh} aria-label="직원 작업 새로 확인"><Icon name="clock" /></button>
        {!final && <button type="button" className="button run-header-stop" disabled={!data?.canCancel || controls.cancelPending} onClick={() => setConfirmStop(true)}>
          {controls.cancelPending ? '중단 확인 중…' : '작업 중단'}</button>}</div></header>
    {!!requests.length && <div className="run-attention" role="status">{requests.map(item => <button type="button" key={item.workflowStepId} onClick={() => onSelectRole(item.agentRole)}>
      <span />{roleLabel(item.agentRole)} 담당이 확인을 기다려요<Icon name="chevron" /></button>)}</div>}
    <div className="run-employee-picker" role="group" aria-label="작업을 살펴볼 직원">{order.map(i => <button key={humanAgents[i].id} type="button"
      aria-pressed={i === safeIndex} data-state={rows[i]?.displayState} onClick={() => onSelectRole(humanAgents[i].id.toUpperCase())}>
      <img src={humanAgents[i].portrait} alt="" /><span>{roleLabel(humanAgents[i].id)}</span><i aria-hidden="true" />
    </button>)}</div>
    <article className="run-person-work" data-state={failure?.recovering ? 'waiting' : row?.displayState}>
      <div className="run-person-heading"><strong>{roleLabel(person.id)}</strong><span>{failure?.recovering ? '수정·재검사 중' : row?.label || '상태 확인 중'}</span></div>
      {!target && <p className="run-person-purpose">{purposes[safeIndex]}</p>}
      {failure && !target && <div className="run-person-result" role={failure.recovering ? 'status' : 'alert'}>
        <h4>{failure.recovering ? '수정·재검사를 진행 중이에요.' : failure.message}</h4><p>{failure.instruction}</p>
        {!!failure.validationIssues?.length && <details><summary>검사 오류 자세히</summary><ul>{failure.validationIssues.slice(0, 4).map((issue, i) =>
          <li key={i}>{issue.reason}{issue.fieldPath && <small> ({issue.fieldPath})</small>}</li>)}</ul></details>}
        {!failure.recovering && failure.nextAction === 'CHECK_PROVIDER' && <button type="button" className="button" onClick={controls.refresh}>중단 원인 다시 확인</button>}
      </div>}
      {popup && !target && !failure && <div className="run-person-result"><h4>{popup.title}</h4>{popup.source === 'report' ? <p>{popup.detail}</p>
        : <details><summary>현재 작업 자세히</summary><p>{popup.detail}</p></details>}</div>}
      {target && <div className="run-question"><strong>{target.reason}</strong><ul>{target.questions.map((question, i) => <li key={i}>{question}</li>)}</ul>
        {target.replyMode === 'NEW_RUN' && <><p>이 작업은 새 요청으로 시작해야 해요.</p><button type="button" className="button" onClick={onNew}>새 작업 만들기<Icon name="arrow" /></button></>}
        {target.replyMode === 'UNAVAILABLE' && <button className="button" type="button" onClick={controls.refresh}>질문 다시 확인</button>}
      </div>}
      {(target?.replyMode === 'ANSWER' || (memoOpen && !final && !target)) && <ReplyEditor key={runId + ':' + (target?.workflowStepId || role)} runId={runId} role={role} target={target} controls={controls} />}
      {target && popup && <details className="run-waiting-work"><summary>이 직원의 작업 내용</summary><p>{popup.detail}</p></details>}
    </article>
    {receiptMessage && <p className="run-command-notice" role="status">{receiptMessage}</p>}
    {controls.error && <div className="run-control-error" role="alert"><p>{controls.error}</p><button type="button" className="button" onClick={controls.refresh}>처리 상태 확인</button></div>}
    {data?.continuationUnavailable && !target && <p className="run-control-error" role="alert">{data.continuationUnavailable}</p>}
    {!!data?.feedback?.length && <details className="run-waiting-work"><summary>전달한 추가 설명</summary>{data.feedback.map(note => <p key={note.feedbackId}>
      {note.text}<br /><small>{note.status === 'INCLUDED' ? '직원 작업 입력에 포함됨' : note.status === 'NOT_APPLIED' ? '다음 단계 없이 종료되어 전달되지 않음' : '다음 직원 작업에 전달 대기'}</small></p>)}</details>}
    {!data && !controls.error && <p className="run-command-notice" role="status">직원과 연결하고 있어요…</p>}
    <footer className="run-work-actions">
      <button type="button" className="button" onClick={onResults}><Icon name="file" />만든 결과</button>
      {!final && !target && data?.canAnnotate && <button type="button" className="button" aria-expanded={memoOpen} onClick={() => setMemoOpen(value => !value)}><Icon name="plan" />추가 의견</button>}
      {(final || failures.some(item => !item.recovering && item.nextAction === 'REVIEW_OR_NEW_RUN') || data?.continuationUnavailable) && <button type="button" className="button start" onClick={onNew}>새 작업 만들기</button>}
    </footer>
    {confirmStop && <StopDialog onClose={() => setConfirmStop(false)} onConfirm={() => { setConfirmStop(false); controls.cancel() }} />}
  </section>
}
