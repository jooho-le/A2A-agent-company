/* eslint-disable react-refresh/only-export-components -- Isolated offline test entry. */
import { act, StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import useOfficeRunSubmission from '../../src/hooks/useOfficeRunSubmission'
import RunWorkPanel from '../../src/components/atelier/RunWorkPanel'
import { buildAgentRunDisplayState } from '../../src/components/atelier/agentRunDisplayState'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const scenarioId = '03d94bc5-1a31-473b-8527-c4acf8ea58a1'
const parentId = id(1), projectId = id(2), oldKey = id(3), childId = id(4)
const storageKey = 'ai-company.pending-office-submission.1'
let root, host, mode, calls, selected, current, releasePost, approvalCount
function check(value, message) { if (!value) throw new Error(message) }
const posts = () => calls.filter(call => call.method === 'POST')
const respond = (data, {status=200, headers={}}={}) => ({status, ok:status>=200 && status<300,
  headers:new Headers(headers), json:async () => data})
function created(key, runId, targetProject = projectId) {
  return { requestId:key, projectId:targetProject, submissionStatus:'CREATED',
    run:{runId, scenarioId}, firstStep:{runId}, dispatchStatus:'SCHEDULED' }
}
globalThis.fetch = async (url, options = {}) => {
  const method = options.method || 'GET'
  calls.push({url, method, options})
  if (url.startsWith('/api/v1/host/approved-configuration')) {
    approvalCount++
    if (mode === 'approval-failed' && approvalCount > 2) return respond({}, {status:503})
    return respond({scenarioId, configurationId:'a'.repeat(64), configuration:{architecture:'MULTI_AGENT'}})
  }
  if (url === '/api/v1/submissions/'+oldKey) {
    if (mode === 'missing') return respond({}, {status:404})
    return respond(mode === 'unknown' ? {requestId:oldKey, projectId:id(9), submissionStatus:'UNKNOWN'}
      : created(oldKey, id(8), id(9)))
  }
  if (url === '/api/v1/runs' && method === 'POST') {
    const key = new Headers(options.headers).get('Idempotency-Key')
    if (mode === 'active') return respond({detail:'CONTINUATION_ALREADY_ACTIVE'},
      {status:409, headers:{'X-Submission-Status':'NOT_ACCEPTED'}})
    if (mode === 'pending') await new Promise(resolve => { releasePost = resolve })
    return respond({...created(key, childId), continuationOf:parentId}, {status:201})
  }
  throw new Error('No real network fallback')
}
function Fixture() {
  const submission = useOfficeRunSubmission(null, result => selected.push(result))
  useEffect(() => { current = submission }, [submission])
  const run = {runId:parentId,status:'FINISHED',verdict:'FAIL',codeVersion:1,fixAttempt:1}
  const displayState = buildAgentRunDisplayState({runId:parentId,run,architecture:'MULTI_AGENT',
    status:{run:'success',steps:'success'},steps:[]})
  return <RunWorkPanel runId={parentId} projectName="원래 프로젝트" displayState={displayState}
    controls={{data:{runId:parentId,status:'FINISHED',canCancel:false,requests:[],
      continuation:{available:true,parentRunId:parentId,preservesHistory:true}}, commands:{}, refresh() {}}}
    onContinue={instruction => submission.submit('원래 프로젝트',instruction,{projectId,continuationOf:parentId})}
    continuationBusy={submission.busy} continuationError={submission.error} onResults={() => {}} onNew={() => {}} />
}
async function mount(next, pending = true) {
  if (root) await act(async () => root.unmount())
  host?.remove()
  sessionStorage.clear()
  sessionStorage.setItem('access_token','test.'+btoa(JSON.stringify({sub:'1'}))+'.test')
  if (pending) sessionStorage.setItem(storageKey,oldKey)
  mode=next;calls=[];selected=[];releasePost=null;approvalCount=0
  host=document.createElement('div');document.body.append(host);root=createRoot(host)
  await act(async () => root.render(<StrictMode><Fixture /></StrictMode>))
  check(posts().length===0,'page load never creates a Run')
}
async function clickContinue(twice = false) {
  await act(async () => [...host.querySelectorAll('button')].find(b=>b.textContent==='이어서 수정·재시도').click())
  const form = host.querySelector('form')
  check(form?.textContent.includes('기존 기록은 남고'),'continuation has an explicit history-preserving explanation')
  await act(async () => {
    form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))
    if (twice) form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))
  })
}
async function verify() {
  const passed=[]
  await mount('pending');await clickContinue(true)
  check(posts().length===1 && releasePost,'double click sends one new POST')
  const body=JSON.parse(posts()[0].options.body)
  check(body.projectId===projectId && body.continuationOf===parentId,'explicitly selected project and parent are sent')
  check(new Headers(posts()[0].options.headers).get('Idempotency-Key')!==oldKey,'new attempt gets a new idempotency key')
  check(selected.length===0,'settling an old receipt does not select its different project')
  await act(async () => releasePost())
  check(selected.length===1 && selected[0].run.runId===childId,'only the confirmed child is selected')
  passed.push('이전 접수 확인·대상 프로젝트 유지·중복 클릭 차단·새 실행 연결')
  for (const next of ['unknown','missing']) {
    await mount(next);await clickContinue()
    check(posts().length===0 && sessionStorage.getItem(storageKey)===oldKey,'unsettled receipt preserves its key and blocks new work')
    check(current.uncertain && !current.busy && selected.length===0,'unsettled receipt is explicit without changing selection')
  }
  passed.push('미확인·누락 접수 키 보존과 자동 재전송 차단')
  await mount('approval-failed');await clickContinue()
  check(posts().length===0 && !current.uncertain && !current.busy,'approval failure before POST is never an uncertain creation')
  passed.push('승인 조회 실패와 실제 접수 불확실성 구분')
  await mount('active',false);await clickContinue()
  check(posts().length===1 && !current.uncertain && !current.locked,'confirmed rejection never loops in unknown state')
  check(current.error.includes('이미 진행 중'),'confirmed conflict gives an actionable Korean message')
  passed.push('진행 중 수정 실행 충돌 안내와 불확실 접수 반복 방지')
  await act(async () => root.unmount())
  return {passed}
}
verify().then(result => {
  const pre=document.createElement('pre');pre.id='test-result';pre.textContent=JSON.stringify(result);document.body.append(pre)
}).catch(error => {
  const pre=document.createElement('pre');pre.id='test-result';pre.textContent=JSON.stringify({failed:error.message});document.body.append(pre)
})
