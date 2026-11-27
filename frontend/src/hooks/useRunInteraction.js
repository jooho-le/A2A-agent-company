import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { getRunCommand, getRunInteraction, sendRunCommand } from '../api/runInteraction'
import { pendingOfficeSubmissionKey } from './pendingOfficeSubmission'
import { runUuid } from '../api/agentRunSubmission'

const pending = row => row && ['SENDING', 'QUEUED', 'RUNNING', 'UNKNOWN'].includes(row.status)
const finished = row => row && ['SUCCEEDED', 'REJECTED'].includes(row.status)

export default function useRunInteraction(runId, enabled, onChanged) {
  const [{ data, error, commands }, patch] = useReducer((state, next) => ({ ...state, ...next }), { data: null, error: '', commands: {} })
  const [revision, setRevision] = useState(0)
  const rows = useRef({})
  const sending = useRef(new Set())
  const owner = pendingOfficeSubmissionKey()
  const context = useRef({ runId: null, active: false })
  const onChangedRef = useRef(onChanged)
  useEffect(() => { onChangedRef.current = onChanged }, [onChanged])
  const storageKey = useCallback(action => owner && runId ? `${owner}.control.${runId}.${action}` : null, [owner, runId])
  const publish = useCallback((action, row) => {
    rows.current = { ...rows.current, [action]: row }
    patch({ commands: rows.current })
    try {
      const key = storageKey(action)
      if (key) finished(row) ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, JSON.stringify({
        runId: row.runId, commandId: row.commandId, action, status: row.status,
      }))
    } catch { /* In-memory duplicate prevention remains in force. */ }
  }, [storageKey])

  useEffect(() => {
    const identity = { runId, active: true }
    context.current = identity
    rows.current = {}; patch({ commands: {}, data: null, error: '' })
    if (!runId) return () => { identity.active = false }
    for (const action of ['CANCEL', 'RESUME', 'ANNOTATE']) {
      try {
        const key = storageKey(action)
        const saved = key ? JSON.parse(sessionStorage.getItem(key)) : null
        if (saved?.runId === runId && saved.action === action && runUuid.test(saved.commandId)) {
          rows.current[action] = { ...saved, status: 'UNKNOWN' }
        }
      } catch { /* A malformed local reference never authorizes execution. */ }
    }
    patch({ commands: rows.current })
    return () => { identity.active = false }
  }, [runId, storageKey])

  useEffect(() => {
    if (!enabled || !runId) return
    const controller = new AbortController()
    let timer
    async function poll() {
      try {
        const next = await getRunInteraction(runId, controller.signal)
        if (controller.signal.aborted) return
        patch({ data: next, error: '' })
        for (const [action, row] of Object.entries(rows.current)) {
          if (!pending(row) || row.status === 'SENDING') continue
          try {
            const receipt = await getRunCommand(runId, row.commandId, controller.signal)
            if (controller.signal.aborted) return
            publish(action, receipt)
            if (finished(receipt)) onChangedRef.current?.()
          } catch (failure) {
            if (!controller.signal.aborted) patch({ error: failure.message })
          }
        }
      } catch (failure) {
        if (!controller.signal.aborted) patch({ error: failure.message })
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 3000)
      }
    }
    poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [enabled, runId, revision, publish])

  async function submit(action, clarification, stepId) {
    if (!data || data.runId !== runId || sending.current.has(action) || pending(rows.current[action])) return false
    const identity = context.current
    const commandId = crypto.randomUUID()
    const body = { action, revision: data.revision, ...(action === 'RESUME' ? { workflowStepId: stepId, clarification }
      : action === 'ANNOTATE' ? { clarification } : {}) }
    sending.current.add(action); patch({ error: '' })
    publish(action, { runId, commandId, action, status: 'SENDING', createdAt: new Date().toISOString() })
    try {
      const receipt = await sendRunCommand(runId, commandId, body)
      if (!identity.active || context.current !== identity) return false
      publish(action, receipt); setRevision(value => value + 1); onChangedRef.current?.()
      return true
    } catch (failure) {
      if (!identity.active || context.current !== identity) return false
      if ([400, 401, 403, 404, 409, 422, 429].includes(failure.status)) {
        publish(action, { runId, commandId, action, status: 'REJECTED', createdAt: new Date().toISOString() })
      } else publish(action, { runId, commandId, action, status: 'UNKNOWN', createdAt: new Date().toISOString() })
      patch({ error: failure.message }); setRevision(value => value + 1); onChangedRef.current?.()
      return false
    } finally { sending.current.delete(action) }
  }
  const scopedCommands = Object.fromEntries(Object.entries(commands).filter(([, row]) => row.runId === runId))
  return { data: data?.runId === runId ? data : null, error, commands: scopedCommands,
    cancel: () => submit('CANCEL'), reply: (text, stepId) => submit('RESUME', text, stepId),
    annotate: text => submit('ANNOTATE', text),
    refresh: () => { setRevision(value => value + 1); onChangedRef.current?.() },
    cancelPending: pending(scopedCommands.CANCEL), replyPending: pending(scopedCommands.RESUME),
    annotatePending: pending(scopedCommands.ANNOTATE) }
}
