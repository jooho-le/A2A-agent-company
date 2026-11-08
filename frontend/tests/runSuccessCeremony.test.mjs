import assert from 'node:assert/strict'
import test from 'node:test'
import { claimSuccessCeremony, createRunSuccessCeremony, isSuccessCeremonyEligible,
  readSuccessCeremonies, resetSuccessCeremonies, successCeremonyStorageKey as key } from '../src/components/atelier/runSuccessCeremony.js'
import { getAgentRunArchitecture } from '../src/api/agentRuns.js'

const ids = [1,2,3,4].map(index => `00000000-0000-4000-8000-${String(index).padStart(12,'0')}`)
const ready = { ready: true, motionEnabled: true }
function storage() {
  const values = new Map(), writes = []
  return { values, writes, getItem: name => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); writes.push({ name, value }) } }
}
function success(runId = ids[0], overrides = {}) {
  return { source: 'orchestrator', runId, architecture: 'MULTI_AGENT', availability: 'ready',
    queryStatus: { run: 'success', steps: 'success', events: 'empty', architecture: 'success' }, errors: {},
    run: { status: 'FINISHED', verdict: 'SUCCESS' }, ...overrides }
}
function controller(saved = storage()) { return { saved, gate: createRunSuccessCeremony({ getStorage: () => saved }) } }

