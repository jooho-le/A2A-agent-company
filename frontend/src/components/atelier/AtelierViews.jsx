import { Icon, OfficeDesk } from './AtelierArtwork'
import { ActivityList, AgentCards, ProjectCard, ProjectOptions, RequestForm } from './AtelierCards'
import { agentStates, employees, progress, projectStatus } from './atelierMock'

export function HomeView({ mode, onMode, projects, project, activities, onSelect, onCreate, onNew, onEmployee, onPlan, onAdvance, onWorklog }) {
  const status = projectStatus(project)
  const amount = progress(project)
  const roles = agentStates(project).flatMap((state, index) => state.state === 'active' ? [employees[index].role] : [])
  const deskRole = roles.join(' + ') || (project.phase === 3 ? 'All done' : 'Plan review')
  const summaries = project.needsApproval ? ['사슴의 기획안이 대표님을 기다려요.', '기획안을 확인하면 고양이가 개발을 시작합니다.'] : [
    ['사슴이 대표님의 요청을 정리하고 있어요.', '기능과 작업 순서가 담긴 기획안을 만들고 있어요.'],
    ['고양이가 화면과 기능을 만들고 있어요.', '사슴의 기획안을 바탕으로 차근차근 구현하고 있어요.'],
    ['강아지와 토끼가 함께 확인하고 있어요.', '기능 품질과 보안 관점에서 나란히 검토합니다.'],
    ['네 직원의 작업이 모두 끝났어요.', '결과물 보관함에서 예시 문서와 코드를 확인하세요.'],
  ][project.phase]
  const pending = projects.filter((item) => item.needsApproval)
  const approval = pending.find((item) => item.id === project.id) || pending[0]
  return <>
    <RequestForm mode={mode} onMode={onMode} onCreate={onCreate} deskRole={deskRole} />
    <div className="office-grid"><div className="office-work">
      <section className="work-section" aria-label="선택한 프로젝트의 직원 업무">
        <div className="section-heading"><div><p className="eyebrow">TEAM AT WORK</p><h2>우리 팀의 업무</h2></div><button type="button" className="text-button" id="open-worklog" onClick={onWorklog}>업무 기록<Icon name="chevron" /></button></div>
        <div className="project-toolbar"><div className="project-label"><Icon name="folder" /><label className="sr-only" htmlFor="project-select">진행 상황을 볼 프로젝트</label><ProjectOptions id="project-select" projects={projects} selectedId={project.id} onSelect={onSelect} /></div><span className={`badge ${status.kind}`}>{status.text}</span></div>
        <p className="project-subtitle">{project.description}</p>
        <div className="team-relay" data-phase={project.phase} data-waiting={String(project.needsApproval)} aria-label="기획, 개발, 품질·보안 검토 업무 전달">
          <span className="relay-track" aria-hidden="true" /><span className="relay-packet" aria-hidden="true"><Icon name="file" /></span>
          {[[employees[0].name, '기획', 'plan'], [employees[1].name, '개발', 'code'], [`${employees[2].name} · ${employees[3].name}`, '품질·보안 동시 검토', 'shield']].map(([name, label, icon], index) => <span key={icon} className={`relay-node${project.needsApproval && index === 0 ? ' is-done' : project.phase === index && !project.needsApproval ? ' is-working' : project.phase > index ? ' is-done' : ''}`}><span className="relay-node-icon"><Icon name={icon} /></span><span className="relay-node-copy"><strong>{name}</strong><small>{label}</small></span></span>)}
        </div>
        <div className="agent-grid"><AgentCards project={project} onEmployee={onEmployee} /></div>
        <div className="team-report"><span className="report-mark"><Icon name="team" /></span><div><strong>{summaries[0]}</strong><p>{summaries[1]}</p></div><button type="button" className="text-button" id="advance-project" onClick={onAdvance} disabled={project.needsApproval || project.phase === 3}><Icon name="arrow" /><span>{project.needsApproval ? '승인 대기' : project.phase === 3 ? '완료 상태' : '다음 상태 보기'}</span></button></div>
        <div className="progress-meta"><span>프로젝트 진행률</span><strong>{amount}%</strong></div><div className="progress-track" role="progressbar" aria-label="선택한 프로젝트 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={amount}><div className="progress-fill" style={{ width: `${amount}%` }} /></div>
      </section>
      <section className="activity-section" aria-labelledby="activity-heading"><div className="section-heading"><h2 id="activity-heading">팀에서 온 업무 보고</h2><span className="section-note">RECENT UPDATES</span></div><ActivityList projects={projects} activities={activities.slice(0, 3)} /></section>
    </div><aside className="owner-column">
      <section className="approval-panel" aria-label="대표님 결재함"><div className="section-heading"><h2>대표님 결재함</h2><span className="count-pill">{pending.length}</span></div><div className="approval-paper"><Icon name={approval ? 'plan' : 'check'} /></div>
        {approval ? <><h3>대표님, 이 방향으로<br />진행해도 될까요?</h3><p className="approval-project">{approval.name}</p><p className="approval-description">사슴이 기획안을 정리했어요.<br />기능과 작업 순서를 확인해주세요.</p><button type="button" className="button start" onClick={() => onPlan(approval.id)}>기획안 확인<Icon name="arrow" /></button><p className="approval-footer">기획 담당 사슴 → 대표님<br />승인 후 개발 담당 고양이에게 전달됩니다.</p></> : <><h3>확인할 일을<br />모두 마쳤어요.</h3><p className="approval-description">직원들이 다음 작업을 이어갑니다.<br />새 아이디어가 떠오르면 맡겨주세요.</p><button type="button" className="button" onClick={onNew}>새 업무 맡기기<Icon name="plus" /></button><p className="approval-footer">진행 상태는 왼쪽 직원 카드에서<br />언제든 확인할 수 있어요.</p></>}
      </section>
      <section className="brief-panel"><div className="section-heading"><h2>회사 한눈에 보기</h2><span className="section-note">TODAY</span></div><dl className="company-stats"><div><dt>진행 중인 프로젝트</dt><dd>{projects.filter((item) => item.phase !== 3).length}<span>건</span></dd></div><div><dt>함께 일하는 직원</dt><dd>4<span>명</span></dd></div></dl><p>요청을 맡기면 기획 → 개발 →<br />품질·보안 검토 순서로 진행해요.</p></section>
    </aside></div>
  </>
}

