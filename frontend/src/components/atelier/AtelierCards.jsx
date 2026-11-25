import { useState } from 'react'
import { ModeSelector } from './ModeSelector'
import { StudioObject } from './StudioVisuals'
import { roleLabel } from './roleLabels'
import { Icon, OfficeDesk } from './AtelierArtwork'
import { agentStates, employees, motionPortraits, portraits, progress, projectStatus } from './atelierMock'

export function Portrait({ employee, state = '' }) {
  const [failedSource, setFailedSource] = useState(null)
  const motion = motionPortraits[employee.id]
  const source = ({ active: motion.working[0], warning: motion.warning, done: motion.done })[state] || portraits[employee.id]
  return (
    <span className="portrait" data-person={employee.id}>
      {employee.leadership && <span className="leader-portrait-badge">{employee.leadership.badge}</span>}
      {source && failedSource !== source ? <img src={source} alt={`${employee.name} 캐릭터`} onError={() => setFailedSource(source)} /> : (
        <svg className="person-placeholder" viewBox="0 0 120 130" aria-hidden="true"><circle cx="60" cy="39" r="24" /><path d="M9 134v-22c0-29 22-46 51-46s51 17 51 46v22Z" /></svg>
      )}
    </span>
  )
}

export function EmployeeCard({ employee, index, state, profile = false, onEmployee, portrait }) {
  return (
      <button key={employee.id} type="button" className={`agent-card ${state.state}${profile ? ' staff-profile' : ''}`} data-lead={Boolean(employee.leadership)} data-studio-tilt="" data-palette={index % 5} onClick={() => onEmployee(index)} aria-label={`${employee.name} ${employee.job} 직원 상세`}>
        {portrait || <Portrait employee={employee} index={index} state={profile ? '' : state.state} />}
        <span className="employee-body">
          <span className="employee-name-line"><span className="employee-name">{profile ? roleLabel(employee.id, employee.name) : employee.name}</span>{!profile && <span className="agent-status">{state.label}</span>}</span>
          {profile ? <span className="employee-quote">{employee.responsibilities?.[0] || employee.purpose}</span> : <><span className="employee-role">{employee.roleLabel || `${employee.job} · ${employee.role} AI`}</span><span className="agent-task">{state.task}</span></>}
          <span className="employee-link">{profile ? '소개 보기' : '자세히'}<Icon name="chevron" /></span>
        </span>
      </button>
  )
}

export function AgentCards({ project, profile = false, onEmployee, agents }) {
  const cards = agents || agentStates(project).map((state, index) => ({ state, employee: employees[index] }))
  return cards.map((card, index) => <EmployeeCard key={card.employee.id} {...card} index={index} profile={profile} onEmployee={onEmployee} />)
}

export function ProjectOptions({ projects, selectedId, onSelect, id }) {
  return <select id={id} value={selectedId} onChange={(event) => onSelect(event.target.value)}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
}

export function ProjectCard({ project, onSelect, status = project.status || projectStatus(project), amount = project.actual ? null : progress(project), staffCount = project.actual ? project.agents.filter(agent => agent.assigned).length : 5, palette = 0 }) {
  const interactive = !project.stored || typeof onSelect === 'function'
  const Card = interactive ? 'button' : 'article'
  return (
    <Card {...(interactive ? { type: 'button', onClick: () => onSelect(project.id) } : {})} className="project-card" data-studio-tilt="" data-palette={palette % 5} aria-label={project.stored ? `${project.name} 상세 보기` : `${project.name} 업무 보기`}>
      <span className="studio-project-cover" aria-hidden="true"><StudioObject kind="folder" /><span className="studio-cover-grid" /></span>
      <span className="project-card-top"><span className={`project-monogram ${['flow', 'desk'].includes(project.id) ? project.id : ''}`}>{({ orbit: 'O', flow: 'F', desk: 'D' })[project.id] || '+'}</span><span className={`badge ${status.kind}`}>{status.text}</span></span>
      <h2>{project.name}</h2>{!project.stored && <p>{project.description}</p>}
      <span className="project-card-meta"><span>{project.stored ? <>생성일 · <time dateTime={project.createdAt}>{project.time}</time></> : project.time}</span></span>
      {project.actual ? !project.stored && <span className="project-agent-states">{project.agents.map(agent => <span key={agent.role}>{roleLabel(agent.role, agent.name)} · {agent.label}</span>)}</span> : <><span className="progress-meta"><span>프로젝트 진행</span><strong>{amount}%</strong></span>
      <span className="progress-track"><span className="progress-fill" style={{ display: 'block', width: `${amount}%` }} /></span></>}
      <span className="project-card-footer">{project.stored ? <span>{interactive ? '프로젝트 열기 →' : '저장된 프로젝트'}</span> : <><span>담당 직원 {staffCount}명</span><Icon name="arrow" /></>}</span>
    </Card>
  )
}

