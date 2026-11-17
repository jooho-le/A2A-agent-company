// 화면에서만 쓰는 쉬운 이름입니다. API 역할과 상태 값은 변경하지 않습니다.
const names = { orchestrator: '팀장', planner: '기획', developer: '개발', qa: '품질 검사', security: '보안 검사' }
export function roleLabel(role, fallback = role) {
  return names[String(role).toLowerCase()] || fallback
}

export function runLabel(run) {
  if (run?.status === 'FINISHED') return {
    SUCCESS: '작업을 마쳤어요.', FAIL: '검사를 통과하지 못했어요.',
    UNVERIFIED: '검사를 끝내지 못했어요.', HUMAN_REVIEW: '직접 확인이 필요해요.',
  }[run.verdict] || '작업이 끝났어요. 결과를 확인해주세요.'
  return {
    RECEIVED: '요청을 받았어요.', PLANNING: '만들 순서를 정하고 있어요.', IMPLEMENTING: '기능을 만들고 있어요.',
    SNAPSHOT_READY: '검사를 준비하고 있어요.', VALIDATING: '품질과 보안을 확인하고 있어요.',
    FIX_REQUIRED: '고칠 부분이 있어요.', FIXING: '발견한 문제를 고치고 있어요.', REVALIDATING: '수정한 내용을 다시 확인하고 있어요.',
    WAITING_INPUT: '확인이 필요해 잠시 멈췄어요.', HUMAN_REVIEW: '직접 확인이 필요해요.', ABORTED: '작업이 중단됐어요.',
  }[run?.status] || '작업 상태를 확인할 수 없어요.'
}
