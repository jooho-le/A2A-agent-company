import { useState } from 'react'
import { ModeSelector } from './AtelierSingleMode'
import { Icon, OfficeDesk } from './AtelierArtwork'
import { agentStates, employees, motionPortraits, portraits, progress, projectStatus } from './atelierMock'

export function Portrait({ employee, index, state = '' }) {
  const [failedSource, setFailedSource] = useState(null)
  const motion = motionPortraits[employee.id]
  const source = ({ active: motion.working[0], warning: motion.warning, done: motion.done })[state] || portraits[employee.id]
  return (
    <span className="portrait" data-person={employee.id}>
      <span className="portrait-number">STAFF / 0{index + 1}</span>
      {failedSource !== source ? <img src={source} alt={`${employee.name} 캐릭터`} onError={() => setFailedSource(source)} /> : (
        <svg className="person-placeholder" viewBox="0 0 120 130" aria-hidden="true"><circle cx="60" cy="39" r="24" /><path d="M9 134v-22c0-29 22-46 51-46s51 17 51 46v22Z" /></svg>
      )}
    </span>
  )
}

export function AgentCards({ project, profile = false, onEmployee }) {
  return agentStates(project).map((state, index) => {
    const employee = employees[index]
    return (
      <button key={employee.id} type="button" className={`agent-card ${state.state}${profile ? ' staff-profile' : ''}`} onClick={() => onEmployee(index)} aria-label={`${employee.name} ${employee.job} 직원 상세`}>
        <Portrait employee={employee} index={index} state={profile ? '' : state.state} />
        <span className="employee-body">
          <span className="employee-name-line"><span className="employee-name">{employee.name}</span>{!profile && <span className="agent-status">{state.label}</span>}</span>
          <span className="employee-role">{employee.job} · {employee.role} AI</span>
          {profile ? <><span className="employee-quote">{employee.quote}</span><span className="personality-tag">{employee.personality}</span></> : <span className="agent-task">{state.task}</span>}
          <span className="employee-link">{profile ? '직원 소개 보기' : '작업 자세히 보기'}<Icon name="chevron" /></span>
        </span>
      </button>
    )
  })
}

export function ProjectOptions({ projects, selectedId, onSelect, id }) {
  return <select id={id} value={selectedId} onChange={(event) => onSelect(event.target.value)}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
}

export function ProjectCard({ project, onSelect }) {
  const status = projectStatus(project)
  return (
    <button type="button" className="project-card" onClick={() => onSelect(project.id)} aria-label={`${project.name} 업무 보기`}>
      <span className="project-card-top"><span className={`project-monogram ${['flow', 'desk'].includes(project.id) ? project.id : ''}`}>{({ orbit: 'O', flow: 'F', desk: 'D' })[project.id] || '+'}</span><span className={`badge ${status.kind}`}>{status.text}</span></span>
      <h2>{project.name}</h2><p>{project.description}</p>
      <span className="project-card-meta"><span>의뢰인 · 주호 대표님</span><span>{project.time}</span></span>
      <span className="progress-meta"><span>프로젝트 진행</span><strong>{progress(project)}%</strong></span>
      <span className="progress-track"><span className="progress-fill" style={{ display: 'block', width: `${progress(project)}%` }} /></span>
      <span className="project-card-footer"><span>담당 직원 4명</span><Icon name="arrow" /></span>
    </button>
  )
}

export function ActivityList({ activities, projects }) {
  return <ol className="activities">{activities.map((activity, index) => {
    const employee = employees[activity.employee]
    return <li className="activity-item" key={`${activity.projectId}-${index}`}><span className="activity-avatar">{employee?.initial || '주'}</span><div><p className="activity-text"><strong>{employee ? `${employee.name} · ${employee.job}` : '주호 대표님'}</strong><br />{activity.text}</p><span className="activity-meta">{projects.find((project) => project.id === activity.projectId)?.name || '프로젝트'} · {activity.time}</span></div></li>
  })}</ol>
}

export function RequestForm({ mode = 'multi', onMode, modal = false, onCreate, onClose, deskRole = 'Developer' }) {
  const prefix = modal ? 'new' : 'request'
  function submit(event) {
    event.preventDefault()
    const form = event.currentTarget
    const name = form.elements.projectName.value.trim()
    const request = form.elements.request.value.trim()
    if (!name) { form.elements.projectName.focus(); return }
    if (!request) { form.elements.request.focus(); return }
    form.reset()
    onCreate(name, request)
  }
  const fields = <>
    <label className={modal ? 'field-label' : 'sr-only'} htmlFor={`${prefix}-name`}>{modal ? '프로젝트 이름' : '새 업무의 프로젝트 이름'}</label>
    <input id={`${prefix}-name`} name="projectName" className={modal ? 'field' : 'request-name'} placeholder={modal ? '예: 우리 팀의 업무 도구' : '프로젝트 이름'} maxLength={60} required autoComplete="off" />
    {modal ? <div className="blue-field"><label className="field-label" htmlFor="new-request">업무 요청</label><textarea id="new-request" name="request" placeholder="어떤 걸 만들고 싶으세요? 필요한 기능과 원하는 모습을 편하게 적어주세요." maxLength={1600} rows={5} required /><span>기획 담당 사슴이 요청을 받아 계획을 정리합니다.</span></div> : <textarea id="request-text" name="request" className="request-text" placeholder="어떤 걸 만들고 싶으세요? 필요한 기능과 원하는 모습을 편하게 적어주세요." maxLength={1600} rows={2} required />}
  </>
  if (modal) return <form id="new-form" onSubmit={submit}><div className="dialog-body">{fields}<p className="dialog-note">디자인 목업입니다. 실제 서버에 요청을 보내지 않습니다.</p></div><div className="dialog-footer"><button className="button" type="button" onClick={onClose}>취소</button><button className="button start" type="submit">업무 시작<Icon name="arrow" /></button></div></form>
  return <form className="request-composer" id="request-form" onSubmit={submit}><div className="composer-top"><label htmlFor="request-text"><Icon name="plan" />직원들에게 새 업무 맡기기</label><span>기획부터 결과 보고까지, 한 팀으로.</span></div>{onMode && <ModeSelector mode={mode} onMode={onMode} />}{fields}<div className="composer-office" data-scene-review={String(deskRole.includes('QA') || deskRole.includes('Security'))}><OfficeDesk role={deskRole} /></div><div className="composer-bottom"><p><span className="mini-avatar">사</span>기획 담당 <strong>사슴</strong>이 먼저 요청을 정리할게요.</p><button className="button start" type="submit">업무 시작<Icon name="arrow" /></button></div></form>
}