export function ProjectsView({ projects, search, onSearch, onSelect }) {
  const matches = projects.filter((project) => `${project.name} ${project.description}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  return <><div className="view-toolbar"><p className="toolbar-note">우리 회사가 함께 만드는 일 <strong>{String(projects.length).padStart(2, '0')}</strong></p><div className="search"><Icon name="search" /><label className="sr-only" htmlFor="atelier-search">프로젝트 검색</label><input id="atelier-search" type="search" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="프로젝트 이름 검색" autoComplete="off" /></div></div><div className="project-grid">{matches.length ? matches.map((project) => <ProjectCard key={project.id} project={project} onSelect={onSelect} />) : <div className="empty-state"><Icon name="search" /><h2>일치하는 프로젝트가 없어요.</h2><p>검색어를 바꾸거나 새 업무를 만들어보세요.</p></div>}</div></>
}

export function TeamView({ project, onEmployee }) {
  return <><div className="team-banner"><div><span className="eyebrow">SMALL TEAM. CLEAR ROLES.</span><h2>대표님과 함께 일할 네 명의 직원.</h2><p>이름도, 성격도, 잘하는 일도 달라요. 목표는 하나예요.</p></div><span className="headcount"><strong>04</strong>AI EMPLOYEES</span></div><div className="employee-grid"><AgentCards project={project} profile onEmployee={onEmployee} /></div><p className="view-footnote">직원 이름·성격·진행 상태는 디자인 시연용 목업입니다.</p></>
}

export function OutputsView({ projects, project, onSelect, onFile, onDownload }) {
  return <><div className="view-toolbar"><div><h2>{project.name}</h2><p className="toolbar-note">직원들이 정리한 문서와 코드를 확인하세요.</p></div><label className="output-selector"><span className="sr-only">결과물을 볼 프로젝트</span><ProjectOptions projects={projects} selectedId={project.id} onSelect={onSelect} /></label></div><div className="output-grid">{project.files.map((file, index) => {
    const employee = file.endsWith('.md') ? employees[0] : file.includes('test') ? employees[2] : employees[1]
    return <article className="output-card" key={file}><div className="output-card-top"><span className="file-icon"><Icon name={file.endsWith('.md') ? 'file' : 'code'} /></span><span className="file-type">{file.endsWith('.md') ? 'DOCUMENT' : file.includes('test') ? 'TEST NOTES' : 'SOURCE CODE'}</span></div><h3>{file.split('/').pop()}</h3><p className="file-path">{file}</p><div className="file-owner"><span className="mini-avatar">{employee.initial}</span>{employee.name} · {employee.job}<span> / 예시 파일</span></div><div className="output-actions"><button type="button" className="button file-open" onClick={() => onFile(index)}>파일 보기<Icon name="chevron" /></button><button type="button" className="button" onClick={() => onDownload(index)} aria-label={`${file.split('/').pop()} 예시 다운로드`}><Icon name="down" />다운로드</button></div></article>
  })}</div><p className="view-footnote">디자인 시연용 예시 파일입니다. 실제 AI가 생성하거나 검증한 결과는 아닙니다.</p></>
}

export function WorkflowView({ project, activeStep, onStep, onEmployee }) {
  const states = agentStates(project)
  const labels = ['기획', '개발', '품질', '보안']
  return <section className="handoff-section" aria-labelledby="handoff-title"><header className="handoff-heading"><div><p className="eyebrow">FROM ONE DESK TO THE NEXT.</p><h2 id="handoff-title">하나의 일이, 네 사람의 손을 거쳐.</h2><p>{project.name} · 각 직원이 맡은 역할과 현재 상태</p></div><span className="scroll-cue" aria-hidden="true"><i />SCROLL TO FOLLOW</span></header><div className="handoff-layout"><div className="handoff-sticky"><div className="handoff-scene-top"><span className="office-floor-label">ATELIER / TEAM DESK</span><span>0{activeStep + 1} / 04</span></div><div className="handoff-office" data-role={activeStep}><OfficeDesk role={employees[activeStep].role} /></div><div className="handoff-scene-info" key={activeStep}><p className="eyebrow">단계별 역할 안내</p><h3>{employees[activeStep].name}의 {['기획', '개발', '품질 확인', '보안 검토'][activeStep]}</h3><p>{['만들 기능과 작업 순서를 정리해요.', '승인된 계획을 실제 기능으로 만들어요.', '사용자 입장에서 기능을 확인해요.', '접근 권한과 정보 보호를 살펴봐요.'][activeStep]}</p></div><div className="handoff-tabs" role="group" aria-label="업무 단계 살펴보기">{employees.map((employee, index) => <button type="button" className="handoff-tab" data-focus-step={index} onClick={onStep} aria-pressed={index === activeStep} key={employee.id}><span>0{index + 1}</span>{labels[index]}</button>)}</div></div><div className="handoff-steps">{employees.map((employee, index) => <article key={employee.id} className={`handoff-step${index === activeStep ? ' is-current' : ''}`} data-handoff-step={index}><span className="handoff-step-number">0{index + 1}</span><div className="handoff-step-content"><div className="handoff-step-top"><span className="handoff-employee"><Icon name={['plan', 'code', 'check', 'shield'][index]} />{employee.name} · {employee.job}</span><span className="handoff-state">{states[index].label}</span></div><h3>{['먼저, 만들 일을 분명하게.', '계획을 작동하는 모습으로.', '사용하는 사람의 눈으로.', '안심할 수 있는 마지막 확인.'][index]}</h3><p>{employee.purpose}</p><div className="handoff-deliverable"><Icon name="file" /><span>{['기능 명세 · 작업 계획', '변경 코드 · 빌드 기록', '테스트 항목 · 확인 결과', '보안 검토 · 보완 요청'][index]}<small>산출물 예시 · {employee.role} AI</small></span></div><button type="button" className="text-button" onClick={() => onEmployee(index)}>{employee.name}의 업무 보기<Icon name="chevron" /></button></div></article>)}</div></div></section>
}
