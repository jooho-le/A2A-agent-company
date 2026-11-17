import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { AtelierIcons } from '../components/atelier/AtelierArtwork'
import { AtelierHeader, AtelierSidebar } from '../components/atelier/AtelierChrome'
import AgentOffice3D from '../components/atelier/AgentOffice3D'
import { runLabel } from '../components/atelier/roleLabels'
import useStudioMotion from '../components/atelier/useStudioMotion'
import RunWorkPanel from '../components/atelier/RunWorkPanel'
import { humanAgents } from '../components/atelier/humanAgents'
import useRunInteraction from '../hooks/useRunInteraction'
import WorkflowRunView from '../components/atelier/WorkflowRunView'
import useOfficeRunSubmission from '../hooks/useOfficeRunSubmission'
import { initialRunSelection, runEventLimit, runQueryReducer } from '../hooks/agentRunSelection'
import { savedProjectCards, submittedRunProject } from '../components/atelier/agentRunProject'
import useSavedProjects from '../hooks/useSavedProjects'
import { loadSelectedProject, readProjectSelection } from '../hooks/projectSelection'
import { buildAgentRunDisplayState } from '../components/atelier/agentRunDisplayState'
import AtelierDialog from '../components/atelier/AtelierDialog'
import { HomeView, OutputsView, ProjectsView, TeamView } from '../components/atelier/AtelierViews'
import { employees, viewCopy } from '../components/atelier/atelierMock'
import { useMediaQuery } from '../components/atelier/useAtelierMotion'
import useAgentRunMonitor from '../hooks/useAgentRunMonitor'
import { hasStoredAccessToken } from '../api/productRunRequest'
import './AtelierDashboard.css'
import '../components/atelier/ImmersiveOffice.css'

