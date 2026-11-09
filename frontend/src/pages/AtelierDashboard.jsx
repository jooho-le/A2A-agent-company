import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { AtelierIcons } from '../components/atelier/AtelierArtwork'
import { AtelierHeader, AtelierSidebar, OfficeDate } from '../components/atelier/AtelierChrome'
import AgentOffice3D from '../components/atelier/AgentOffice3D'
import AgentRunMonitor from '../components/atelier/AgentRunMonitor'
import { buildAgentRunDisplayState } from '../components/atelier/agentRunDisplayState'
import AtelierDialog from '../components/atelier/AtelierDialog'
import { HomeView, OutputsView, ProjectsView, TeamView, WorkflowView } from '../components/atelier/AtelierViews'
import { artifactContent, initialActivities, initialProjects, viewCopy } from '../components/atelier/atelierMock'
import useAtelierMotion, { useMediaQuery } from '../components/atelier/useAtelierMotion'
import useAgentRunMonitor from '../hooks/useAgentRunMonitor'
import './AtelierDashboard.css'

const runEventLimit = 20

function runQueryReducer(state, action) {
  if (action.type === 'select') return { runId: action.runId, offset: 0, followLatest: Boolean(action.runId) }
  if (action.type === 'refresh') return { ...state, followLatest: true }
  if (action.type === 'page') return { ...state, offset: action.offset, followLatest: false }
  // 사용자가 조회·새로고침할 때 최신 구간을 한 번 결정합니다. 자동 갱신은 선택한 페이지를 유지합니다.
  if (action.type === 'latest' && state.runId === action.runId && state.followLatest) {
    return { ...state, offset: Math.max(0, action.total - runEventLimit), followLatest: false }
  }
  return state
}

function readView() {
  const route = window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : 'home'
  return Object.hasOwn(viewCopy, route) ? route : 'home'
}

