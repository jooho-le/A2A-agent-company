import { useEffect, useRef, useState } from 'react'
import { HomeView, OutputsView, ProjectsView, TeamView, WorkflowView } from './AtelierViews'
import AtelierDialog from './AtelierDialog'
import SingleGiraffe from './SingleGiraffe'

const stages = ['기획', '개발', 'QA', '보안 검토']
const roles = ['planner', 'developer', 'qa', 'security']
const roleNames = ['Planner', 'Developer', 'QA', 'Security']
const descriptions = ['요청과 작업 계획 정리', '계획에 따른 기능 구현', '기능 및 예외 상황 확인', '접근 권한과 정보 보호 검토']
const giraffe = {
  id: 'giraffe', name: '기린', initial: '기', job: '싱글 에이전트', role: 'Single',
  roleLabel: '싱글 에이전트 · 전체 업무 담당',
  quote: '“처음부터 끝까지, 차근차근 이어갈게요.”', personality: '하나의 AI · 네 가지 역할',
  purpose: '기린 한 명이 기획, 개발, QA, 보안 검토를 순서대로 수행합니다.',
  responsibilities: descriptions,
}

function characterState(project) {
  if (!project.started) return 'intro'
  if (project.approval || project.step === 4) return 'done'
  return project.characterState || 'working'
}
function stageState(project, index) {
  if (index < project.step) return 'done'
  if (index !== project.step || !project.started) return 'waiting'
  return characterState(project) === 'working' ? 'active' : characterState(project)
}
function stageLabel(project, index) {
  if (project.approval && index === 0) return '승인 대기'
  return { done: '목업 완료', warning: '문제 발견 · 목업', active: '목업 진행 중', waiting: '대기' }[stageState(project, index)]
}
function progress(project) {
  return !project.started ? 0 : project.step === 4 ? 100 : project.step * 25 + (characterState(project) === 'done' ? 25 : 10)
}
function status(project) {
  const state = characterState(project)
  return { text: state === 'intro' ? '업무 대기 · 목업' : project.approval ? '기획안 승인 대기' : project.step === 4 ? '목업 작업 완료' : `${stages[project.step]} · ${{ working: '목업 진행 중', warning: '문제 발견 · 목업', done: '단계 완료 · 목업' }[state]}`, kind: project.approval || state === 'warning' ? 'review' : state === 'done' ? 'complete' : '' }
}
// 실제 파일을 만들지 않고 공통 화면이 필요한 표시 필드만 계산합니다.
function displayProject(project) {
  return { ...project, phase: project.step === 4 ? 3 : Math.min(project.step, 2), needsApproval: project.approval, scope: [project.description, ...(project.feedback ? [project.feedback] : [])], files: [], time: '목업' }
}

