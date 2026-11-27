import { act } from 'react'
import { createRoot } from 'react-dom/client'
import App from '../../src/App.jsx'
import { officeScenarioId } from '../../src/api/officeService.js'
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const token = sub => 'review.' + btoa(JSON.stringify({sub})) + '.dummy'
const projectId=id(10), runIds=[id(11),id(12)], date='2026-11-19T00:00:00+09:00'
const project={projectId,name:'검토용 프로젝트',scenarioId:officeScenarioId,createdAt:date,updatedAt:date,runs:runIds.map(runId=>({runId,hostKey:'local-multi',configurationId:'c'.repeat(64),linkedAt:date}))}
let root, host, calls=[], loginSubject='2'
globalThis.fetch=async (url,options={})=>{
 const u=new URL(url,location.href),p=u.pathname,method=options.method||'GET'; calls.push({p,method})
 let data
 if(p==='/api/auth/login'&&method==='POST')data={access_token:token(loginSubject),token_type:'bearer'}
 else if(method!=='GET')throw Error('Review forbids work writes: '+p)
 else if(p==='/api/projects')data={projects:[project]}
 else if(p==='/api/projects/'+projectId)data=project
 else if(p==='/api/v1/scenarios')data={scenarios:[{scenarioId:officeScenarioId}]}
 else if(p==='/api/v1/host/approved-configuration')data={scenarioId:officeScenarioId,configurationId:'c'.repeat(64),configuration:{architecture:'MULTI_AGENT'}}
 else if(p==='/api/documents')data={documents:[]}
 else if(p.startsWith('/api/v1/runs/')){
  const runId=p.split('/')[4],run={runId,scenarioId:officeScenarioId,status:'FINISHED',verdict:'SUCCESS',codeVersion:1,fixAttempt:0}
  if(p.endsWith('/steps'))data={runId,steps:[{runId,workflowStepId:id(21),agentRole:'PLANNER',status:'SUCCEEDED',attempt:0,codeVersion:null}]}
  else if(p.endsWith('/configuration'))data={runId,artifactType:'RUN_CONFIGURATION',configuration:{architecture:'MULTI_AGENT'}}
  else if(p.endsWith('/events')){
   const limit=Number(u.searchParams.get('limit')),offset=Number(u.searchParams.get('offset'))
   const events=[1,2].map(n=>({runId,eventId:id(30+n),workflowStepId:id(21),eventType:'A2A_MESSAGE_SENT',actor:'Orchestrator',occurredAt:date,attempt:0,codeVersion:null}))
   data={runId,limit,offset,total:events.length,events:events.slice(offset,offset+limit)}
  }else if(p.endsWith('/artifacts'))data={runId,artifacts:[]}
  else if(p.endsWith('/issues'))data={runId,issues:[]}
  else if(p.endsWith('/interaction'))data={runId,revision:'a'.repeat(64),status:'FINISHED',canCancel:false,requests:[]}
  else data=run
 }else throw Error('No network fallback: '+p)
 return Response.json(data)
}
const find=(text,selector='button')=>Array.from(document.querySelectorAll(selector)).find(n=>n.textContent.trim()===text)
async function settle(){await act(async()=>{await new Promise(r=>setTimeout(r,20))})}
async function click(n){if(!n)throw Error('Review expected a button');await act(async()=>{n.click();await new Promise(r=>setTimeout(r,30))});await settle()}
async function nav(view){await click(document.querySelector(`[data-view=${view}]`))}
async function mount(selected=projectId){
 if(root){await act(async()=>root.unmount());host.remove()}
 sessionStorage.clear();sessionStorage.setItem('access_token',token('1'))
 if(selected)sessionStorage.setItem('ai-company.selected-project-id',selected)
 history.replaceState(null,'','#/home');calls=[];host=document.createElement('div');document.body.append(host);root=createRoot(host)
 await act(async()=>root.render(<App/>));await settle()
}
async function remount(){await act(async()=>root.unmount());host.remove();host=document.createElement('div');document.body.append(host);root=createRoot(host);await act(async()=>root.render(<App/>));await settle()}
async function back(){await act(async()=>{history.back();await new Promise(r=>setTimeout(r,50))});await settle()}
async function account(text){if(!document.querySelector('#atelier-account-menu'))await click(document.querySelector('.owner-profile'));await click(find(text,'#atelier-account-menu button'))}
async function fill(selector,value){await act(async()=>{const el=document.querySelector(selector);if(!el)throw Error('Missing input '+selector);const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}))})}
function check(value,message){if(!value)throw Error(message)}
async function forward(){await act(async()=>{history.forward();await new Promise(r=>setTimeout(r,50))});await settle()}
async function login(sub){loginSubject=sub;await fill('input[type=email]','review@example.invalid');await fill('input[type=password]','ReviewOnly!1234');await act(async()=>document.querySelector('.login-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await settle()}
async function verify(){const passed=[]
 await mount();await nav('projects');await click(document.querySelector('.project-card'))
 const choices=document.querySelectorAll('.saved-project-runs .start');check(choices.length===2,'two Run choices')
 await click(choices[1]);check(globalThis.__officeProps.runDisplayState.runId===runIds[1],'second Run selected')
 await nav('outputs');await remount();check(location.hash==='#/outputs','refresh preserves current page')
 await nav('home');check(globalThis.__officeProps.runDisplayState.runId===runIds[1],'refresh preserves chosen Run')
 passed.push('선택한 Run과 결과물 화면을 새로고침 후 그대로 복원')
 await nav('workflow');await settle();const records=document.querySelector('.workflow-records')
 check(records.querySelector('.count-pill').textContent==='2'&&!records.textContent.includes('아직 기록이 없어요.'),'stored records shown')
 check(globalThis.__officeProps.runDisplayState.trace.entries.length===0,'historical records never replay as animation')
 passed.push('과거 작업 기록은 표시하고 과거 애니메이션은 재생하지 않음')
 await nav('projects');await account('다시 로그인');check(document.querySelector('#login-title'),'login opened')
 await back();check(!document.querySelector('#login-title')&&location.hash==='#/projects','browser back closes login and restores page')
 await forward();check(document.querySelector('#login-title'),'browser forward restores login')
 await click(find('회원가입'));check(document.querySelector('#signup-title'),'signup opened')
 await back();check(document.querySelector('#login-title')&&!document.querySelector('#signup-title'),'signup back returns to login')
 await back();check(!document.querySelector('#login-title'),'login back returns to dashboard')
 await account('전자문서 관리');check(document.querySelector('.document-page'),'documents opened')
 await back();check(!document.querySelector('.document-page')&&location.hash==='#/projects','documents back restores projects')
 await forward();check(document.querySelector('.document-page'),'documents forward restores document screen')
 await click(find('오피스로 돌아가기'));check(!document.querySelector('.document-page'),'document close uses history')
 await account('다시 로그인');await click(find('회원가입'));await act(async()=>{const close=document.querySelector('[aria-label="회원가입 닫기"]');close.click();close.click();await new Promise(r=>setTimeout(r,50))});await settle()
 check(!document.querySelector('#signup-title')&&!document.querySelector('#login-title'),'nested signup close returns to dashboard')
 passed.push('로그인·회원가입·전자문서의 뒤로가기·앞으로가기·닫기와 Run 유지')
 await mount(null);await fill('#request-name','A 전용 검토 초안');await fill('#request-text','계정 A에서만 보관할 요청입니다.')
 await account('다시 로그인');await back();check(document.querySelector('#request-name').value==='A 전용 검토 초안','login cancellation preserves draft')
 await account('다시 로그인');await login('1');check(document.querySelector('#request-name').value==='A 전용 검토 초안','same account preserves draft')
 await account('다시 로그인');await login('2');check(document.querySelector('#request-name').value===''&&document.querySelector('#request-text').value==='','different account never sees old draft')
 check(calls.every(c=>c.method==='GET'||c.p==='/api/auth/login'),'no real work POST')
 passed.push('취소·동일 계정은 초안 유지, 다른 계정 로그인은 초안 분리')
 await act(async()=>root.unmount());return{passed}
}
verify().then(value=>{const n=document.createElement('pre');n.id='test-result';n.textContent=JSON.stringify(value);document.body.append(n)}).catch(error=>{const n=document.createElement('pre');n.id='test-result';n.textContent=JSON.stringify({failed:error.stack});document.body.append(n)})