export default function AtelierDashboard({ onRunScenario, onOpenDocuments, onLogout }) {
  const rootRef = useRef(null)
  const returnFocusRef = useRef(null)
  const sequence = useRef(0)
  const toastTimer = useRef(null)
  const celebrationTimer = useRef(null)
  const celebrated = useRef(new Set())
  const downloads = useRef(new Map())
  const [view, setView] = useState(readView)
  const [projects, setProjects] = useState(() => structuredClone(initialProjects))
  const [activities, setActivities] = useState(initialActivities)
  const [selectedId, setSelectedId] = useState('orbit')
  const [search, setSearch] = useState('')
  const [modal, setModal] = useState(null)
  const [toast, setToast] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [motionRequested, setMotionRequested] = useState(true)
  const [handoffStep, setHandoffStep] = useState(0)
  const [celebration, setCelebration] = useState(null)
  // 실제 Run 조회는 Dashboard에서 한 번만 수행하며 목업 프로젝트·3D와 독립적으로 관리합니다.
  const [runQuery, dispatchRunQuery] = useReducer(runQueryReducer, { runId: '', offset: 0, followLatest: false })
  const [officeModeChoice, setOfficeModeChoice] = useState({ runId: '', mode: 'mock' })
  const officeMode = officeModeChoice.runId === runQuery.runId ? officeModeChoice.mode : runQuery.runId ? 'actual' : 'mock'
  const runMonitor = useAgentRunMonitor(runQuery.runId, { limit: runEventLimit, offset: runQuery.offset, includeArchitecture: true,
    collectEvents: view === 'home' && officeMode === 'actual',
    polling: view === 'home' && officeMode === 'actual' })
  const pollingLabels = { idle: 'Run 선택 전', inactive: '목업 또는 다른 화면에서 중지', paused: '일시정지',
    finished: 'Run 종료로 중지', not_multi: '멀티 Run이 아니므로 중지', active: '3초 간격',
    retrying: `연결 오류 · ${runMonitor.polling.delayMs / 1000}초 후 재시도(기존 데이터 유지)` }
  // 공유할 실제 데이터를 표시용으로만 변환하고 기존 목업 시연에는 반영하지 않습니다.
  const runDisplayState = buildAgentRunDisplayState(runMonitor)
  const eventTotal = runMonitor.pagination.total
  const eventQueryStatus = runMonitor.status.events
  const mobile = useMediaQuery('(max-width:760px)')
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const motion = motionRequested && !reducedMotion
  const project = projects.find((item) => item.id === selectedId)
  const copy = viewCopy[view]

  useAtelierMotion(rootRef, { enabled: motion, view, mode: 'multi', project, projectCount: projects.length, onStep: setHandoffStep })

  useEffect(() => {
    if (runQuery.followLatest && eventTotal !== null && (eventQueryStatus === 'success' || eventQueryStatus === 'empty')) {
      dispatchRunQuery({ type: 'latest', runId: runQuery.runId, total: eventTotal })
    }
  }, [runQuery.followLatest, runQuery.runId, eventTotal, eventQueryStatus])

  useEffect(() => {
    function restoreView() { setView(readView()); setDrawerOpen(false) }
    if (window.location.hash !== `#/${readView()}`) window.history.replaceState(null, '', `#/${readView()}`)
    window.addEventListener('hashchange', restoreView)
    window.addEventListener('popstate', restoreView)
    return () => {
      window.removeEventListener('hashchange', restoreView)
      window.removeEventListener('popstate', restoreView)
    }
  }, [])

  useEffect(() => {
    const previousTitle = document.title
    document.title = `${viewCopy[view][0]} — AI 에이전트 회사`
    rootRef.current?.querySelector('#page-title')?.focus({ preventScroll: true })
    return () => { document.title = previousTitle }
  }, [view])

  useEffect(() => {
    const pendingDownloads = downloads.current
    return () => {
      clearTimeout(toastTimer.current)
      clearTimeout(celebrationTimer.current)
      pendingDownloads.forEach((timer, url) => { clearTimeout(timer); URL.revokeObjectURL(url) })
      pendingDownloads.clear()
    }
  }, [])

  function requestNew() { openModal('new') }
  function refreshRun() {
    dispatchRunQuery({ type: 'refresh' })
    void runMonitor.refresh()
  }
  function selectRun(runId) {
    if (runId === runQuery.runId) refreshRun()
    else dispatchRunQuery({ type: 'select', runId })
  }
  function notify(message) {
    clearTimeout(toastTimer.current)
    setToast(message)
    toastTimer.current = setTimeout(() => setToast(''), 4500)
  }
  function changeView(next) {
    if (!Object.hasOwn(viewCopy, next)) return
    setView(next); setDrawerOpen(false); setCelebration(null)
    if (window.location.hash !== `#/${next}`) window.history.pushState(null, '', `#/${next}`)
    window.scrollTo({ top: 0, behavior: 'instant' })
  }
  const closeDrawer = useCallback((focus = false) => {
    setDrawerOpen(false)
    if (focus) rootRef.current?.querySelector('#mobile-menu')?.focus({ preventScroll: true })
  }, [])
  function openModal(type, extra = {}) {
    if (!modal) returnFocusRef.current = document.activeElement
    setDrawerOpen(false)
    setModal({ type, projectId: selectedId, ...extra })
  }
  function closeModal(type) { setModal((current) => current?.type === type ? null : current) }
  function selectProject(id) { if (projects.some((item) => item.id === id)) setSelectedId(id) }
  function record(employee, text, projectId) { setActivities((current) => [{ employee, text, projectId, time: '방금 전' }, ...current]) }
  function updateProject(id, changes) { setProjects((current) => current.map((item) => item.id === id ? { ...item, ...changes, time: '방금 전' } : item)) }

  // 요청·승인·진행률·예시 파일은 메모리 안의 목업입니다. 실제 API는 호출하지 않습니다.
  function createProject(name, request) {
    sequence.current += 1
    const id = `draft-${sequence.current}`
    setProjects((current) => [{ id, name, description: request, phase: 0, needsApproval: false, time: '방금 전', scope: [request], files: ['docs/project-plan.md'] }, ...current])
    setSelectedId(id); setSearch(''); setModal(null)
    record(0, '대표님의 새 업무 요청을 받았어요. 먼저 기획안을 정리할게요.', id)
    changeView('home'); notify('기획 에이전트에게 업무를 맡겼어요. 3D 오피스의 시연 컨트롤로 흐름을 살펴보세요.')
  }
  function approvePlan() {
    const target = projects.find((item) => item.id === modal?.projectId)
    if (!target?.needsApproval) return
    updateProject(target.id, { needsApproval: false, phase: 1, files: target.id.startsWith('draft-') ? ['docs/project-plan.md', 'src/pages/Project.tsx', 'src/styles/tokens.css'] : target.files })
    setSelectedId(target.id); setModal(null)
    record(null, '기획안을 승인했어요. 개발 에이전트가 개발을 시작합니다.', target.id)
    changeView('home'); notify('기획안을 승인했어요. 목업의 개발 에이전트가 개발을 시작합니다.')
  }
  function feedback(text) {
    const target = projects.find((item) => item.id === modal?.projectId)
    if (!target?.needsApproval) return
    updateProject(target.id, { feedback: text, scope: [...target.scope, text], needsApproval: false, phase: 0 })
    setSelectedId(target.id); setModal(null)
    record(null, '기획안 수정을 요청했어요. 기획 에이전트가 피드백을 반영합니다.', target.id)
    changeView('home'); notify('기획 에이전트에게 피드백을 전달했어요. 목업의 기획 작업을 다시 시작합니다.')
  }
  function advance() {
    if (project.needsApproval || project.phase === 3) return
    if (project.phase === 0) {
      updateProject(project.id, { needsApproval: true })
      record(0, '기획안을 준비했어요. 대표님께 확인을 요청합니다.', project.id)
    } else {
      updateProject(project.id, { phase: project.phase + 1 })
      record(2, project.phase === 1 ? '개발을 마친 기능을 보안 에이전트와 함께 확인하고 있어요.' : '기능·보안 검토를 마친 목업 상태예요. 결과물을 확인해주세요.', project.id)
      if (project.phase === 2 && motion && !document.hidden && !celebrated.current.has(project.id)) {
        celebrated.current.add(project.id); setCelebration(project.id)
        clearTimeout(celebrationTimer.current)
        celebrationTimer.current = setTimeout(() => setCelebration(null), 3700)
      }
    }
    notify('디자인 목업의 다음 상태로 바뀌었어요. 실제 작업 실행 결과는 아닙니다.')
  }
  function focusStep(event) {
    const index = Number(event.currentTarget.dataset.focusStep)
    setHandoffStep(index)
    const element = Array.from(rootRef.current?.querySelectorAll(`[data-handoff-step="${index}"]`) || []).find((item) => !item.closest('[hidden]'))
    if (!element) return
    const box = element.getBoundingClientRect()
    const targetY = innerWidth <= 600 ? Math.min(innerHeight * 0.63, 490) : innerHeight * 0.5
    window.scrollTo({ top: Math.max(0, scrollY + box.top + box.height * 0.42 - targetY), behavior: motion ? 'smooth' : 'instant' })
  }
  function download(index) {
    const target = projects.find((item) => item.id === modal?.projectId) || project
    const file = target.files[index]
    if (!file) return
    const url = URL.createObjectURL(new Blob([artifactContent(target, index)], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url; link.download = file.split('/').pop()
    document.body.append(link); link.click(); link.remove()
    downloads.current.set(url, setTimeout(() => { URL.revokeObjectURL(url); downloads.current.delete(url) }, 1000))
    notify('목업용 예시 파일을 다운로드했어요.')
  }

  return <div ref={rootRef} className={`atelier-dashboard ${motion ? 'motion-ready' : 'motion-paused'}${mobile && drawerOpen ? ' nav-open' : ''}`}>
    <button type="button" className="sidebar-dismiss" aria-label="회사 메뉴 닫기" tabIndex={-1} onClick={() => closeDrawer(true)} />
    <AtelierIcons />
    <div className="app"><div className="titlebar"><span className="window-dots" aria-hidden="true"><i /><i /><i /></span><span>AI 에이전트 회사 / company workspace</span><span className="titlebar-note">AI AGENT COMPANY</span></div>
      <div className="layout"><AtelierSidebar view={view} mode="multi" count={projects.length} onView={changeView} mobile={mobile} open={drawerOpen} onClose={closeDrawer} onRunScenario={onRunScenario} onOpenDocuments={onOpenDocuments} onLogout={onLogout} />
        <main className="main"><AtelierHeader view={view} drawerOpen={mobile && drawerOpen} onMenu={() => setDrawerOpen(!drawerOpen)} motion={motion} reducedMotion={reducedMotion} onMotion={() => setMotionRequested(!motionRequested)} onNew={requestNew} />
          <div className="content"><header className="page-header"><div><p className="eyebrow">{copy[1]}</p><h1 id="page-title" tabIndex={-1} key={view}>{(copy[2].match(/\S+\s*/g) || []).map((word, index) => <span className="headline-word" style={{ '--word-index': index }} key={index}>{word}</span>)}</h1><p className="page-description">{copy[3]}</p></div><OfficeDate /></header>
            <div>
            <div id="home-view" className="view" hidden={view !== 'home'}><HomeView officeScene={view === 'home' ? <AgentOffice3D requestText={project.description} motionEnabled={motion} runDisplayState={runDisplayState} displayMode={officeMode} onDisplayModeChange={(mode) => setOfficeModeChoice({ runId: runQuery.runId, mode })} /> : null} mode="multi" projects={projects} project={project} activities={activities} onSelect={selectProject} onCreate={createProject} onNew={() => openModal('new')} onEmployee={(index) => openModal('employee', { index })} onPlan={(projectId) => openModal('plan', { projectId })} onAdvance={advance} onWorklog={() => openModal('worklog')} /><p className="arm-note" role="status" data-run-polling={runMonitor.polling.state}>자동 갱신: {pollingLabels[runMonitor.polling.state]} <button type="button" className="button" disabled={!runQuery.runId || officeMode !== 'actual' || ['finished', 'not_multi'].includes(runMonitor.polling.state)} onClick={runMonitor.polling.state === 'paused' ? runMonitor.resumePolling : runMonitor.pausePolling}>{runMonitor.polling.state === 'paused' ? '자동 갱신 재개' : '자동 갱신 일시정지'}</button></p><AgentRunMonitor monitor={runMonitor} query={runQuery} onSelectRun={selectRun} onRefresh={refreshRun} onChangePage={(offset) => dispatchRunQuery({ type: 'page', offset })} onClear={() => dispatchRunQuery({ type: 'select', runId: '' })} /></div>
            <section id="workflow-view" className="view" aria-label="팀의 업무 흐름" hidden={view !== 'workflow'}><WorkflowView project={project} activeStep={handoffStep} onStep={focusStep} onEmployee={(index) => openModal('employee', { index })} /></section>
            <section id="projects-view" className="view" aria-label="전체 프로젝트" hidden={view !== 'projects'}><ProjectsView projects={projects} search={search} onSearch={setSearch} onSelect={(id) => { selectProject(id); changeView('home') }} /></section>
            <section id="team-view" className="view" aria-label="직원 소개" hidden={view !== 'team'}><TeamView project={project} onEmployee={(index) => openModal('employee', { index })} /></section>
            <section id="outputs-view" className="view" aria-label="결과물 보관함" hidden={view !== 'outputs'}><OutputsView projects={projects} project={project} onSelect={selectProject} onFile={(index) => openModal('file', { index })} onDownload={download} /></section>
            </div>
            <footer className="page-footer"><span>목업 프로젝트·3D는 시연용입니다. 실제 실행 정보는 오피스의 Run 조회 영역에서 확인하세요.</span><span>AI 에이전트 회사 — a company of your own.</span></footer>
          </div>
        </main>
      </div>
    </div>
    {modal && <AtelierDialog key={modal.type} modal={modal} project={projects.find((item) => item.id === modal.projectId) || project} projects={projects} activities={activities} onClose={() => closeModal(modal.type)} onCreate={createProject} onApprove={approvePlan} onRevision={() => openModal('revision', { projectId: modal.projectId })} onFeedback={feedback} onDownload={download} returnFocusRef={returnFocusRef} rootRef={rootRef} />}
    {toast && <div className="toast" role="status">{toast}</div>}
    {motion && celebration && <div className="office-celebration" aria-hidden="true">{Array.from({ length: 22 }, (_, index) => <i key={index} style={{ '--paper-left': `${4 + index * 4.25}%`, '--paper-width': `${6 + index % 4 * 3}px`, '--paper-height': `${9 + index % 3 * 6}px`, '--paper-color': ['#D4FF24', '#1558C9', '#e6e8de', '#98a58e'][index % 4], '--paper-delay': `${index % 5 * 75}ms`, '--paper-drift': `${(index % 2 ? 1 : -1) * (35 + index % 6 * 15)}px`, '--paper-rotation': `${(index % 2 ? 1 : -1) * (120 + index * 21)}deg` }} />)}</div>}
  </div>
}
