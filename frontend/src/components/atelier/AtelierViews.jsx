import { cloneElement } from 'react'
import { StudioBanner, StudioObject, StudioRail } from './StudioVisuals'
import TeamStoryDeck from './TeamStoryDeck'
import AgentLeadership from './AgentLeadership'
import SourceArtifactDownload from './SourceArtifactDownload'
import { Icon, OfficeDesk } from './AtelierArtwork'
import { ActivityList, AgentCards, ProjectCard, ProjectOptions, RequestForm } from './AtelierCards'
import { agentStates, employees, progress, projectStatus } from './atelierMock'

export function HomeView({ mode, onMode, projects, project, activities, onSelect, onCreate, onNew, onEmployee, onPlan, onWorklog, officeScene, submission = null, presentation = {} }) {
  if (mode === 'multi') return officeScene ? cloneElement(officeScene, {
    requestPanel: <RequestForm mode={mode} onCreate={onCreate} submission={submission} />,
  }) : null
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
      <section className="brief-panel"><div className="section-heading"><h2>회사 한눈에 보기</h2><span className="section-note">TODAY</span></div><dl className="company-stats"><div><dt>진행 중인 프로젝트</dt><dd>{projects.filter((item) => item.phase !== 3).length}<span>건</span></dd></div><div><dt>함께 일하는 직원</dt><dd>{presentation.staff?.length || 5}<span>명</span></dd></div></dl><p>{mode === 'single' ? <>기린 한 명이 기획 → 개발 → QA →<br />보안 검토를 순서대로 맡습니다.</> : <>요청을 맡기면 기획 → 개발 →<br />품질·보안 검토 순서로 진행해요.</>}</p></section>
    </aside></div>
  </>
}

