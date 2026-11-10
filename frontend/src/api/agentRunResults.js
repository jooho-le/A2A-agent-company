// 팝업에 필요한 공개 필드만 남깁니다. 설정·환경·원본 로그는 React 상태에 복사하지 않습니다.
export function safePopupText(value, limit = 150) {
  if (typeof value !== 'string') return ''
  // 잘라내기 전에 전체 문장을 검사해 잘린 키 이름 뒤의 비밀 값도 노출하지 않습니다.
  if (/(?:api[ _-]?key|access[ _-]?key|secret|token|password|passwd|authorization|cookie|비밀번호|암호|토큰|인증키|BEGIN .*PRIVATE KEY|\bBearer\b|\bsk-[\w-]+|\bAKIA[0-9A-Z]+|eyJ[\w-]+\.[\w-]+\.[\w-]+|traceback|```)/i.test(value)) {
    return '민감한 내용은 생략했습니다.'
  }
  const text = value.replace(/https?:\/\/\S+/gi, '[주소 생략]').replace(/\p{Cc}/gu, ' ').replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

const list = value => Array.isArray(value) ? value : []
const identity = row => ({ artifactId: row.artifactId, artifactType: row.artifactType, runId: row.runId,
  workflowStepId: row.workflowStepId, createdBy: row.createdBy, artifactVersion: row.artifactVersion,
  codeVersion: row.codeVersion ?? null, a2aTaskId: row.a2aTaskId })
const result = row => ({ outcome: row.outcome, title: safePopupText(row.title, 70), details: safePopupText(row.details),
  actualResult: safePopupText(row.actualResult) })

export function popupArtifacts(rows) {
  return rows.filter(row => row && ['REQUIREMENT', 'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'].includes(row.artifactType)).map(row => {
    const base = identity(row)
    switch (row.artifactType) {
      case 'REQUIREMENT': return { ...base, requirementCount: list(row.requirementIds).length,
        requirementSummary: row.payload?.schemaVersion === 1 ? safePopupText(list(row.payload.requirements)[0]?.description) : '' }
      case 'CHANGE_REPORT': return { ...base, summary: safePopupText(row.summary), changedFileCount: list(row.fileChanges).length }
      case 'BUILD_REPORT': return { ...base, executionOutcome: row.executionOutcome, exitCode: row.exitCode,
        failureKind: row.failureKind, diagnostic: safePopupText(list(row.diagnostics)[0]?.message) }
      case 'QA_REPORT': return { ...base, tests: list(row.tests).map(result) }
      case 'SECURITY_REPORT': return { ...base, requirementResults: list(row.requirementResults).map(result),
        findings: list(row.findings).map(item => ({ severity: item.severity, disposition: item.disposition,
          title: safePopupText(item.title, 70), description: safePopupText(item.description) })) }
      default: return base
    }
  })
}

export function popupIssues(rows) {
  return rows.filter(Boolean).map(row => ({ issueId: row.issueId, runId: row.runId, codeVersion: row.codeVersion,
    sourceArtifactId: row.sourceArtifactId, reportArtifactId: row.reportArtifactId, reporter: row.reporter,
    severity: safePopupText(row.severity, 20), title: safePopupText(row.title, 70), description: safePopupText(row.description),
    fixWorkflowStepId: row.fixWorkflowStepId, newCodeVersion: row.newCodeVersion, revalidationResult: row.revalidationResult }))
}