test('서로 다른 성공 Run은 team→cheer→hug→team 순서로 하나씩 저장한다', () => {
  const saved = storage()
  assert.deepEqual(ids.map(id => claimSuccessCeremony(id,saved).pose), ['team','cheer','hug','team'])
  assert.equal(readSuccessCeremonies(saved).nextIndex, 1)
  assert.equal(readSuccessCeremonies(saved).runs.length, 4)
})
test('같은 Run·UUID 대소문자·다른 인스턴스는 순서를 다시 소비하지 않는다', () => {
  const { saved, gate } = controller()
  gate.observe(success(),'actual');assert.equal(gate.start({ ...ready, manual:true }),true)
  const another = createRunSuccessCeremony({getStorage:()=>saved})
  another.observe(success(),'actual')
  assert.equal(another.start({...ready,manual:true}),false)
  assert.equal(claimSuccessCeremony(ids[0].toUpperCase(),saved).claimed,false)
  assert.equal(saved.writes.length,1)
})
test('과거 성공 Run을 처음 조회하면 자동 재생하지 않고 명시적인 버튼을 기다린다', () => {
  const { gate, saved } = controller()
  gate.observe(success(),'actual')
  assert.equal(gate.start(ready),false)
  assert.equal(saved.writes.length,0)
  assert.equal(gate.start({...ready,manual:true}),true)
})
test('실제 모드에서 RUNNING 단계의 Run→최종 성공 전환을 관측하면 한 번 자동 시작한다', () => {
  const { gate, saved } = controller()
  gate.observe(success(ids[0],{run:{status:'VALIDATING',verdict:null}}),'actual')
  gate.observe(success(),'actual')
  assert.equal(gate.info().autoPending,true)
  assert.equal(gate.start(ready),true)
  gate.observe(success(),'actual')
  assert.equal(gate.start(ready),false)
  assert.equal(saved.writes.length,1)
})
test('Scene 준비 전에는 성공 전환을 보존하되 순서를 쓰지 않는다', () => {
  const { gate,saved }=controller()
  gate.observe(success(ids[0],{run:{status:'PLANNING',verdict:null}}),'actual')
  gate.observe(success(),'actual')
  assert.equal(gate.start({...ready,ready:false}),false)
  assert.equal(saved.writes.length,0)
  assert.equal(gate.start(ready),true)
})
test('모션 끄기·움직임 감소 상태에서는 재생 시작을 보류한다', () => {
  const { gate,saved }=controller();gate.observe(success(),'actual')
  assert.equal(gate.start({...ready,motionEnabled:false,manual:true}),false)
  assert.equal(saved.writes.length,0)
})
for(const verdict of ['FAIL','UNVERIFIED','HUMAN_REVIEW',null]) {
  test(`FINISHED + ${verdict}는 성공 세리머니가 아니다`,()=>{
    const {gate,saved}=controller();gate.observe(success(ids[0],{run:{status:'FINISHED',verdict}}),'actual')
    assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.writes.length,0)
  })
}
test('Step SUCCEEDED만으로 전체 성공을 만들지 않는다',()=>{
  const {gate,saved}=controller();gate.observe(success(ids[0],{run:{status:'VALIDATING',verdict:null},agents:[{displayState:'succeeded'}]}),'actual')
  assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.writes.length,0)
})
for(const architecture of ['SINGLE_AGENT',null,undefined,'UNRECOGNIZED']) {
  test(`architecture=${architecture}는 MULTI_AGENT로 추정하지 않는다`,()=>{
    assert.equal(isSuccessCeremonyEligible(success(ids[0],{architecture}),'actual'),false)
  })
}
test('목업 모드에서는 실제 성공을 관측해도 재생하거나 기록하지 않는다',()=>{
  const {gate,saved}=controller();gate.observe(success(),'mock')
  assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.writes.length,0)
})
for(const name of ['run','steps','events','architecture']) {
  test(`${name} 조회 실패·취소·조회 중 상태에서는 순서를 소비하지 않는다`,()=>{
    for(const state of ['error','cancelled','loading']) {
      const {gate,saved}=controller(), input=success()
      input.queryStatus[name]=state
      gate.observe(input,'actual')
      assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.writes.length,0)
    }
  })
}
test('저장소 읽기 오류와 쓰기 오류 시 재생·가짜 메모리 fallback이 없다',()=>{
  for(const kind of ['read','write']) {
    const saved=storage()
    if(kind==='read')saved.getItem=()=>{throw Error('차단')}
    else saved.setItem=()=>{throw Error('한도 초과')}
    const gate=createRunSuccessCeremony({getStorage:()=>saved});gate.observe(success(),'actual')
    assert.equal(gate.start({...ready,manual:true}),false)
    assert.equal(gate.info().storageError,true)
    assert.equal(gate.info().playback,null)
    assert.equal(saved.values.size,0)
  }
})
test('localStorage 속성 자체가 차단되어도 안전하게 중단한다',()=>{
  const gate=createRunSuccessCeremony({getStorage:()=>{throw Error('SecurityError')}})
  gate.observe(success(),'actual');assert.equal(gate.start({...ready,manual:true}),false)
  assert.equal(gate.info().storageError,true)
})
test('손상·불일치 기록은 자동 초기화하거나 덮어쓰지 않는다',()=>{
  for(const value of ['broken',JSON.stringify({version:1,nextIndex:2,runs:[]}),JSON.stringify({version:1,nextIndex:1,runs:[{runId:ids[0],pose:'hug'}]})]) {
    const {gate,saved}=controller();saved.values.set(key,value);gate.observe(success(),'actual')
    assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.values.get(key),value)
    assert.equal(saved.writes.length,0)
  }
})
test('저장 실패 후 자동 반복 시도를 하지 않고 수동 재시도를 기다린다',()=>{
  const {gate,saved}=controller(),write=saved.setItem.bind(saved)
  gate.observe(success(ids[0],{run:{status:'VALIDATING',verdict:null}}),'actual')
  saved.setItem=()=>{throw Error('쓰기 실패')};gate.observe(success(),'actual')
  assert.equal(gate.start(ready),false);saved.setItem=write
  assert.equal(gate.start(ready),false);assert.equal(gate.start({...ready,manual:true}),true)
})
test('재생은 끝에서 멈추고 같은 포즈를 연속으로 다시 시작하지 않는다',()=>{
  const {gate,saved}=controller();gate.observe(success(),'actual');gate.start({...ready,manual:true})
  const first=gate.advance(1000,ready),next=gate.advance(1000,ready)
  assert.deepEqual(first,next);assert.equal(first.completed,true);assert.equal(first.pose,'team')
  assert.equal(gate.start({...ready,manual:true}),false);assert.equal(saved.writes.length,1)
})
test('같은 Run 조회 중에는 일시정지하고 완료된 재조회 후 이어 재생한다',()=>{
  const {gate}=controller();gate.observe(success(),'actual');gate.start({...ready,manual:true})
  gate.advance(2,ready)
  const input=success();input.queryStatus.run='loading';gate.observe(input,'actual')
  assert.equal(gate.advance(3,ready).elapsed,2)
  gate.observe(success(),'actual');assert.equal(gate.advance(1,ready).elapsed,3)
})
test('다른 Run으로 바뀌면 이전 세리머니를 중단하고 UUID 이력은 보존한다',()=>{
  const {gate,saved}=controller();gate.observe(success(),'actual');gate.start({...ready,manual:true})
  gate.observe(success(ids[1]),'actual');assert.equal(gate.info().playback,null)
  assert.equal(gate.start(ready),false)
  assert.equal(readSuccessCeremonies(saved).runs[0].runId,ids[0])
})
test('중간 조회 오류·화면 재진입 이후 동일 Run은 다시 재생하지 않는다',()=>{
  const {gate,saved}=controller();gate.observe(success(),'actual');gate.start({...ready,manual:true})
  const input=success();input.errors.events=Error('연결 실패');gate.observe(input,'actual')
  assert.equal(gate.info().playback,null)
  const reload=createRunSuccessCeremony({getStorage:()=>saved});reload.observe(success(),'actual')
  assert.equal(reload.start({...ready,manual:true}),false);assert.equal(saved.writes.length,1)
})
test('실제 초기화와 목업 저장 키는 서로 독립이다',()=>{
  const saved=storage(),mockKey='atelier.office3d.ceremony-order.v1'
  saved.values.set(mockKey,'{"nextIndex":2}');claimSuccessCeremony(ids[0],saved)
  resetSuccessCeremonies(saved)
  assert.equal(saved.getItem(mockKey),'{"nextIndex":2}')
  assert.deepEqual(readSuccessCeremonies(saved),{version:1,nextIndex:0,runs:[]})
  saved.setItem(mockKey,'{"nextIndex":0}')
  assert.equal(readSuccessCeremonies(saved).runs.length,0)
})
test('초기화 후에는 이미 성공한 Run을 자동 재생하지 않는다',()=>{
  const {gate}=controller();gate.observe(success(),'actual');gate.start({...ready,manual:true})
  gate.advance(1000,ready);assert.equal(gate.reset(),true);gate.observe(success(),'actual')
  assert.equal(gate.start(ready),false);assert.equal(gate.start({...ready,manual:true}),true)
})

