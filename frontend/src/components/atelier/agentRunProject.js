import { buildAgentRunDisplayState } from './agentRunDisplayState.js'

const states = { RECEIVED: '요청 접수', PLANNING: '요구사항 분석', IMPLEMENTING: '개발 중', SNAPSHOT_READY: '소스 Snapshot 준비',
  VALIDATING: '검증 중', FIX_REQUIRED: '수정 필요', FIXING: '수정 중', REVALIDATING: '재검증 중',
  WAITING_INPUT: '입력 대기', HUMAN_REVIEW: '사람 검토 필요', ABORTED: '실행 중단' }
const verdicts = { SUCCESS: '업무 완료', FAIL: '종료 · 실패', UNVERIFIED: '종료 · 검증 미완료', HUMAN_REVIEW: '종료 · 사람 검토 필요' }

// 제품 목록 API에는 Run/Step 상태가 없습니다. 실행 상태·진행률·담당 Agent를 추측하지 않습니다.
export function savedProjectCards(projects) {
  return projects.map(project => ({ ...project, id: project.projectId, stored: true, actual: true,
    description: `시나리오 ID: ${project.scenarioId}`, time: project.createdAt.slice(0, 10),
    status: { text: '저장된 프로젝트', kind: 'active' }, agents: [],
    notice: '업무 실행 상태는 아직 조회하지 않았습니다.' }))
}

// 현재 화면에서 접수한 Run만 연결합니다. 전체 Run/프로젝트 목록 API를 가정하지 않습니다.
export function submittedRunProject(submission, monitor) {
  const accepted = submission?.result?.run
  if (!accepted?.runId || accepted.runId !== monitor.runId) return null
  const run = monitor.run?.runId === accepted.runId ? monitor.run : accepted
  const failed = Boolean(monitor.errors?.run)
  const text = run.status === 'FINISHED' ? verdicts[run.verdict] || '종료 · 최종 판정 미확인' : states[run.status] || '상태 확인 필요'
  const display = buildAgentRunDisplayState(monitor)
  return { actual: true, id: accepted.runId, runId: accepted.runId, name: submission.name || '접수한 업무',
    description: submission.requestText || '', time: '이 화면에서 접수',
    status: { text: failed ? '상태 조회 오류' : text, kind: !failed && run.status === 'FINISHED' && run.verdict === 'SUCCESS' ? 'done' : 'active' },
    agents: display.agents.map(agent => ({ role: agent.agentRole, name: agent.name, label: agent.label, assigned: Boolean(agent.step) })),
    notice: failed ? '상태를 불러오지 못했습니다. 실행 실패로 판정하지 않습니다.' : 'Run·Step 기준 · 진행률 수치는 제공되지 않습니다.' }
}
