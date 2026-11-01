import { useEffect, useRef, useState } from 'react'
import { Icon, OfficeDesk } from './AtelierArtwork'
import SingleGiraffe from './SingleGiraffe'

const stages = ['기획', '개발', 'QA', '보안 검토']
const roles = ['planner', 'developer', 'qa', 'security']

function characterState(project) {
  if (!project.started) return 'intro'
  if (project.approval || project.step === 4) return 'done'
  return project.characterState || 'working'
}

const descriptions = ['요청과 작업 계획 정리', '계획에 따른 기능 구현', '기능 및 예외 상황 확인', '접근 권한과 정보 보호 검토']

export function ModeSelector({ mode, onMode }) {
  return <fieldset className="execution-modes"><legend>업무 수행 방식</legend><div className="execution-options">{[['multi', '멀티 에이전트', '네 직원이 역할을 나누어 협업'], ['single', '싱글 에이전트', '싱글 AI가 모든 단계를 순차적으로 수행']].map(([value, label, description]) => <button key={value} type="button" data-mode={value} aria-pressed={mode === value} onClick={() => onMode(value)}>{label}<small>{description}</small></button>)}</div><p className="execution-mode-note">화면 전환은 목업이며 실제 Agent Run을 생성하지 않습니다.</p></fieldset>
}

function stageState(project, index) {
  if (index < project.step) return 'done'
  if (index !== project.step || !project.started) return 'waiting'
  return characterState(project) === 'working' ? 'active' : characterState(project)
}

function SingleStages({ project }) {
  return <ol className="team-relay single-relay" aria-label="기린 한 명의 기획, 개발, QA, 보안 검토 순차 업무">{stages.map((stage, index) => {
    const state = stageState(project, index)
    const label = project.approval && index === 0 ? '승인 대기' : ({ done: '목업 완료', warning: '문제 발견 · 목업', active: '목업 진행 중', waiting: '대기' })[state]
    return <li className={`relay-node${state === 'active' ? ' is-working' : state === 'done' ? ' is-done' : ''}`} key={stage} data-state={state}><span className="relay-node-icon"><Icon name={['plan', 'code', 'check', 'shield'][index]} /></span><span className="relay-node-copy"><strong>{index + 1}. {stage}</strong><small>{label}</small></span></li>
  })}</ol>
}

function SingleActivityList({ project, full = false }) {
  const records = (full ? project.history : project.history.slice(-3)).slice().reverse()
  return <ol className="activities">{records.map((text, index) => <li className="activity-item" key={project.history.length - index}><span className="activity-avatar">기</span><div><p className="activity-text"><strong>기린 · 싱글 에이전트</strong><br />{text}</p><span className="activity-meta">{project.name} · 목업 기록 {project.history.length - index}</span></div></li>)}</ol>
}

