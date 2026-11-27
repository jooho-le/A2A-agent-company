import { runApiTransport } from './productRunRequest.js'
import { runUuid } from './agentRunSubmission.js'

const commandStates = new Set(['QUEUED', 'RUNNING', 'SUCCEEDED', 'REJECTED', 'UNKNOWN'])
const roles = new Set(['PLANNER', 'DEVELOPER', 'QA', 'SECURITY'])
const failureActions = new Set(['REVIEW_OR_NEW_RUN', 'WAIT_FOR_REVALIDATION', 'CHECK_PROVIDER'])

function invalidFailureState(failure) {
  return ['recovering', 'blocking', 'productFailure', 'workPreserved'].some(key =>
    failure[key] !== undefined && typeof failure[key] !== 'boolean')
    || (failure.nextAction !== undefined && !failureActions.has(failure.nextAction))
    || (failure.providerRetriesUsed !== undefined && (!Number.isInteger(failure.providerRetriesUsed)
      || failure.providerRetriesUsed < 0 || failure.providerRetriesUsed > 64))
    || (failure.validationIssues !== undefined && (!Array.isArray(failure.validationIssues)
      || failure.validationIssues.length > 64 || failure.validationIssues.some(issue => !issue
        || typeof issue.reason !== 'string' || issue.reason.length > 600
        || typeof issue.reasonCode !== 'string' || !/^[A-Z][A-Z0-9_]{2,63}$/.test(issue.reasonCode)
        || typeof issue.fieldPath !== 'string' || issue.fieldPath.length > 512)))
}

function path(runId) {
  if (!runUuid.test(runId)) throw new Error('작업을 먼저 선택해주세요.')
  return `/api/v1/runs/${runId}`
}
async function read(url, options = {}) {
  try {
    const response = await runApiTransport.fetch(url, { ...options, cache: 'no-store' })
    return await response.json()
  } catch (error) {
    if (error.name === 'AbortError') throw error
    const message = ({ 401: '다시 로그인해주세요.', 404: '요청 기록을 아직 확인할 수 없어요.',
      409: '작업 상태가 바뀌었어요. 최신 내용을 확인하고 다시 선택해주세요.',
      422: '답변할 직원과 입력 내용을 확인해주세요.' })[error.status]
    throw Object.assign(new Error(message || '서버의 처리 결과를 확인하지 못했어요. 처리 상태를 다시 확인해주세요.'), { status: error.status })
  }
}
export async function getRunInteraction(runId, signal) {
  const data = await read(path(runId) + '/interaction', { signal })
  if (data?.runId !== runId || !/^[0-9a-f]{64}$/.test(data.revision) || !Array.isArray(data.requests)
      || typeof data.canCancel !== 'boolean' || data.requests.some(row => !row || !runUuid.test(row.workflowStepId)
        || !roles.has(row.agentRole) || !['ANSWER', 'NEW_RUN', 'OPERATOR', 'UNAVAILABLE'].includes(row.replyMode)
        || typeof row.reason !== 'string' || !Array.isArray(row.questions)
        || row.questions.length > 8 || row.questions.some(text => typeof text !== 'string' || text.length > 2000))) {
    throw new Error('직원의 확인 요청을 읽을 수 없어요.')
  }
  if ((data.canAnnotate !== undefined && typeof data.canAnnotate !== 'boolean')
      || (data.continuation != null && (typeof data.continuation.available !== 'boolean'
        || data.continuation.parentRunId !== runId || data.continuation.preservesHistory !== true
        || data.continuation.reason != null && (typeof data.continuation.reason !== 'string' || data.continuation.reason.length > 600)))
      || (data.continuationUnavailable != null && (typeof data.continuationUnavailable !== 'string'
        || data.continuationUnavailable.length > 600))
      || (data.feedback !== undefined && (!Array.isArray(data.feedback) || data.feedback.length > 16
        || data.feedback.some(note => !note || !runUuid.test(note.feedbackId) || typeof note.text !== 'string'
          || note.text.length > 4000 || !['QUEUED', 'INCLUDED', 'NOT_APPLIED'].includes(note.status))))
      || (data.failures !== undefined && (!Array.isArray(data.failures) || data.failures.length > 4
        || data.failures.some(failure => !failure || !runUuid.test(failure.workflowStepId)
          || !roles.has(failure.agentRole) || typeof failure.message !== 'string' || failure.message.length > 600
          || typeof failure.instruction !== 'string' || failure.instruction.length > 600
          || invalidFailureState(failure))))) {
    throw new Error('추가 설명이나 실패 원인을 읽을 수 없어요. 다시 확인해주세요.')
  }
  return data
}
function receipt(data, runId, commandId) {
  if (data?.runId !== runId || data.commandId !== commandId || !commandStates.has(data.status)
      || !['CANCEL', 'RESUME', 'ANNOTATE'].includes(data.action)) throw new Error('요청 접수를 확인하지 못했어요. 처리 상태를 확인해주세요.')
  return data
}
export async function sendRunCommand(runId, commandId, body) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 10000)
  try {
    const data = await read(path(runId) + '/commands', { method: 'POST', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': commandId }, body: JSON.stringify(body) })
    return receipt(data, runId, commandId)
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('접수 응답이 늦어 처리 상태를 확인하고 있어요. 요청을 다시 보내지 않아요.', { cause: error })
    throw error
  } finally { clearTimeout(timer) }
}
export async function getRunCommand(runId, commandId, signal) {
  if (!runUuid.test(commandId)) throw new Error('요청 식별자를 확인할 수 없어요.')
  return receipt(await read(path(runId) + '/commands/' + commandId, { signal }), runId, commandId)
}