export default function AtelierSingleMode({ view, active, onMode, onView, newRequest, onNewHandled, activeStep, onStep, rootRef, onProjectCount, onNotify }) {
  const [projects, setProjects] = useState([
    { id: 'single-demo', name: '싱글 에이전트 체험 프로젝트', description: '한 AI가 기획부터 보안 검토까지 순서대로 수행하는 목업입니다.', started: false, step: 0, approval: false, feedback: '', history: ['싱글 에이전트 목업을 준비했어요.'] },
    // 드롭다운 전환 확인용 로컬 목업입니다. 서버 프로젝트나 실제 실행 결과가 아닙니다.
    { id: 'single-notes-demo', name: '업무 메모 도구 · 선택 확인용 목업', description: '프로젝트 선택에 따라 화면 전체가 바뀌는지 확인하는 개발 단계 목업입니다.', started: true, step: 1, approval: false, feedback: '', history: ['프로젝트 선택 확인용 개발 단계 목업을 준비했어요.'] },
  ])
  const [selectedId, setSelectedId] = useState('single-demo')
  const [query, setQuery] = useState('')
  const [dialog, setDialog] = useState(null)
  const triggerRef = useRef(null)
  const sequence = useRef(0)
  const project = projects.find((item) => item.id === selectedId)
  const state = characterState(project)
  const roleIndex = Math.min(project.step, 3)
  const visibleProject = displayProject(project)
  const visibleProjects = projects.map(displayProject)
  const activities = project.history.slice().reverse().map((text, index) => ({ employee: 0, text, projectId: project.id, time: `목업 기록 ${project.history.length - index}` }))
  useEffect(() => { onProjectCount(projects.length) }, [onProjectCount, projects.length])

  function close() { setDialog(null); onNewHandled() }
  function open(type, index = roleIndex) { triggerRef.current = document.activeElement; setDialog({ type, index }) }
  function selectProject(id) {
    if (!projects.some((item) => item.id === id)) return
    setSelectedId(id); onNotify('')
  }
  function change(changes, record) {
    setProjects((items) => items.map((item) => item.id === selectedId ? { ...item, characterState: 'working', ...changes, history: [...item.history, record] } : item))
    onNotify('목업 상태만 변경했습니다. 실제 Agent는 실행하지 않았습니다.')
  }
  function create(name, description) {
    const id = `single-${++sequence.current}`
    setProjects((items) => [{ id, name, description, started: true, step: 0, approval: false, feedback: '', history: ['새 요청을 받고 기획 목업을 시작했어요.'] }, ...items])
    setSelectedId(id); setDialog(null); setQuery(''); onNewHandled(); onView('home')
    onNotify('기린에게 업무를 맡긴 목업입니다. 서버 요청은 전송하지 않았습니다.')
  }
  function advance() {
    if (project.approval || project.step === 4) return
    if (!project.started) change({ started: true }, '기린이 기획 목업을 시작했어요.')
    else if (project.step === 0) change({ approval: true }, '기획안 확인을 요청했어요.')
    else change({ step: project.step + 1 }, `${stages[project.step]} 단계의 목업을 마쳤어요.`)
  }
  function approve() { change({ step: 1, approval: false }, '기획안을 승인하고 같은 기린이 개발을 시작했어요.'); close() }
  function feedback(text) { change({ step: 0, approval: false, feedback: text }, '피드백을 받아 같은 기린이 기획을 다시 정리해요.'); close() }

  const task = state === 'intro' ? '업무 요청을 기다리고 있어요. 기린이 네 단계를 모두 맡습니다.' : project.approval ? '기획안을 준비했어요. 승인하면 같은 기린이 개발을 이어갑니다.' : project.step === 4 ? '기획·개발·QA·보안 검토의 목업을 모두 마쳤어요.' : state === 'warning' ? `${stages[roleIndex]} 단계에서 문제를 발견한 목업입니다.` : state === 'done' ? `${stages[roleIndex]} 단계의 목업을 마쳤어요.` : `${descriptions[roleIndex]} 작업을 진행하는 목업입니다.`
  const steps = stages.map((name, index) => ({ name, state: stageState(project, index), label: stageLabel(project, index) }))
  const workflowSteps = steps.map((step, index) => ({ employee: { ...giraffe, id: `giraffe-${roles[index]}`, role: roleNames[index], job: `${stages[index]} 담당`, purpose: `같은 기린이 ${descriptions[index]} 작업을 맡습니다.` }, state: { state: step.state, label: step.label, task: descriptions[index] } }))
  const presentation = {
    mode: 'single', staff: [giraffe], status: status(project), amount: progress(project), deskRole: 'Single AI',
    agents: [{ employee: giraffe, state: { state: state === 'working' ? 'active' : state === 'intro' ? '' : state, label: stageLabel(project, roleIndex), task }, portrait: <SingleGiraffe role={roles[roleIndex]} state={state} /> }],
    summaries: [status(project).text, task], steps, workflowSteps,
    advanceDisabled: project.approval || project.step === 4,
    advanceLabel: project.approval ? '승인 대기' : project.step === 4 ? '완료 상태' : !project.started ? '기획 목업 시작' : '다음 상태 보기',
    progressLabel: '프로젝트 진행률 · 목업', searchId: 'single-atelier-search', handoffTitleId: 'single-handoff-title',
    projectMeta: (item) => ({ status: status(item), amount: progress(item), staffCount: 1 }),
    teamTitle: '대표님과 함께 일할 한 명의 직원.', teamDescription: '기린 한 명이 기획·개발·QA·보안 검토를 모두 맡습니다.',
    workflowTitle: '하나의 일이, 한 기린의 네 역할을 거쳐.', workflowDescription: '동일한 기린이 순서대로 맡는 네 가지 역할과 현재 상태',
    outputsNote: '실제로 생성된 파일이 없습니다. 목업 진행 상태를 결과물로 표시하지 않습니다.',
  }
  const currentDialog = newRequest ? { type: 'new' } : dialog
  const detailIndex = currentDialog?.index ?? roleIndex
  const detailState = workflowSteps[detailIndex]?.state
  const controls = <fieldset className="character-controls"><legend>캐릭터 상태 미리보기 · 목업</legend>{[['working', '작업 중'], ['warning', '문제 발견'], ['done', '단계 완료']].map(([value, text]) => <button key={value} type="button" className="button compact" aria-pressed={state === value} disabled={!project.started || project.approval || project.step === 4} onClick={() => change({ characterState: value }, `${stages[roleIndex]} 캐릭터의 ‘${text}’ 목업 상태를 확인했어요.`)}>{text}</button>)}<p className="dialog-note">이미지 확인용 목업이며 실제 실행·검증 결과가 아닙니다.</p></fieldset>
  return <div className="single-workspace" hidden={!active}>
    {view === 'home' && <HomeView mode="single" onMode={onMode} projects={visibleProjects} project={visibleProject} activities={activities} onSelect={selectProject} onCreate={create} onNew={() => open('new')} onEmployee={() => open('employee')} onPlan={(id) => { selectProject(id); open('plan') }} onAdvance={advance} onWorklog={() => open('worklog')} presentation={presentation} />}
    {view === 'projects' && <ProjectsView projects={visibleProjects} search={query} onSearch={setQuery} onSelect={(id) => { selectProject(id); onView('home') }} presentation={presentation} />}
    {view === 'team' && <TeamView project={visibleProject} onEmployee={() => open('employee')} presentation={presentation} />}
    {view === 'workflow' && <WorkflowView project={visibleProject} activeStep={activeStep} onStep={onStep} onEmployee={(index) => open('employee', index)} presentation={presentation} />}
    {view === 'outputs' && <OutputsView projects={visibleProjects} project={visibleProject} onSelect={selectProject} onFile={(index) => open('file', index)} presentation={presentation} />}
    {active && currentDialog && <AtelierDialog key={currentDialog.type} modal={currentDialog} project={visibleProject} projects={visibleProjects} activities={activities} onClose={close} onCreate={create} onApprove={approve} onRevision={() => open('revision')} onFeedback={feedback} returnFocusRef={triggerRef} rootRef={rootRef} presentation={{ ...presentation, detailEmployee: workflowSteps[detailIndex]?.employee, detailState, detailPortrait: <SingleGiraffe role={roles[detailIndex]} state={detailState?.state === 'active' ? 'working' : detailState?.state === 'waiting' ? 'intro' : detailState?.state} />, controls: detailIndex === roleIndex ? controls : null }} />}
  </div>
}
