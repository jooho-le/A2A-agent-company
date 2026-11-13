import { useEffect, useReducer } from 'react'
import { loadSelectedProject } from '../../hooks/projectSelection'

// Preserve the server's timezone information; do not assume naive dates are UTC.
function Timestamp({ value }) {
  return <time dateTime={value}>{value.replace('T', ' ')}</time>
}

export function SavedProjectDetailContent({ status, project, error, onRetry, onLogin }) {
  if (status === 'loading') return <p role="status">프로젝트 상세와 Run 연결 이력을 불러오는 중입니다.</p>
  if (status === 'error') return <div role="alert">
    <p>{error.message}</p>
    {error.code === 'AUTH_REQUIRED'
      ? <button type="button" className="button" onClick={onLogin}>로그인</button>
      : <button type="button" className="button" onClick={onRetry}>다시 불러오기</button>}
  </div>
  if (!project) return null
  return <>
    <h3>{project.name}</h3>
    <dl className="saved-project-fields">
      <dt>프로젝트 ID</dt><dd>{project.projectId}</dd>
      <dt>시나리오 ID</dt><dd>{project.scenarioId}</dd>
      <dt>생성일</dt><dd><Timestamp value={project.createdAt} /></dd>
    </dl>
    <h3 className="detail-section-title">Run 연결 이력 <span>({project.runs.length})</span></h3>
    {project.runs.length ? <ol className="saved-project-runs" aria-label="저장된 Run 연결 이력">
      {project.runs.map(run => <li key={JSON.stringify([run.hostKey, run.runId])}>
        <dl className="saved-project-fields">
          <dt>Run ID</dt><dd>{run.runId}</dd>
          <dt>Host</dt><dd>{run.hostKey}</dd>
          <dt>Configuration ID</dt><dd>{run.configurationId}</dd>
          <dt>연결 시각</dt><dd><Timestamp value={run.linkedAt} /></dd>
        </dl>
      </li>)}
    </ol> : <p className="dialog-description">아직 연결된 Run이 없습니다.</p>}
    <p className="dialog-note">저장된 Run 연결 이력입니다. 실제 실행 상태는 아직 조회하지 않습니다.</p>
  </>
}

export default function SavedProjectDetail({ projectId, onLogin }) {
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
    <SavedProjectDetailContent {...state} onRetry={retry} onLogin={onLogin} />
  </div>
}
