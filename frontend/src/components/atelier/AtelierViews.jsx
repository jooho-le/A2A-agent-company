import AgentLeadership from './AgentLeadership'
import { Icon, OfficeDesk } from './AtelierArtwork'
import { ActivityList, AgentCards, ProjectCard, ProjectOptions, RequestForm } from './AtelierCards'
import { agentStates, employees, progress, projectStatus } from './atelierMock'

export function HomeView({ mode, onMode, projects, project, activities, onSelect, onCreate, onNew, onEmployee, onPlan, onWorklog, officeScene, presentation = {} }) {
  const status = presentation.status || projectStatus(project)
  const amount = presentation.amount ?? progress(project)
  const prefix = mode === 'single' ? 'single-' : ''
  const planner = presentation.staff?.[0] || employees[0]
  const roles = agentStates(project).flatMap((state, index) => state.state === 'active' ? [employees[index].role] : [])
  const deskRole = presentation.deskRole || roles.join(' + ') || (project.phase === 3 ? 'All done' : 'Plan review')
  const pending = projects.filter((item) => item.needsApproval)
  const approval = pending.find((item) => item.id === project.id) || pending[0]
  return <>
    <RequestForm mode={mode} onMode={onMode} onCreate={onCreate} deskRole={deskRole} planner={planner} />
    <div className="office-grid"><div className="office-work">
      <section className="work-section" aria-label="선택한 프로젝트의 직원 업무">
        <div className="section-heading"><div><p className="eyebrow">TEAM AT WORK</p><h2>우리 팀의 업무</h2></div><button type="button" className="text-button" id={`${prefix}open-worklog`} onClick={onWorklog}>업무 기록<Icon name="chevron" /></button></div>
        <div className="project-toolbar"><div className="project-label"><Icon name="folder" /><label className="sr-only" htmlFor={`${prefix}project-select`}>진행 상황을 볼 프로젝트</label><ProjectOptions id={`${prefix}project-select`} projects={projects} selectedId={project.id} onSelect={onSelect} /></div><span className={`badge ${status.kind}`}>{status.text}</span></div>
        <p className="project-subtitle">{project.description}</p>
        {mode === 'multi' ? officeScene : <div className="agent-grid"><AgentCards project={project} agents={presentation.agents} onEmployee={onEmployee} /></div>}
        <div className="progress-meta"><span>{presentation.progressLabel || '프로젝트 진행률'}</span><strong>{amount}%</strong></div><div className="progress-track" role="progressbar" aria-label={presentation.progressLabel || '선택한 프로젝트 진행률'} aria-valuemin={0} aria-valuemax={100} aria-valuenow={amount}><div className="progress-fill" style={{ width: `${amount}%` }} /></div>
      </section>
      {mode !== 'multi' && <section className="activity-section" aria-labelledby={`${prefix}activity-heading`}><div className="section-heading"><h2 id={`${prefix}activity-heading`}>팀에서 온 업무 보고</h2><span className="section-note">RECENT UPDATES</span></div><ActivityList projects={projects} activities={activities.slice(0, 3)} staff={presentation.staff} /></section>}
    </div><aside className="owner-column">
      <section className="approval-panel" aria-label="대표님 결재함"><div className="section-heading"><h2>대표님 결재함</h2><span className="count-pill">{pending.length}</span></div><div className="approval-paper"><Icon name={approval ? 'plan' : 'check'} /></div>
        {approval ? <><h3>대표님, 이 방향으로<br />진행해도 될까요?</h3><p className="approval-project">{approval.name}</p><p className="approval-description">{planner.name}의 기획안이 준비됐어요.<br />기능과 작업 순서를 확인해주세요.</p><button type="button" className="button start" onClick={() => onPlan(approval.id)}>기획안 확인<Icon name="arrow" /></button><p className="approval-footer">{planner.job} {planner.name} → 대표님<br />{mode === 'single' ? '승인 후 같은 기린이 개발을 이어갑니다.' : '승인 후 개발 에이전트에게 전달됩니다.'}</p></> : <><h3>확인할 일을<br />모두 마쳤어요.</h3><p className="approval-description">직원들이 다음 작업을 이어갑니다.<br />새 아이디어가 떠오르면 맡겨주세요.</p><button type="button" className="button" onClick={onNew}>새 업무 맡기기<Icon name="plus" /></button><p className="approval-footer">진행 시연은 왼쪽 3D 오피스에서<br />언제든 확인할 수 있어요.</p></>}
      </section>
      <section className="brief-panel"><div className="section-heading"><h2>회사 한눈에 보기</h2><span className="section-note">TODAY</span></div><dl className="company-stats"><div><dt>진행 중인 프로젝트</dt><dd>{projects.filter((item) => item.phase !== 3).length}<span>건</span></dd></div><div><dt>함께 일하는 직원</dt><dd>{presentation.staff?.length || 4}<span>명</span></dd></div></dl><p>{mode === 'single' ? <>기린 한 명이 기획 → 개발 → QA →<br />보안 검토를 순서대로 맡습니다.</> : <>요청을 맡기면 기획 → 개발 →<br />품질·보안 검토 순서로 진행해요.</>}</p></section>
    </aside></div>
  </>
}

