import { useEffect, useReducer } from 'react'
import { loadSelectedProject } from '../../hooks/projectSelection'
import { officeScenarioId } from '../../api/officeService'

// Preserve the server's timezone information; do not assume naive dates are UTC.
function Timestamp({ value }) {
  return <time dateTime={value}>{value.replace('T', ' ')}</time>
}

export function SavedProjectDetailContent({ status, project, error, onRetry, onLogin, onOpenRun }) {
  if (status === 'loading') return <p role="status">프로젝트를 여는 중…</p>
  if (status === 'error') return <div role="alert">
    <p>{error.message}</p>
    {error.code === 'AUTH_REQUIRED'
      ? <button type="button" className="button" onClick={onLogin}>로그인</button>
      : <button type="button" className="button" onClick={onRetry}>다시 불러오기</button>}
  </div>
  if (!project) return null
  return <>
    <h3>{project.name}</h3>
    <details className="ux-details"><summary>프로젝트 정보</summary><dl className="saved-project-fields">
      <dt>작업 유형</dt><dd>{project.scenarioId === officeScenarioId ? '회원가입 기능 제작' : '기타 시나리오'}</dd>
      <dt>생성일</dt><dd><Timestamp value={project.createdAt} /></dd>
    </dl></details>
    <h3 className="detail-section-title">작업 기록 <span>({project.runs.length})</span></h3>
    {project.runs.length ? <ol className="saved-project-runs" aria-label="저장된 Run 연결 이력">
      {project.runs.map((run, index) => <li key={JSON.stringify([run.hostKey, run.runId])}>
        <div className="saved-run-heading"><strong>작업 {index + 1}</strong><Timestamp value={run.linkedAt} /></div>
        {run.hostKey === 'local-multi' && onOpenRun && <button type="button" className="button start" onClick={() => onOpenRun(project, run)}>진행 보기</button>}
      </li>)}
    </ol> : <p className="dialog-description">아직 시작한 작업이 없어요.</p>}
  </>
}

export default function SavedProjectDetail({ projectId, onLogin, onOpenRun }) {
  const [state, update] = useReducer((_state, next) => next, { status: 'loading', project: null, error: null })
  const [attempt, retry] = useReducer(value => value + 1, 0)
  useEffect(() => {
    const controller = new AbortController()
    let current = true
    update({ status: 'loading', project: null, error: null })
    loadSelectedProject(projectId, { signal: controller.signal }).then(project => {
      if (current) update({ status: 'success', project, error: null })
    }).catch(error => {
      if (current && error.name !== 'AbortError') update({ status: 'error', project: null, error })
    })
    return () => { current = false; controller.abort() }
  }, [projectId, attempt])
  return <div className="dialog-body saved-project-detail">
    <SavedProjectDetailContent {...state} onRetry={retry} onLogin={onLogin} onOpenRun={onOpenRun} />
  </div>
}