export function ProjectsView({ projects, search, onSearch, onSelect, actual = false, onNew, onRetry, saved = null, onLogin, onStoredSelect, presentation = {} }) {
  const matches = projects.filter((project) => `${project.name} ${project.description}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const pending = saved && ['idle', 'loading'].includes(saved.status)
  const failed = saved?.status === 'error'
  const needsLogin = saved?.error?.code === 'AUTH_REQUIRED'
  return <>
    <StudioBanner kind="folder"><span className="studio-banner-label">모아둔 프로젝트</span><strong className="studio-banner-number">{pending || (failed && !projects.length) ? '—' : projects.length}<small>개</small></strong></StudioBanner>
    <div className="view-toolbar">{projects.length > 0 && <div className="search"><Icon name="search" /><label className="sr-only" htmlFor={presentation.searchId || 'atelier-search'}>프로젝트 검색</label><input id={presentation.searchId || 'atelier-search'} type="search" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="이름으로 찾기" autoComplete="off" /></div>}</div>
    {pending && <p role="status">저장된 프로젝트 목록을 불러오는 중입니다.</p>}
    {failed && <p role="alert">{saved.error.message} {needsLogin ? <button className="button" type="button" onClick={onLogin}>로그인</button> : <button className="button" type="button" onClick={saved.retry}>다시 불러오기</button>}</p>}
    {actual && projects.some(item => item.status.text === '상태 조회 오류') && <p role="alert">업무 상태 조회에 실패했습니다. <button className="button" type="button" onClick={onRetry}>다시 불러오기</button></p>}
    <StudioRail className="project-grid" label="프로젝트 카드" count={matches.length}>{matches.length ? matches.map((project, index) => <ProjectCard palette={index} key={`${project.stored ? 'stored' : 'run'}:${project.id}`} project={project} onSelect={project.stored ? onStoredSelect : onSelect} {...presentation.projectMeta?.(project)} />)
      : !pending && !failed && <div className="empty-state studio-empty"><div className="studio-empty-art"><StudioObject kind="folder" /></div><h2>{projects.length ? '검색 결과가 없어요' : '첫 프로젝트를 만들어보세요'}</h2>
        <p>{projects.length ? '다른 이름으로 찾아보세요.' : '만들고 싶은 것을 알려주면 팀이 작업해요.'}</p>
        {!projects.length && onNew && <button type="button" className="button start" onClick={onNew}>새로 만들기<Icon name="arrow" /></button>}
      </div>}</StudioRail>
  </>
}

export function TeamView({ project, onEmployee, actualStates, presentation = {} }) {
  const agents = actualStates ? employees.map(employee => {
    const current = actualStates.find(agent => agent.agentRole === employee.role.toUpperCase())
    return { employee, state: { state: current?.displayState === 'working' ? 'active' : current?.displayState === 'succeeded' ? 'done' : 'idle', label: current?.label || '업무 조율', task: employee.purpose } }
  }) : presentation.agents
  return <><div className="employee-grid"><AgentCards project={project} agents={agents} profile onEmployee={onEmployee} /></div>
    <details className="ux-details team-story"><summary><span>팀은 어떻게<br />일하나요?</span><span className="team-story-open"><Icon name="arrow" /></span></summary><TeamStoryDeck /><details className="ux-details"><summary>전체 협업 구조</summary><AgentLeadership /></details></details></>
}

export function OutputsView({ projects, project, onSelect, onFile, onDownload, actual = false, catalog, onRetry, onBrowseProjects, presentation = {} }) {
  if (actual) return <RunOutputs projects={projects} project={project} onSelect={onSelect} catalog={catalog} onRetry={onRetry} onBrowseProjects={onBrowseProjects} />
  return <><div className="view-toolbar"><div><h2>{project.name}</h2><p className="toolbar-note">직원들이 정리한 문서와 코드를 확인하세요.</p></div><label className="output-selector"><span className="sr-only">결과물을 볼 프로젝트</span><ProjectOptions projects={projects} selectedId={project.id} onSelect={onSelect} /></label></div><div className="output-grid">{project.files.map((file, index) => {
    const employee = presentation.staff?.[0] || (file.endsWith('.md') ? employees[0] : file.includes('test') ? employees[2] : employees[1])
    return <article className="output-card" key={file}><div className="output-card-top"><span className="file-icon"><Icon name={file.endsWith('.md') ? 'file' : 'code'} /></span><span className="file-type">{file.endsWith('.md') ? 'DOCUMENT' : file.includes('test') ? 'TEST NOTES' : 'SOURCE CODE'}</span></div><h3>{file.split('/').pop()}</h3><p className="file-path">{file}</p><div className="file-owner"><span className="mini-avatar">{employee.initial}</span>{employee.name} · {employee.job}<span> / 예시 파일</span></div><div className="output-actions"><button type="button" className="button file-open" onClick={() => onFile(index)}>파일 보기<Icon name="chevron" /></button><button type="button" className="button" onClick={() => onDownload(index)} aria-label={`${file.split('/').pop()} 예시 다운로드`}><Icon name="down" />다운로드</button></div></article>
  })}{!project.files.length && <div className="empty-state"><Icon name="file" /><h2>아직 보관된 결과물이 없어요.</h2><p>실제로 생성된 파일이 없습니다. 목업 진행 상태를 결과물로 표시하지 않습니다.</p></div>}</div><p className="view-footnote">{presentation.outputsNote || '디자인 시연용 예시 파일입니다. 실제 AI가 생성하거나 검증한 결과는 아닙니다.'}</p></>
}

function RunOutputs({ projects, project, onSelect, catalog, onRetry, onBrowseProjects }) {
  if (!project) return <div className="empty-state studio-empty"><div className="studio-empty-art"><StudioObject kind="paper" /></div><h2>프로젝트를 먼저 선택해주세요</h2><p>선택한 프로젝트의 결과를 여기서 볼 수 있어요.</p>{onBrowseProjects && <button type="button" className="button start" onClick={onBrowseProjects}>프로젝트 선택<Icon name="arrow" /></button>}</div>
  const current = catalog?.runId === project.runId ? catalog : { items: [], status: 'idle' }
  const failed = current.status === 'error'
  const loading = ['idle', 'loading', 'cancelled'].includes(current.status)
  return <>
    <StudioBanner kind="paper"><span className="studio-banner-label">팀이 만든 결과</span><h2>{project.name}</h2>
      <label className="output-selector"><span className="sr-only">결과물을 볼 프로젝트</span><ProjectOptions id="actual-output-project" projects={projects} selectedId={project.id} onSelect={onSelect} /></label></StudioBanner>
    {failed && <p role="alert">결과물을 불러오지 못했어요. <button type="button" className="button" onClick={onRetry}>다시 불러오기</button></p>}
    {loading && <p role="status">산출물 목록을 확인하고 있습니다.</p>}
    <StudioRail className="output-grid" label="결과 카드" count={current.items.length}>{current.items.map((item, index) => <article className="output-card" data-studio-tilt="" data-palette={index % 5} key={item.artifactId}>
      <div className="studio-output-cover" aria-hidden="true"><StudioObject kind={item.source ? 'monitor' : 'paper'} /></div>
      <div className="output-card-top"><span className="file-icon"><Icon name={item.source ? 'code' : 'file'} /></span></div>
      <h3>{item.source ? '소스 코드' : item.type}</h3>
      <div className="file-owner">{item.author}{item.source && item.codeVersion !== null && <span> · 버전 {item.codeVersion}</span>}</div>
      <details className="artifact-details"><summary>내용 보기</summary>
        <p>이름: {item.name || item.type}</p>
        {item.createdAt && <p>등록 시각: <time dateTime={item.createdAt}>{item.createdAt}</time></p>}
        {item.version !== null && <p>산출물 버전: {item.version}</p>}
        {!item.source && item.codeVersion !== null && <p>코드 버전: {item.codeVersion}</p>}
        {item.details.map((detail, index) => <p key={index}>{detail}</p>)}
        {!!item.changes.length && <><p>변경 기록입니다. 현재 파일과 다를 수 있어요.</p><ul>{item.changes.map((file, index) => <li key={index}>{file.path} · {file.action}</li>)}</ul></>}
      </details>
      {item.source && <SourceArtifactDownload key={`${item.runId}:${item.artifactId}`} item={item} />}
    </article>)}</StudioRail>
    {!failed && !loading && !current.items.length && <div className="empty-state studio-empty"><div className="studio-empty-art"><StudioObject kind="paper" /></div><h2>아직 결과가 없어요</h2><p>작업 중 만들어진 결과가 여기에 모여요.</p></div>}
    <p className="view-footnote">소스 코드는 ZIP으로 받을 수 있어요. 보고서는 내용 보기를 눌러주세요.</p>
  </>
}

export function WorkflowView({ project, activeStep, onStep, onEmployee, presentation = {} }) {
  const states = agentStates(project)
  const details = {
    orchestrator: { label: '관리', icon: 'grid', title: '전체 흐름을 관리하고, 다음 작업을 결정해요.', deliverable: '호출 순서 · 병렬 검토 · 수정·재검증 결정' },
    planner: { label: '계획', icon: 'plan', title: '먼저, 만들 일을 분명하게.', deliverable: '기능 명세 · 작업 계획' },
    developer: { label: '개발', icon: 'code', title: '계획을 작동하는 모습으로.', deliverable: '변경 코드 · 빌드 기록' },
    qa: { label: 'QA', icon: 'check', title: '요구사항대로 작동하는지 확인해요.', deliverable: '테스트 항목 · 확인 결과' },
    security: { label: '보안', icon: 'shield', title: '접근 권한과 정보 보호를 확인해요.', deliverable: '보안 검토 · 보완 요청' },
  }
  const steps = presentation.workflowSteps || [4, 0, 1, 2, 3].map(index => ({ employee: employees[index], state: states[index] }))
  const currentIndex = Math.min(activeStep, steps.length - 1)
  const current = steps[currentIndex].employee
  const detail = employee => details[employee.id] || details[['planner', 'developer', 'qa', 'security'][steps.findIndex(step => step.employee === employee)]] || details.planner
  return <section className="handoff-section" aria-labelledby={presentation.handoffTitleId || 'handoff-title'}>
    <header className="handoff-heading"><div><p className="eyebrow">FROM ONE DESK TO THE NEXT.</p><h2 id={presentation.handoffTitleId || 'handoff-title'}>{presentation.workflowTitle || '다섯 명의 에이전트, 함께 일하는 하나의 팀.'}</h2><p>{project.name} · {presentation.workflowDescription || '프로젝트 목업의 역할 안내 · 실제 호출 순서는 Run Event로 확인합니다.'}</p></div><span className="scroll-cue" aria-hidden="true"><i />SCROLL TO FOLLOW</span></header>
    <AgentLeadership />
    <div className="handoff-layout"><div className="handoff-sticky">
      <div className="handoff-scene-top"><span className="office-floor-label">AIO / TEAM DESK</span><span>{String(currentIndex + 1).padStart(2, '0')} / {String(steps.length).padStart(2, '0')}</span></div>
      <div className="handoff-office" data-role={currentIndex}><OfficeDesk role={current.role === 'Orchestrator' ? 'Planner' : current.role} /></div>
      <div className="handoff-scene-info" key={currentIndex}><p className="eyebrow">역할 안내 · 프로젝트 목업</p><h3>{current.name}</h3><p>{current.purpose}</p></div>
      <div className="handoff-tabs" role="group" aria-label="리더와 작업 역할 살펴보기" style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0,1fr))` }}>{steps.map(({ employee }, index) => <button type="button" className="handoff-tab" data-focus-step={index} onClick={onStep} aria-pressed={index === currentIndex} key={employee.id}><span>{String(index + 1).padStart(2, '0')}</span>{detail(employee).label}</button>)}</div>
    </div><div className="handoff-steps">{steps.map(({ employee, state }, index) => <article key={employee.id} className={`handoff-step${index === currentIndex ? ' is-current' : ''}`} data-handoff-step={index} data-lead={Boolean(employee.leadership)}>
      <span className="handoff-step-number">{String(index + 1).padStart(2, '0')}</span>
      <div className="handoff-step-content"><div className="handoff-step-top"><span className="handoff-employee"><Icon name={detail(employee).icon} />{employee.name} · {employee.job}</span><span className="handoff-state" data-state={state.state}>{state.label}</span></div><h3>{detail(employee).title}</h3><p>{employee.purpose}</p><div className="handoff-deliverable"><Icon name="file" /><span>{detail(employee).deliverable}<small>{employee.id === 'orchestrator' ? '리더 · 작업 Step과 구분' : `산출물 예시 · ${employee.role} AI`}</small></span></div><button type="button" className="text-button" onClick={() => onEmployee(employees.findIndex(item => item.id === employee.id))}>{employee.name}의 업무 보기<Icon name="chevron" /></button></div>
    </article>)}</div></div>
    <p className="view-footnote">위 목록은 고정 실행 순서가 아닌 역할 소개입니다. 실제 멀티 Run에서는 오케스트레이터가 QA·Security 병렬 실행과 Developer 재작업·재검증을 관리합니다.</p>
  </section>
}