export function ProjectsView({ projects, search, onSearch, onSelect, presentation = {} }) {
  const matches = projects.filter((project) => `${project.name} ${project.description}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  return <><div className="view-toolbar"><p className="toolbar-note">우리 회사가 함께 만드는 일 <strong>{String(projects.length).padStart(2, '0')}</strong></p><div className="search"><Icon name="search" /><label className="sr-only" htmlFor={presentation.searchId || 'atelier-search'}>프로젝트 검색</label><input id={presentation.searchId || 'atelier-search'} type="search" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="프로젝트 이름 검색" autoComplete="off" /></div></div><div className="project-grid">{matches.length ? matches.map((project) => <ProjectCard key={project.id} project={project} onSelect={onSelect} {...presentation.projectMeta?.(project)} />) : <div className="empty-state"><Icon name="search" /><h2>일치하는 프로젝트가 없어요.</h2><p>검색어를 바꾸거나 새 업무를 만들어보세요.</p></div>}</div></>
}

export function TeamView({ project, onEmployee, presentation = {} }) {
  return <><div className="team-banner"><div><span className="eyebrow">SMALL TEAM. CLEAR ROLES.</span><h2>{presentation.teamTitle || '대표님과 함께 일할 네 명의 직원.'}</h2><p>{presentation.teamDescription || '기획 총괄이 방향을 정리하고, 개발·품질·보안 에이전트가 전문 분야를 맡습니다.'}</p></div><span className="headcount"><strong>{String(presentation.staff?.length || 4).padStart(2, '0')}</strong>AI EMPLOYEES</span></div><AgentLeadership /><div className="employee-grid"><AgentCards project={project} agents={presentation.agents} profile onEmployee={onEmployee} /></div><p className="view-footnote">직원 이름·성격·진행 상태는 디자인 시연용 목업입니다.</p></>
}

export function OutputsView({ projects, project, onSelect, onFile, onDownload, presentation = {} }) {
  return <><div className="view-toolbar"><div><h2>{project.name}</h2><p className="toolbar-note">직원들이 정리한 문서와 코드를 확인하세요.</p></div><label className="output-selector"><span className="sr-only">결과물을 볼 프로젝트</span><ProjectOptions projects={projects} selectedId={project.id} onSelect={onSelect} /></label></div><div className="output-grid">{project.files.map((file, index) => {
    const employee = presentation.staff?.[0] || (file.endsWith('.md') ? employees[0] : file.includes('test') ? employees[2] : employees[1])
    return <article className="output-card" key={file}><div className="output-card-top"><span className="file-icon"><Icon name={file.endsWith('.md') ? 'file' : 'code'} /></span><span className="file-type">{file.endsWith('.md') ? 'DOCUMENT' : file.includes('test') ? 'TEST NOTES' : 'SOURCE CODE'}</span></div><h3>{file.split('/').pop()}</h3><p className="file-path">{file}</p><div className="file-owner"><span className="mini-avatar">{employee.initial}</span>{employee.name} · {employee.job}<span> / 예시 파일</span></div><div className="output-actions"><button type="button" className="button file-open" onClick={() => onFile(index)}>파일 보기<Icon name="chevron" /></button><button type="button" className="button" onClick={() => onDownload(index)} aria-label={`${file.split('/').pop()} 예시 다운로드`}><Icon name="down" />다운로드</button></div></article>
  })}{!project.files.length && <div className="empty-state"><Icon name="file" /><h2>아직 보관된 결과물이 없어요.</h2><p>실제로 생성된 파일이 없습니다. 목업 진행 상태를 결과물로 표시하지 않습니다.</p></div>}</div><p className="view-footnote">{presentation.outputsNote || '디자인 시연용 예시 파일입니다. 실제 AI가 생성하거나 검증한 결과는 아닙니다.'}</p></>
}

export function WorkflowView({ project, activeStep, onStep, onEmployee, presentation = {} }) {
  const states = agentStates(project)
  const steps = presentation.workflowSteps || employees.map((employee, index) => ({ employee, state: states[index] }))
  const labels = ['기획', '개발', '품질', '보안']
  return <section className="handoff-section" aria-labelledby={presentation.handoffTitleId || 'handoff-title'}><header className="handoff-heading"><div><p className="eyebrow">FROM ONE DESK TO THE NEXT.</p><h2 id={presentation.handoffTitleId || 'handoff-title'}>{presentation.workflowTitle || '하나의 일이, 네 사람의 손을 거쳐.'}</h2><p>{project.name} · {presentation.workflowDescription || '각 직원이 맡은 역할과 현재 상태'}</p></div><span className="scroll-cue" aria-hidden="true"><i />SCROLL TO FOLLOW</span></header><AgentLeadership /><div className="handoff-layout"><div className="handoff-sticky"><div className="handoff-scene-top"><span className="office-floor-label">AI 에이전트 회사 / TEAM DESK</span><span>0{activeStep + 1} / 04</span></div><div className="handoff-office" data-role={activeStep}><OfficeDesk role={steps[activeStep].employee.role} /></div><div className="handoff-scene-info" key={activeStep}><p className="eyebrow">단계별 역할 안내</p><h3>{steps[activeStep].employee.name}의 {['기획', '개발', '품질 확인', '보안 검토'][activeStep]}</h3><p>{['만들 기능과 작업 순서를 정리해요.', '승인된 계획을 실제 기능으로 만들어요.', '사용자 입장에서 기능을 확인해요.', '접근 권한과 정보 보호를 살펴봐요.'][activeStep]}</p></div><div className="handoff-tabs" role="group" aria-label="업무 단계 살펴보기">{steps.map(({ employee }, index) => <button type="button" className="handoff-tab" data-focus-step={index} onClick={onStep} aria-pressed={index === activeStep} key={employee.id}><span>0{index + 1}</span>{labels[index]}</button>)}</div></div><div className="handoff-steps">{steps.map(({ employee }, index) => <article key={employee.id} className={`handoff-step${index === activeStep ? ' is-current' : ''}`} data-handoff-step={index} data-lead={Boolean(employee.leadership)}><span className="handoff-step-number">0{index + 1}</span><div className="handoff-step-content"><div className="handoff-step-top"><span className="handoff-employee"><Icon name={['plan', 'code', 'check', 'shield'][index]} />{employee.name} · {employee.job}</span><span className="handoff-state" data-state={steps[index].state.state}>{steps[index].state.label}</span></div><h3>{['먼저, 만들 일을 분명하게.', '계획을 작동하는 모습으로.', '사용하는 사람의 눈으로.', '안심할 수 있는 마지막 확인.'][index]}</h3><p>{employee.purpose}</p><div className="handoff-deliverable"><Icon name="file" /><span>{['기능 명세 · 작업 계획', '변경 코드 · 빌드 기록', '테스트 항목 · 확인 결과', '보안 검토 · 보완 요청'][index]}<small>산출물 예시 · {employee.role} AI</small></span></div><button type="button" className="text-button" onClick={() => onEmployee(index)}>{employee.name}의 업무 보기<Icon name="chevron" /></button></div></article>)}</div></div></section>
}
