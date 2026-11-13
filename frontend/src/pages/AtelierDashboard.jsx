import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { AtelierIcons } from '../components/atelier/AtelierArtwork'
import { AtelierHeader, AtelierSidebar, OfficeDate } from '../components/atelier/AtelierChrome'
import AgentOffice3D from '../components/atelier/AgentOffice3D'
import useOfficeRunSubmission from '../hooks/useOfficeRunSubmission'
import { initialRunSelection, runEventLimit, runQueryReducer } from '../hooks/agentRunSelection'
import { savedProjectCards, submittedRunProject } from '../components/atelier/agentRunProject'
import useSavedProjects from '../hooks/useSavedProjects'
import { readProjectSelection } from '../hooks/projectSelection'
import { buildAgentRunDisplayState } from '../components/atelier/agentRunDisplayState'
import AtelierDialog from '../components/atelier/AtelierDialog'
import { HomeView, OutputsView, ProjectsView, TeamView, WorkflowView } from '../components/atelier/AtelierViews'
import { artifactContent, initialActivities, initialProjects, viewCopy } from '../components/atelier/atelierMock'
import useAtelierMotion, { useMediaQuery } from '../components/atelier/useAtelierMotion'
import useAgentRunMonitor from '../hooks/useAgentRunMonitor'
import { hasStoredAccessToken } from '../api/productRunRequest'
import './AtelierDashboard.css'

function readView() {
  const route = window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : 'home'
  if (['projects', 'outputs'].includes(route) && !hasStoredAccessToken()) return 'home'
  return Object.hasOwn(viewCopy, route) ? route : 'home'
}

