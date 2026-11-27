import { popupArtifacts, safePopupText } from './agentRunResults.js'
import { runUuid } from './agentRunSubmission.js'

const types = { REQUIREMENT: '요구사항', SOURCE: '소스 Snapshot', CHANGE_REPORT: '변경 보고서',
  BUILD_REPORT: '빌드 보고서', QA_REPORT: 'QA 보고서', SECURITY_REPORT: '보안 보고서', RUN_CONFIGURATION: '실행 설정 기록' }
const authors = { PLANNER: '계획 에이전트', DEVELOPER: '개발 에이전트', QA: 'QA 에이전트', SECURITY: '보안 에이전트', Orchestrator: '오케스트레이터' }
const actions = { ADDED: '추가', MODIFIED: '수정', DELETED: '삭제' }
const outcomes = new Set(['PASS', 'FAIL', 'UNVERIFIED', 'ERROR', 'SUCCESS'])
const list = value => Array.isArray(value) ? value : []

function registeredName(row) {
  try {
    const uri = new URL(row.artifactUri)
    if (uri.protocol !== 'artifact:' || uri.username || uri.password || uri.search || uri.hash) return ''
    return safePopupText(decodeURIComponent(uri.pathname.split('/').filter(Boolean).at(-1) || ''), 120)
  } catch { return '' }
}

// 목록 API의 응답에서 공개 표시값만 추출합니다. URI/설정/manifest/원본 로그는 보관하지 않습니다.
export function artifactCatalog(rows, runId) {
  return rows.filter(row => row?.runId === runId && runUuid.test(row.artifactId)).map(row => {
    const popup = popupArtifacts([row])[0]
    const details = [popup?.requirementSummary, popup?.summary, popup?.diagnostic].filter(Boolean)
    for (const result of [...list(popup?.tests), ...list(popup?.requirementResults)]) {
      details.push([result.title, outcomes.has(result.outcome) ? result.outcome : '', result.details, result.actualResult].filter(Boolean).join(' · '))
    }
    for (const finding of list(popup?.findings)) details.push([finding.title, finding.description].filter(Boolean).join(' · '))
    const changes = row.artifactType === 'CHANGE_REPORT' ? list(row.fileChanges).filter(file => typeof file?.path === 'string'
      && !file.path.startsWith('/') && !file.path.includes('\\') && !file.path.includes(':')
      && file.path.split('/').every(part => part && part !== '.' && part !== '..') && actions[file.action])
      .map(file => ({ path: safePopupText(file.path, 240), action: actions[file.action] })) : []
    return { artifactId: row.artifactId, runId, type: types[row.artifactType] || '기타 산출물',
      source: row.artifactType === 'SOURCE', name: registeredName(row),
      author: authors[row.createdBy] || '생성 주체 미확인',
      createdAt: typeof row.createdAt === 'string' && Number.isFinite(Date.parse(row.createdAt)) ? new Date(row.createdAt).toISOString() : '',
      version: Number.isSafeInteger(row.artifactVersion) ? row.artifactVersion : null,
      codeVersion: Number.isSafeInteger(row.codeVersion) ? row.codeVersion : null,
      details: details.filter(Boolean), changes }
  })
}