test('실제 configuration API는 계약을 검증하고 architecture 이외의 설정을 버린다',async()=>{
  const original=globalThis.fetch
  try {
    globalThis.fetch=async(path,options)=>{
      assert.equal(path,`/api/v1/runs/${ids[0]}/configuration`);assert.equal(options.method,'GET')
      return new Response(JSON.stringify({runId:ids[0],artifactType:'RUN_CONFIGURATION',configuration:{architecture:'MULTI_AGENT',model:{provider:'fixture-only'}}}))
    }
    assert.deepEqual(await getAgentRunArchitecture(ids[0]),{runId:ids[0],architecture:'MULTI_AGENT'})
    for(const data of [{runId:ids[1],artifactType:'RUN_CONFIGURATION',configuration:{architecture:'MULTI_AGENT'}},{runId:ids[0],configuration:{architecture:'MULTI_AGENT'}},{runId:ids[0],artifactType:'RUN_CONFIGURATION',configuration:{}}]) {
      globalThis.fetch=async()=>new Response(JSON.stringify(data))
      await assert.rejects(getAgentRunArchitecture(ids[0]),error=>error.code==='INVALID_RESPONSE')
    }
    for(const status of [404,422,503]) {
      globalThis.fetch=async()=>new Response(JSON.stringify({detail:'CONFIG_NOT_AVAILABLE'}),{status})
      await assert.rejects(getAgentRunArchitecture(ids[0]),error=>error.status===status&&error.code==='CONFIG_NOT_AVAILABLE')
    }
    const abort=new AbortController();abort.abort()
    await assert.rejects(getAgentRunArchitecture(ids[0],{signal:abort.signal}),error=>error.name==='AbortError')
  } finally { globalThis.fetch=original }
})