// 멀티 모드와 분리된 메모리 상태입니다. 실제 API·파일 생성·검증은 실행하지 않습니다.
export default function AtelierSingleMode({ view, active, onMode, onView, newRequest, onNewHandled }) {
  const [projects, setProjects] = useState([{ id: 'single-demo', name: '싱글 에이전트 체험 프로젝트', description: '한 AI가 기획부터 보안 검토까지 순서대로 수행하는 목업입니다.', started: false, step: 0, approval: false, feedback: '', history: ['싱글 에이전트 목업을 준비했어요.'] }])
  const [selectedId, setSelectedId] = useState('single-demo')
  const [query, setQuery] = useState('')
  const [dialog, setDialog] = useState(null)
  const [message, setMessage] = useState('')
  const dialogRef = useRef(null)
  const triggerRef = useRef(null)
  const sequence = useRef(0)
  const project = projects.find((item) => item.id === selectedId)
  const portraitState = characterState(project)
  const roleIndex = Math.min(project.step, 3)
  const label = portraitState === 'intro' ? '업무 대기 · 목업' : project.approval ? '기획안 승인 대기' : project.step === 4 ? '목업 작업 완료' : `${stages[roleIndex]} · ${{ working: '목업 진행 중', warning: '문제 발견 · 목업', done: '단계 완료 · 목업' }[portraitState]}`
  useEffect(() => {
    if (!dialog && !newRequest) return
    const element = dialogRef.current
    const trigger = triggerRef.current || document.activeElement
    element.showModal()
    return () => { element.close(); if (trigger?.isConnected) trigger.focus({ preventScroll: true }) }
  }, [dialog, newRequest])
  function close() { setDialog(null); onNewHandled() }
  function open(type) { triggerRef.current = document.activeElement; setDialog(type) }
  function change(changes, record) {
    setProjects((items) => items.map((item) => item.id === selectedId ? { ...item, characterState: 'working', ...changes, history: [...item.history, record] } : item))
    setMessage('목업 상태만 변경했습니다. 실제 Agent는 실행하지 않았습니다.')
  }
  function create(event) {
    event.preventDefault()
    const form = event.currentTarget
    const name = form.elements.projectName.value.trim(), description = form.elements.request.value.trim()
    if (!name || !description) return
    const id = `single-${++sequence.current}`
    setProjects((items) => [{ id, name, description, started: true, step: 0, approval: false, feedback: '', history: ['새 요청을 받고 기획 목업을 시작했어요.'] }, ...items])
    setSelectedId(id); setDialog(null); form.reset(); onNewHandled(); onView('home')
    setMessage('한 AI에게 업무를 맡긴 목업입니다. 서버 요청은 전송하지 않았습니다.')
  }
  function advance() {
    if (!project.started) change({ started: true }, '싱글 AI가 기획 목업을 시작했어요.')
    else if (project.step === 0) change({ approval: true }, '기획안 확인을 요청했어요.')
    else change({ step: project.step + 1 }, `${stages[project.step]} 단계의 목업을 마쳤어요.`)
  }
  // 완료한 단계와 현재 목업 상태로 계산하며 실제 서버 진행률은 사용하지 않습니다.
  const amount = !project.started ? 0 : project.step === 4 ? 100 : project.step * 25 + (portraitState === 'done' ? 25 : 10)
  const cardState = portraitState === 'working' ? 'active' : portraitState === 'intro' ? 'waiting' : portraitState
  const task = portraitState === 'intro' ? '업무 요청을 기다리고 있어요. 기린이 네 단계를 모두 맡습니다.' : project.approval ? '기획안을 준비했어요. 승인하면 같은 기린이 개발을 이어갑니다.' : project.step === 4 ? '기획·개발·QA·보안 검토의 목업을 모두 마쳤어요.' : portraitState === 'warning' ? `${stages[roleIndex]} 단계에서 문제를 발견한 목업입니다. 내용을 확인하고 작업을 이어가세요.` : portraitState === 'done' ? `${stages[roleIndex]} 단계의 목업을 마쳤어요. 다음 단계도 같은 기린이 맡습니다.` : `${descriptions[roleIndex]} 작업을 진행하는 목업입니다.`
  const form = <form className="request-composer" onSubmit={create}><div className="composer-top"><label htmlFor="single-request-text"><Icon name="plan" />직원들에게 새 업무 맡기기</label><span>한 AI와 차근차근, 처음부터 끝까지.</span></div><ModeSelector mode="single" onMode={onMode} /><label className="sr-only" htmlFor="single-request-name">프로젝트 이름</label><input id="single-request-name" name="projectName" className="request-name" placeholder="프로젝트 이름" maxLength={60} required /><textarea id="single-request-text" name="request" className="request-text" placeholder="어떤 걸 만들고 싶으세요? 필요한 기능과 원하는 모습을 편하게 적어주세요." maxLength={1600} rows={2} required /><div className="composer-office"><OfficeDesk role="Single AI" /></div><div className="composer-bottom"><p>싱글 AI가 기획 → 개발 → QA → 보안 검토를 순차 수행합니다.</p><button type="submit" className="button start">목업 업무 시작<Icon name="arrow" /></button></div></form>
  const actor = <div className="agent-grid single-agent-grid"><button type="button" className={`agent-card single-agent-card ${cardState}`} onClick={() => open('profile')} aria-label="기린 싱글 에이전트 직원 상세">
    <SingleGiraffe role={roles[roleIndex]} state={portraitState} />
    <span className="employee-body"><span className="employee-name-line"><span className="employee-name">기린</span><span className="agent-status">{portraitState === 'intro' ? '업무 대기' : project.approval ? '승인 대기' : project.step === 4 ? '목업 완료' : { working: '목업 진행 중', warning: '문제 발견 · 목업', done: '단계 완료 · 목업' }[portraitState]}</span></span><span className="employee-role">싱글 에이전트 · 전체 업무 담당</span><span className="agent-task">{label}<br />{task}</span><span className="employee-link">작업 자세히 보기<Icon name="chevron" /></span></span>
  </button></div>
  const characterControls = <fieldset className="single-character-controls"><legend>캐릭터 상태 미리보기 · 목업</legend>{[['working', '작업 중'], ['warning', '문제 발견'], ['done', '단계 완료']].map(([state, text]) => <button key={state} type="button" className="button compact" aria-pressed={portraitState === state} disabled={!project.started || project.approval || project.step === 4} onClick={() => change({ characterState: state }, `${stages[roleIndex]} 캐릭터의 ‘${text}’ 목업 상태를 확인했어요.`)}>{text}</button>)}<p>이미지 확인용 목업 상태입니다. 실제 실행·검증 결과가 아닙니다.</p></fieldset>
  const selector = <label className="output-selector">프로젝트 <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{projects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
  return <div className="single-workspace" hidden={!active}>
    {view === 'home' && <>{form}<div className="office-grid"><div className="office-work">
      <section className="work-section" aria-label="선택한 싱글 프로젝트의 기린 업무">
        <div className="section-heading"><div><p className="eyebrow">SINGLE AGENT AT WORK</p><h2>우리 팀의 업무</h2></div><button type="button" className="text-button" onClick={() => open('history')}>업무 기록<Icon name="chevron" /></button></div>
        <div className="project-toolbar"><div className="project-label"><Icon name="folder" />{selector}</div><span className={`badge${project.approval || portraitState === 'warning' ? ' review' : portraitState === 'done' ? ' complete' : ''}`}>{label}</span></div>
        <p className="project-subtitle">{project.description}</p>
        <SingleStages project={project} />
        <div className="single-agent-layout">{actor}<div className="single-work-summary"><p className="eyebrow">ONE AGENT / FOUR STAGES</p><h3>기린 한 명이 네 단계를 이어갑니다.</h3><p>{task}</p>{characterControls}<p className="single-note">기획 → 개발 → QA → 보안 검토를 순차 수행합니다. 네 명의 독립 Agent가 실행되는 구조가 아닙니다.</p></div></div>
        <div className="team-report"><span className="report-mark"><Icon name="team" /></span><div><strong>{label}</strong><p>{task}</p></div><button type="button" className="text-button" disabled={project.step === 4 || project.approval} onClick={advance}><Icon name="arrow" /><span>{project.approval ? '승인 대기' : project.step === 4 ? '완료 상태' : !project.started ? '기획 목업 시작' : '다음 상태 보기'}</span></button></div>
        <div className="progress-meta"><span>프로젝트 진행률 · 목업</span><strong>{amount}%</strong></div><div className="progress-track" role="progressbar" aria-label="싱글 프로젝트 목업 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={amount}><div className="progress-fill" style={{ width: `${amount}%` }} /></div><p className="view-footnote">목업 상태로 계산한 진행률이며 실제 서버 실행 결과가 아닙니다.</p>
      </section>
      <section className="activity-section" aria-labelledby="single-activity-heading"><div className="section-heading"><h2 id="single-activity-heading">팀에서 온 업무 보고</h2><span className="section-note">MOCK UPDATES</span></div><SingleActivityList project={project} /></section>
    </div><aside className="owner-column">
      <section className="approval-panel" aria-label="싱글 목업 대표님 결재함"><div className="section-heading"><h2>대표님 결재함</h2><span className="count-pill">{project.approval ? 1 : 0}</span></div><div className="approval-paper"><Icon name={project.approval ? 'plan' : 'check'} /></div>{project.approval ? <><h3>대표님, 이 방향으로<br />진행해도 될까요?</h3><p className="approval-project">{project.name}</p><p className="approval-description">기린이 기획안을 정리했어요.<br />기능과 작업 순서를 확인해주세요.</p><button type="button" className="button start" onClick={() => open('plan')}>기획안 확인<Icon name="arrow" /></button><p className="approval-footer">기린 → 대표님<br />승인 후 같은 기린이 개발을 이어갑니다.</p></> : <><h3>확인할 일을<br />모두 마쳤어요.</h3><p className="approval-description">기린의 현재 업무는 직원 카드에서<br />확인할 수 있어요.</p><button type="button" className="button" onClick={() => open('profile')}>AI 업무 보기<Icon name="chevron" /></button><p className="approval-footer">표시된 진행 상황과 기획안은<br />디자인 시연용 목업입니다.</p></>}</section>
      <section className="brief-panel"><div className="section-heading"><h2>회사 한눈에 보기</h2><span className="section-note">MOCK DATA</span></div><dl className="company-stats"><div><dt>진행 중인 프로젝트</dt><dd>{projects.filter((item) => item.step < 4).length}<span>건</span></dd></div><div><dt>함께 일하는 직원</dt><dd>1<span>명</span></dd></div></dl><p>기린 한 명이 기획 → 개발 → QA →<br />보안 검토를 순서대로 맡습니다.</p></section>
    </aside></div></>}
    {view === 'projects' && <><div className="view-toolbar"><h2>싱글 에이전트 목업 프로젝트</h2><label className="search">검색<input aria-label="싱글 프로젝트 검색" value={query} onChange={(event) => setQuery(event.target.value)} /></label></div><div className="single-projects">{projects.filter((item) => `${item.name} ${item.description}`.includes(query)).map((item) => <article className="single-panel" key={item.id}><h3>{item.name}</h3><p>{item.description}</p><p>담당 AI 1개 · 디자인 목업</p><button className="button" onClick={() => { setSelectedId(item.id); onView('home') }}>업무 보기</button></article>)}</div>{!projects.some((item) => `${item.name} ${item.description}`.includes(query)) && <p className="single-note">일치하는 프로젝트가 없어요.</p>}</>}
    {view === 'team' && <section className="single-panel"><h2>한 AI, 네 가지 작업 단계.</h2><p>직원 네 명 중 한 명을 선택하는 방식이 아닙니다.</p>{actor}<button className="button" onClick={() => open('profile')}>AI 소개 보기</button></section>}
    {view === 'workflow' && <section className="single-panel"><p className="eyebrow">ONE AI / FOUR SEQUENTIAL STAGES</p><h2>한 AI가 끝까지 이어가는 업무.</h2><p>{project.name} · 모든 단계는 동일한 AI가 순서대로 수행합니다.</p>{actor}<SingleStages project={project} /><p className="single-note">단계와 진행 상태는 디자인 목업입니다. 실제 실행·테스트 결과가 아닙니다.</p></section>}
    {view === 'outputs' && <section className="single-panel"><h2>싱글 에이전트 결과물 보관함</h2>{selector}<p>실제 생성된 결과물이 없습니다. 목업 진행 상태를 실제 코드·테스트 성공 결과로 표시하지 않습니다.</p><button className="button" onClick={() => open('history')}>목업 업무 기록 보기</button></section>}
    {message && <p className="single-note" role="status">{message}</p>}
    {(dialog || newRequest) && <dialog ref={dialogRef} onCancel={(event) => { event.preventDefault(); close() }}><div className="dialog-header"><h2>{({ profile: '기린 · 싱글 에이전트', history: '싱글 에이전트 목업 기록', plan: '기획안 확인', revision: '기획 수정 요청', new: '싱글 에이전트에 새 업무 맡기기' })[newRequest ? 'new' : dialog]}</h2><button className="close-button" aria-label="창 닫기" onClick={close}>×</button></div><div className="dialog-body">{newRequest && <form className="single-form" onSubmit={create}><label htmlFor="single-new-name">프로젝트 이름</label><input id="single-new-name" className="field" name="projectName" required maxLength={60} /><label htmlFor="single-new-request">업무 요청</label><textarea id="single-new-request" name="request" placeholder="어떤 걸 만들고 싶으세요? 필요한 기능과 원하는 모습을 편하게 적어주세요." required maxLength={1600} /><button className="button start">목업 업무 시작</button></form>}{dialog === 'profile' && <><p className="single-detail">하나의 AI가 요청을 정리하고, 개발하고, QA와 보안 검토를 순서대로 수행합니다. 네 직원 중 한 명을 선택한 것이 아닙니다.</p><SingleStages project={project} /></>}{dialog === 'history' && <SingleActivityList project={project} full />}{dialog === 'plan' && <><p className="single-detail">{project.description}{project.feedback && `\n수정 요청: ${project.feedback}`}</p><p className="single-note">승인하면 동일한 AI의 개발 목업 단계로 넘어갑니다.</p><div className="single-actions"><button className="button" onClick={() => setDialog('revision')}>수정 요청</button><button className="button start" onClick={() => { change({ step: 1, approval: false }, '기획안을 승인하고 같은 AI가 개발을 시작했어요.'); setDialog(null) }}>승인하고 시작</button></div></>}{dialog === 'revision' && <form onSubmit={(event) => { event.preventDefault(); const text = event.currentTarget.elements.feedback.value.trim(); if (!text) return; change({ step: 0, approval: false, feedback: text }, '피드백을 받아 같은 AI가 기획을 다시 정리해요.'); setDialog(null) }}><label htmlFor="single-feedback">동일한 AI에게 전달할 피드백</label><textarea id="single-feedback" name="feedback" maxLength={1600} required /><button className="button start">수정 요청 전달</button></form>}<p className="single-note">디자인 시연용 목업이며 실제 Agent API는 연결되어 있지 않습니다.</p></div></dialog>}
  </div>
}