export default function AtelierDashboard({ onOpenDocuments, onLogout, onLogin, hasAccessToken = hasStoredAccessToken(), requestDraft, onRequestDraftChange, approvedRunSubmission = null }) {
  const rootRef = useRef(null)
  const returnFocusRef = useRef(null)
  const toastTimer = useRef(null)
  const downloads = useRef(new Map())
  const [view, setView] = useState(readView)
  const [projects, setProjects] = useState(() => structuredClone(initialProjects))
  const [activities, setActivities] = useState(initialActivities)
  const [selectedId, setSelectedId] = useState('orbit')
  const [search, setSearch] = useState('')
  const [modal, setModal] = useState(() => {
    const projectId = readProjectSelection()
    // 후보 ID로 기존 상세 창을 열고 서버 확인 후에만 내용을 표시합니다.
    return projectId ? { type: 'saved-project', projectId } : null
  })
  const [toast, setToast] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [motionRequested, setMotionRequested] = useState(true)
  const [handoffStep, setHandoffStep] = useState(0)
  // 실제 Run 조회는 Dashboard에서 한 번만 수행하며 목업 프로젝트·3D와 독립적으로 관리합니다.
  const [runQuery, dispatchRunQuery] = useReducer(runQueryReducer, initialRunSelection)
  const [officeModeChoice, setOfficeModeChoice] = useState({ runId: '', mode: 'mock' })
  const officeMode = officeModeChoice.runId === runQuery.runId ? officeModeChoice.mode : runQuery.runId ? 'actual' : 'mock'
  const runMonitor = useAgentRunMonitor(runQuery.runId, { limit: runEventLimit, offset: runQuery.offset, includeArchitecture: true,
    traceFromStart: runQuery.traceFromStart,
    collectEvents: view === 'home' && officeMode === 'actual',
    includeResults: view === 'home' && officeMode === 'actual' || ['projects', 'outputs'].includes(view),
    polling: view === 'home' && officeMode === 'actual' || ['projects', 'outputs'].includes(view) })
  // 공유할 실제 데이터를 표시용으로만 변환하고 기존 목업 시연에는 반영하지 않습니다.
  const runDisplayState = buildAgentRunDisplayState(runMonitor)
  const mobile = useMediaQuery('(max-width:760px)')
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const motion = motionRequested && !reducedMotion
  const project = projects.find((item) => item.id === selectedId)
  const copy = viewCopy[view]

  useAtelierMotion(rootRef, { enabled: motion, view, mode: 'multi', project, projectCount: projects.length, onStep: setHandoffStep })

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
      pendingDownloads.forEach((timer, url) => { clearTimeout(timer); URL.revokeObjectURL(url) })
      pendingDownloads.clear()
    }
  }, [])

  function requestNew() {
    changeView('home')
    requestAnimationFrame(() => {
      const input = rootRef.current?.querySelector('#request-name')
      input?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      input?.focus({ preventScroll: true })
    })
  }
  const connectCreatedRun = useCallback((response) => {
    const runId = response.run.runId
    dispatchRunQuery({ type: 'created', runId })
    setOfficeModeChoice({ runId, mode: 'actual' })
    setView('home'); setDrawerOpen(false)
    if (window.location.hash !== '#/home') window.history.pushState(null, '', '#/home')
  }, [])
  const submission = useOfficeRunSubmission(approvedRunSubmission, connectCreatedRun)
  const actualProject = submittedRunProject(submission, runMonitor)
  const actualProjects = actualProject ? [actualProject] : []
  const savedProjects = useSavedProjects(view === 'projects')
  // 프로젝트 ID와 Run ID는 별개입니다. 연결 이력 조회 없이 임의로 합치지 않습니다.
  const listedProjects = [...savedProjectCards(savedProjects.projects), ...actualProjects]
  function openActualProject() {
    if (!actualProject) return
    setOfficeModeChoice({ runId: actualProject.runId, mode: 'actual' })
    changeView('home')
  }
  const workflowStatus = runQuery.runId && officeMode === 'actual'
    ? Object.values(runMonitor.errors).some(error => error?.code === 'AUTH_REQUIRED')
      ? '로그인이 필요합니다. 기존 로그인 화면에서 로그인해 주세요.'
      : Object.values(runMonitor.errors).some(Boolean)
      ? '업무 상태를 불러오지 못했습니다. 마지막 상태를 유지하며 연결을 다시 확인합니다.'
      : runMonitor.run?.status === 'FINISHED'
        ? ({ SUCCESS: '업무가 성공적으로 완료되었습니다.', FAIL: '업무가 종료되었으며 최종 판정은 실패입니다.',
          UNVERIFIED: '업무가 종료되었지만 검증이 완료되지 않았습니다.', HUMAN_REVIEW: '업무가 종료되었으며 사람의 검토가 필요합니다.' })[runMonitor.run.verdict] || '업무가 종료되었습니다. 최종 판정을 확인하고 있습니다.'
        : runMonitor.run?.status === 'ABORTED' ? '업무 실행이 중단되었습니다.'
          : ['WAITING_INPUT', 'HUMAN_REVIEW'].includes(runMonitor.run?.status) ? '업무 진행에 추가 확인이 필요합니다.'
            : submission.result?.dispatchStatus === 'NOT_CONFIGURED' ? ''
              : runMonitor.run ? '에이전트의 업무 진행 상황을 자동으로 확인하고 있습니다.' : '접수된 업무 상태를 확인하고 있습니다.'
    : ''
  function notify(message) {
    clearTimeout(toastTimer.current)
    setToast(message)
    toastTimer.current = setTimeout(() => setToast(''), 4500)
  }
  function changeView(next) {
    if (!Object.hasOwn(viewCopy, next)) return
    if (['projects', 'outputs'].includes(next) && !hasStoredAccessToken()) {
      onLogin?.()
      return
    }
    setView(next); setDrawerOpen(false)
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
  function record(employee, text, projectId) { setActivities((current) => [{ employee, text, projectId, time: '방금 전' }, ...current]) }
  function updateProject(id, changes) { setProjects((current) => current.map((item) => item.id === id ? { ...item, ...changes, time: '방금 전' } : item)) }

  // 다른 메뉴에 보존된 프로젝트·결재·예시 파일은 목업입니다.
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
      <div className="layout"><AtelierSidebar view={view} mode="multi" count={listedProjects.length} onView={changeView} mobile={mobile} open={drawerOpen} onClose={closeDrawer} onRunScenario={requestNew} onOpenDocuments={onOpenDocuments} onLogout={onLogout} onLogin={onLogin} hasAccessToken={hasAccessToken} />
        <main className="main"><AtelierHeader view={view} drawerOpen={mobile && drawerOpen} onMenu={() => setDrawerOpen(!drawerOpen)} motion={motion} reducedMotion={reducedMotion} onMotion={() => setMotionRequested(!motionRequested)} onNew={requestNew} />
          <div className="content"><header className="page-header"><div><p className="eyebrow">{copy[1]}</p><h1 id="page-title" tabIndex={-1} key={view}>{(copy[2].match(/\S+\s*/g) || []).map((word, index) => <span className="headline-word" style={{ '--word-index': index }} key={index}>{word}</span>)}</h1><p className="page-description">{copy[3]}</p></div><OfficeDate /></header>
            <div>
            <div id="home-view" className="view" hidden={view !== 'home'}>
              <HomeView mode="multi" projects={projects} project={project} submission={{ ...submission, workflowStatus, draft: requestDraft, onDraftChange: onRequestDraftChange,
                finished: ['FINISHED', 'ABORTED'].includes(runMonitor.run?.status) }} onCreate={submission.submit}
                officeScene={view === 'home' ? <AgentOffice3D requestText={officeMode === 'actual' ? submission.requestText : project.description}
                  motionEnabled={motion} runDisplayState={runDisplayState} displayMode={officeMode}
                  onDisplayModeChange={(mode) => setOfficeModeChoice({ runId: runQuery.runId, mode })} /> : null} />
            </div>
            <section id="workflow-view" className="view" aria-label="팀의 업무 흐름" hidden={view !== 'workflow'}><WorkflowView project={project} activeStep={handoffStep} onStep={focusStep} onEmployee={(index) => openModal('employee', { index })} /></section>
            <section id="projects-view" className="view" aria-label="전체 프로젝트" hidden={view !== 'projects'}><ProjectsView projects={listedProjects} search={search} onSearch={setSearch} onSelect={openActualProject} actual onRetry={runMonitor.refresh} saved={savedProjects} onLogin={onLogin} onStoredSelect={(projectId) => openModal('saved-project', { projectId })} /></section>
            <section id="team-view" className="view" aria-label="직원 소개" hidden={view !== 'team'}><TeamView project={project} onEmployee={(index) => openModal('employee', { index })} /></section>
            <section id="outputs-view" className="view" aria-label="결과물 보관함" hidden={view !== 'outputs'}><OutputsView projects={actualProjects} project={actualProject} onSelect={openActualProject} actual catalog={runMonitor.artifactCatalog} onRetry={runMonitor.refresh} /></section>
            </div>
            <footer className="page-footer"><span>프로젝트에는 저장된 목록과 현재 접수한 업무를 표시합니다. 3D 오피스·보관함은 현재 Run을 관측하며, 목업 시연·직원 소개·업무 흐름의 역할 예시는 실행 결과와 별개입니다.</span><span>AI 에이전트 회사 — a company of your own.</span></footer>
          </div>
        </main>
      </div>
    </div>
    {modal && <AtelierDialog key={`${modal.type}:${modal.projectId}`} onLogin={onLogin} modal={modal} project={projects.find((item) => item.id === modal.projectId) || project} projects={projects} activities={activities} onClose={() => closeModal(modal.type)} onCreate={requestNew} onApprove={approvePlan} onRevision={() => openModal('revision', { projectId: modal.projectId })} onFeedback={feedback} onDownload={download} returnFocusRef={returnFocusRef} rootRef={rootRef} />}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>
}