const emptyProject = { id: 'unassigned', name: '우리 팀', description: '', phase: -1, files: [], scope: [], needsApproval: false }
function readView() {
  const route = window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : 'home'
  if (['projects', 'outputs'].includes(route) && !hasStoredAccessToken()) return 'home'
  return Object.hasOwn(viewCopy, route) ? route : 'home'
}
export default function AtelierDashboard({ onOpenDocuments, onLogout, onLogin, hasAccessToken = hasStoredAccessToken(), requestDraft, onRequestDraftChange }) {
  const rootRef = useRef(null)
  const returnFocusRef = useRef(null)
  const [view, setView] = useState(readView)
  const [search, setSearch] = useState('')
  const [modal, setModal] = useState(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [officePanelOpen, setOfficePanelOpen] = useState(true)
  const [motionRequested, setMotionRequested] = useState(true)
  const [selectedRole, setSelectedRole] = useState(null)
  const [activeWork, setActiveWork] = useState(null)
  const [restoreError, setRestoreError] = useState('')
  const [runQuery, dispatchRunQuery] = useReducer(runQueryReducer, initialRunSelection)
  const runMonitor = useAgentRunMonitor(runQuery.runId, { limit: runEventLimit, offset: runQuery.offset, includeArchitecture: true,
    traceFromStart: runQuery.traceFromStart, collectEvents: ['home', 'workflow'].includes(view), includeResults: true,
    polling: ['home', 'workflow', 'projects', 'outputs'].includes(view) })
  const runDisplayState = buildAgentRunDisplayState(runMonitor)
  const controls = useRunInteraction(runQuery.runId, ['home', 'workflow'].includes(view), runMonitor.refresh)
  const selectOfficeAgent = useCallback(index => {
    setSelectedRole(humanAgents[index]?.id.toUpperCase() || 'ORCHESTRATOR'); setOfficePanelOpen(true)
  }, [])
  const mobile = useMediaQuery('(max-width:760px)')
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const motion = motionRequested && !reducedMotion
  const copy = viewCopy[view]
  useStudioMotion(rootRef, motion, view)

  function changeView(next) {
    if (!Object.hasOwn(viewCopy, next)) return
    if (['projects', 'outputs'].includes(next) && !hasStoredAccessToken()) { onLogin?.(); return }
    setView(next); setDrawerOpen(false)
    if (window.location.hash !== '#/' + next) window.history.pushState(null, '', '#/' + next)
    window.scrollTo({ top: 0, behavior: 'instant' })
  }
  const connectCreatedRun = useCallback(response => {
    setSelectedRole(null)
    setRestoreError('')
    dispatchRunQuery({ type: 'created', runId: response.run.runId })
    setActiveWork(response)
    setView('home'); setDrawerOpen(false)
    window.history.replaceState(null, '', '#/home')
    if (response.projectId) loadSelectedProject(response.projectId).then(project => {
      setActiveWork(current => current?.run?.runId === response.run.runId ? { ...current, projectName: project.name } : current)
    }).catch(() => { /* Run 관측은 유지하고 프로젝트 재조회 오류를 성공으로 처리하지 않습니다. */ })
  }, [])
  const submission = useOfficeRunSubmission(null, connectCreatedRun)
  const currentSubmission = activeWork ? { result: activeWork, name: activeWork.projectName, requestText: activeWork.requestText } : submission
  const actualProject = submittedRunProject(currentSubmission, runMonitor)
  const actualProjects = actualProject ? [actualProject] : []
  const project = actualProject ? { ...emptyProject, ...actualProject } : emptyProject
  const savedProjects = useSavedProjects(view === 'projects')
  const listedProjects = [...savedProjectCards(savedProjects.projects).filter(item => item.projectId !== activeWork?.projectId), ...actualProjects]

  const openRun = useCallback((savedProject, run) => {
    if (run.hostKey !== 'local-multi') return
    setSelectedRole(null)
    setRestoreError('')
    setActiveWork({ projectId: savedProject.projectId, projectName: savedProject.name,
      run: { runId: run.runId, scenarioId: savedProject.scenarioId } })
    dispatchRunQuery({ type: 'select', runId: run.runId })
    setModal(null); setView('home'); setDrawerOpen(false)
    window.history.replaceState(null, '', '#/home')
  }, [])
  useEffect(() => {
    const selected = readProjectSelection()
    if (!selected) return
    const controller = new AbortController()
    loadSelectedProject(selected, { signal: controller.signal }).then(saved => {
      const run = saved.runs.find(item => item.hostKey === 'local-multi')
      if (!controller.signal.aborted && run) openRun(saved, run)
    }).catch(error => { if (!controller.signal.aborted && error.name !== 'AbortError') setRestoreError(error.message) })
    return () => controller.abort()
  }, [openRun])
  useEffect(() => {
    const restore = () => { setView(readView()); setDrawerOpen(false) }
    window.addEventListener('hashchange', restore); window.addEventListener('popstate', restore)
    return () => { window.removeEventListener('hashchange', restore); window.removeEventListener('popstate', restore) }
  }, [])
  useEffect(() => {
    document.title = copy[0] + ' — AI 에이전트 회사'
    rootRef.current?.querySelector('.content')?.scrollTo({ top: 0, behavior: 'instant' })
    rootRef.current?.querySelector('#page-title')?.focus({ preventScroll: true })
  }, [copy])
  function requestNew() {
    const clearDraft = Boolean(submission.result?.run)
    if (submission.locked && !submission.reset()) { changeView('home'); setOfficePanelOpen(true); return }
    if (submission.busy) return
    if (clearDraft) onRequestDraftChange?.({ name: '', requestText: '' })
    changeView('home'); setOfficePanelOpen(true)
    requestAnimationFrame(() => {
      const requestPanel = rootRef.current?.querySelector('.office3d-request')
      if (requestPanel) requestPanel.open = true
      const input = rootRef.current?.querySelector('#request-name')
      const textarea = rootRef.current?.querySelector('#request-text')
      if (clearDraft && input) input.value = ''
      if (clearDraft && textarea) textarea.value = ''
      input?.focus({ preventScroll: true })
    })
  }
  const closeDrawer = useCallback((focus = false) => {
    setDrawerOpen(false)
    if (focus) rootRef.current?.querySelector('#mobile-menu')?.focus({ preventScroll: true })
  }, [])
  function openModal(type, extra = {}) {
    returnFocusRef.current = document.activeElement
    setDrawerOpen(false); setModal({ type, ...extra })
  }
  const workflowStatus = runQuery.runId ? Object.values(runMonitor.errors).some(Boolean)
    ? '일부 상태를 불러오지 못했어요. 다시 확인해주세요.'
    : activeWork?.dispatchStatus === 'NOT_CONFIGURED' ? '실행 서버 연결을 확인해주세요.'
      : runMonitor.run ? runLabel(runMonitor.run) : '진행 상황을 확인하고 있어요.' : ''

  const workPanel = runQuery.runId ? <RunWorkPanel key={runQuery.runId} runId={runQuery.runId} projectName={activeWork?.projectName}
    displayState={runDisplayState} controls={controls} selectedRole={selectedRole} onSelectRole={setSelectedRole}
    onResults={() => changeView('outputs')} onNew={requestNew} /> : null

  return <div ref={rootRef} className={'atelier-dashboard workspace-shell ux-simple ' + (motion ? 'motion-ready' : 'motion-paused') + (view === 'home' ? ' office-immersive' : '') + (mobile && drawerOpen ? ' nav-open' : '')}>
    <button type="button" className="sidebar-dismiss" aria-label="회사 메뉴 닫기" tabIndex={-1} onClick={() => closeDrawer(true)} />
    <AtelierIcons />
    <div className="app"><div className="layout">
      <AtelierSidebar view={view} mode="multi" count={listedProjects.length} onView={changeView} mobile={mobile} open={drawerOpen} onClose={closeDrawer}
        onRunScenario={requestNew} onOpenDocuments={onOpenDocuments} onLogout={onLogout} onLogin={onLogin} hasAccessToken={hasAccessToken} />
      <main className="main"><AtelierHeader view={view} drawerOpen={mobile && drawerOpen} onMenu={() => setDrawerOpen(!drawerOpen)} motion={motion} reducedMotion={reducedMotion} onMotion={() => setMotionRequested(!motionRequested)} onNew={requestNew} hasWork={Boolean(runQuery.runId)} />
        <div className="content"><header className="page-header"><div><h1 id="page-title" tabIndex={-1}>{copy[2]}</h1></div></header>
          {restoreError && <p role="alert">{restoreError}</p>}
          <div id="home-view" className="view" hidden={view !== 'home'}><HomeView mode="multi"
            submission={{ ...submission, draft: requestDraft, onDraftChange: onRequestDraftChange, onLogin: hasAccessToken ? null : onLogin, workflowStatus, finished: ['FINISHED', 'ABORTED'].includes(runMonitor.run?.status) }} onCreate={submission.submit}
            officeScene={view === 'home' ? <AgentOffice3D motionEnabled={motion} runDisplayState={runDisplayState} panelOpen={officePanelOpen} onPanelChange={setOfficePanelOpen} onRefresh={runMonitor.refresh} onProgress={() => changeView('workflow')} workPanel={workPanel} onAgentSelect={selectOfficeAgent} /> : null} /></div>
          <section id="workflow-view" hidden={view !== 'workflow'} aria-label="실제 업무 흐름">
            <WorkflowRunView runId={runQuery.runId} projectName={activeWork?.projectName} monitor={runMonitor} displayState={runDisplayState} status={workflowStatus} workPanel={view === 'workflow' ? workPanel : null} onNew={requestNew} onResults={() => changeView('outputs')} />
          </section>
          <section id="projects-view" hidden={view !== 'projects'}><ProjectsView projects={listedProjects} search={search} onSearch={setSearch} onSelect={() => changeView('home')} actual onNew={requestNew} onRetry={runMonitor.refresh} saved={savedProjects} onLogin={onLogin} onStoredSelect={projectId => openModal('saved-project', { projectId })} /></section>
          <section id="team-view" hidden={view !== 'team'}><TeamView project={project} onEmployee={index => openModal('employee', { index })} actualStates={runDisplayState.agents} /></section>
          <section id="outputs-view" hidden={view !== 'outputs'}><OutputsView projects={actualProjects} project={actualProject} onSelect={() => changeView('home')} actual catalog={runMonitor.artifactCatalog} onRetry={runMonitor.refresh} onBrowseProjects={() => changeView('projects')} /></section>
        </div>
      </main>
    </div></div>
    {modal && <AtelierDialog key={modal.type + ':' + (modal.projectId || modal.index)} modal={modal} project={project} projects={[]} activities={[]} onClose={() => setModal(null)} onLogin={onLogin} onOpenRun={openRun} presentation={{ detailState: { label: runDisplayState.agents.find(agent => agent.agentRole === employees[modal.index]?.role.toUpperCase())?.label || (modal.index === 4 ? '업무 조율' : '업무 대기'), task: '' } }} returnFocusRef={returnFocusRef} rootRef={rootRef} />}
  </div>
}