export function ActivityList({ activities, projects, staff = employees }) {
  return <ol className="activities">{activities.map((activity, index) => {
    const employee = staff[activity.employee]
    return <li className="activity-item" key={`${activity.projectId}-${index}`}><span className="activity-avatar">{employee?.initial || 'A'}</span><div><p className="activity-text"><strong>{employee ? `${employee.name} · ${employee.job}` : '대표님'}</strong><br />{activity.text}</p><span className="activity-meta">{projects.find((project) => project.id === activity.projectId)?.name || '프로젝트'} · {activity.time}</span></div></li>
  })}</ol>
}

export function RequestForm({ mode = 'multi', onMode, modal = false, onCreate, onClose, submission = null, deskRole = 'Developer', planner = employees[0] }) {
  const prefix = modal ? 'new' : mode === 'single' ? 'single-request' : 'request'
  function rememberDraft(event) {
    const form = event.currentTarget.form
    submission?.onDraftChange?.({ name: form.elements.projectName.value, requestText: form.elements.request.value })
  }
  async function submit(event) {
    event.preventDefault()
    if (submission?.disabled) return
    const form = event.currentTarget
    const name = form.elements.projectName.value.trim()
    const request = form.elements.request.value.trim()
    if (!name) { form.elements.projectName.focus(); return }
    if (!request) { form.elements.request.focus(); return }
    const accepted = await onCreate(name, request)
    if (!submission && accepted !== false) form.reset()
  }
  const fields = <>
    <label className={modal ? 'field-label' : 'sr-only'} htmlFor={`${prefix}-name`}>{modal ? '프로젝트 이름' : '새 업무의 프로젝트 이름'}</label>
    <input id={`${prefix}-name`} name="projectName" className={modal ? 'field' : 'request-name'} placeholder={modal ? '예: 우리 팀의 업무 도구' : '프로젝트 이름'} maxLength={60} required autoComplete="off" disabled={submission?.busy || submission?.locked} defaultValue={submission?.draft?.name || ''} onInput={rememberDraft} />
    {modal ? <div className="blue-field"><label className="field-label" htmlFor={`${prefix}-request`}>업무 요청</label><textarea id={`${prefix}-request`} name="request" placeholder="예: 할 일을 추가하고 완료 표시할 수 있는 웹사이트를 만들어줘" maxLength={1600} rows={5} required /><span>{planner.name} · 요청을 받아 계획을 정리합니다.</span></div> : <textarea id={`${prefix}-text`} name="request" className="request-text" placeholder="예: 할 일을 추가하고 완료 표시할 수 있는 웹사이트를 만들어줘" maxLength={1600} rows={2} required disabled={submission?.busy || submission?.locked} defaultValue={submission?.draft?.requestText || ''} onInput={rememberDraft} />}
  </>
  if (modal) return <form id="new-form" onSubmit={submit}><div className="dialog-body">{fields}<p className="dialog-note">디자인 목업입니다. 실제 서버에 요청을 보내지 않습니다.</p></div><div className="dialog-footer"><button className="button" type="button" onClick={onClose}>취소</button><button className="button start" type="submit">{mode === 'single' ? '목업 업무 시작' : '업무 시작'}<Icon name="arrow" /></button></div></form>
  const loginFirst = typeof submission?.onLogin === 'function'
  const problem = submission?.error || submission?.configurationError
  const needsStatus = submission?.busy || submission?.uncertain || submission?.configurationLoading
  return <form className="request-composer" id={`${prefix}-form`} onSubmit={submit}>
    <div className="composer-top"><label htmlFor={`${prefix}-text`}><Icon name="plan" />무엇을 만들까요?</label></div>
    {submission && !submission.locked && <p className="request-scope">만들고 싶은 웹사이트와 필요한 기능을 알려주세요.</p>}
    {onMode && <ModeSelector mode={mode} onMode={onMode} />}
    {fields}
    {!submission && <div className="composer-office"><OfficeDesk role={deskRole} /></div>}
    <div className="composer-bottom">
      <button className="button start" type={loginFirst ? 'button' : 'submit'} disabled={loginFirst ? false : submission?.disabled} onClick={loginFirst ? submission.onLogin : undefined}>
        {loginFirst ? '로그인하고 시작' : submission?.busy ? '요청 보내는 중…' : submission?.result?.submissionStatus === 'CREATED' ? '요청 보냄' : submission?.uncertain ? '접수 확인 필요' : mode === 'single' ? '목업 업무 시작' : '만들기 시작'}<Icon name="arrow" />
      </button>
    </div>
    {submission && <div className="composer-feedback">
      {submission.message && !submission.finished && <p role="status">{submission.message}</p>}
      {needsStatus && !submission.finished && <p role="status">{submission.notice}</p>}
      {problem && <p role="alert">{problem}</p>}
      {submission.uncertain && <button type="button" className="button" disabled={submission.busy} onClick={submission.recover}>접수 상태 확인</button>}
      {submission.uncertain && submission.retire && <button type="button" className="button" disabled={submission.busy} onClick={submission.retire}>이전 요청 확인하고 다시 입력</button>}
      {submission.configurationError && <button type="button" className="button" onClick={submission.refreshApproval}>다시 연결</button>}
      {submission.workflowStatus && <p role="status">{submission.workflowStatus}</p>}
    </div>}
  </form>
}
