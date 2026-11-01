// 원본 디자인의 예시 데이터입니다. 실제 Run·Artifact·검증 결과가 아닙니다.
export const portraits = {"planner": "/characters/deer_01.png", "developer": "/characters/개발_고양이_06.png", "qa": "/characters/qa_dog_01.png", "security": "/characters/security_rabbit_01.png"}

export const motionPortraits = {
    planner: {working: ["/characters/deer_04.png"], warning: "/characters/deer_06.png", done: "/characters/deer_07.png"},
    developer: {working: ["/characters/개발_고양이_05.png"], warning: "/characters/개발_고양이_09.png", done: "/characters/개발_고양이_08.png"},
    qa: {working: ["/characters/qa_dog_10.png"], warning: "/characters/qa_dog_03.png", done: "/characters/qa_dog_09.png"},
    security: {working: ["/characters/security_rabbit_07.png"], warning: "/characters/security_rabbit_10.png", done: "/characters/security_rabbit_08.png"}
  }

export const employees = [
    {id:'planner',name:'사슴',initial:'사',job:'기획 담당',role:'Planner',quote:'“복잡한 생각도, 차근차근 정리해볼게요.”',personality:'차분한 정리왕 · 꼼꼼한 기록가',purpose:'대표님의 요청을 이해하고, 만들 기능과 작업 순서를 정리합니다.',responsibilities:['업무 요청 분석 및 기능 명세 정리','작업 분해와 개발 순서 제안','대표님 피드백을 반영한 기획 수정']},
    {id:'developer',name:'고양이',initial:'고',job:'개발 담당',role:'Developer',quote:'“좋아요. 직접 작동하는 모습으로 보여드릴게요.”',personality:'묵묵한 실행가 · 만들기를 좋아함',purpose:'승인된 계획에 따라 화면과 기능을 만들고, 필요한 코드를 수정합니다.',responsibilities:['승인된 기능과 화면 구현','기존 코드 개선 및 수정 요청 반영','빌드와 변경 파일 정리']},
    {id:'qa',name:'강아지',initial:'강',job:'품질 담당',role:'QA',quote:'“사용하는 사람 입장에서 한 번 더 볼게요.”',personality:'호기심 많은 관찰자 · 디테일에 강함',purpose:'완성된 기능이 기대한 대로 작동하는지 사용자 관점에서 확인합니다.',responsibilities:['요구사항에 따른 확인 항목 정리','기능 테스트와 예외 상황 점검','발견한 문제와 재검토 결과 보고']},
    {id:'security',name:'토끼',initial:'토',job:'보안 담당',role:'Security',quote:'“안심하고 쓸 수 있도록, 빈틈을 살펴볼게요.”',personality:'신중한 수호자 · 원칙을 소중히 여김',purpose:'접근 권한, 입력 처리, 정보 보호에 문제가 없는지 살펴봅니다.',responsibilities:['코드와 접근 권한 점검','취약점과 정보 노출 가능성 확인','보완 요청 및 수정 내용 재검토']}
  ]

export const initialProjects = [
    {id:'orbit',name:'Orbit 브랜드 웹사이트',description:'제품 소개와 고객 문의를 연결하는 브랜드 웹사이트.',phase:1,needsApproval:false,time:'방금 전',scope:['브랜드와 제품을 소개하는 메인 페이지','고객이 메시지를 남기는 문의 폼','모바일에서도 편하게 보는 반응형 화면'],files:['src/pages/Home.tsx','src/components/ContactForm.tsx','src/styles/tokens.css']},
    {id:'flow',name:'팀 업무 관리 도구',description:'해야 할 일과 일정을 한곳에서 관리하는 팀의 업무 공간.',phase:0,needsApproval:true,time:'8분 전',scope:['팀의 해야 할 일을 정리하는 업무 보드','팀원이 함께 확인하는 일정 캘린더','프로젝트별 진행 현황 화면'],files:['docs/project-plan.md','docs/feature-list.md']},
    {id:'desk',name:'고객 지원 대시보드',description:'고객 문의를 정리하고 담당자와 답변 현황을 관리하는 도구.',phase:2,needsApproval:false,time:'14분 전',scope:['고객 문의 목록과 상세 화면','문의 상태와 담당자 변경','담당자만 작성할 수 있는 답변 기능'],files:['src/pages/Inbox.tsx','src/api/tickets.ts','tests/tickets.test.ts']}
  ]

export const initialActivities = [
    {employee:1,text:'문의 폼의 화면 구조를 정리했어요. 입력 기능을 만들고 있어요.',projectId:'orbit',time:'방금 전'},
    {employee:0,text:'업무 보드 기획안이 준비됐어요. 대표님의 확인을 기다려요.',projectId:'flow',time:'8분 전'},
    {employee:2,text:'토끼과 함께 기능 동작과 접근 권한을 확인하고 있어요.',projectId:'desk',time:'14분 전'}
  ]

export function agentStates(project){
    if(project.needsApproval)return [{state:'done',label:'기획 완료',task:'기획안을 정리했어요. 대표님의 확인을 기다려요.'},{state:'',label:'승인 대기',task:'기획안이 승인되면 바로 개발을 시작할게요.'},{state:'',label:'대기',task:'구현 후 기능을 차근차근 확인할게요.'},{state:'',label:'대기',task:'검토할 코드가 준비되면 합류할게요.'}];
    if(project.phase===0)return [{state:'active',label:'기획 중',task:'대표님의 요청을 기능과 작업으로 나누고 있어요.'},{state:'',label:'대기',task:'사슴의 기획안이 준비되기를 기다려요.'},{state:'',label:'대기',task:'만들어질 기능을 확인할 준비를 해요.'},{state:'',label:'대기',task:'검토할 코드가 준비되면 합류할게요.'}];
    if(project.phase===1)return [{state:'done',label:'완료',task:'기능 명세와 작업 순서를 정리했어요.'},{state:'active',label:'개발 중',task:'계획에 맞춰 화면과 기능을 만들고 있어요.'},{state:'',label:'준비 중',task:'확인해야 할 주요 항목을 정리하고 있어요.'},{state:'',label:'대기',task:'개발이 끝나면 보안을 확인할게요.'}];
    if(project.phase===2)return [{state:'done',label:'완료',task:'기능 명세와 작업 순서를 정리했어요.'},{state:'done',label:'완료',task:'화면과 기능 구현을 마쳤어요.'},{state:'active',label:'검토 중',task:'사용자 입장에서 기능을 확인하고 있어요.'},{state:'active',label:'검토 중',task:'접근 권한과 정보 보호를 확인하고 있어요.'}];
    return employees.map((employee,index)=>({state:'done',label:'완료',task:['기능 명세와 작업 순서를 정리했어요.','화면과 기능 구현을 마쳤어요.','주요 기능 확인을 마쳤어요.','보안 검토를 마쳤어요.'][index]}));
  }

export function artifactContent(project,index){
    const file=project.files[index];
    if(file.endsWith('.md'))return `# ${project.name}\n\n디자인 목업용 예시 문서입니다.\n\n## 업무 요청\n${project.description}\n\n## 작업 범위\n${project.scope.map(item=>'- '+item).join('\n')}\n${project.feedback?'\n## 대표님 피드백\n'+project.feedback+'\n':''}`;
    if(file.endsWith('.css'))return '/* 디자인 목업용 예시 코드 */\n:root {\n  --background: #030407;\n  --request-input: #1558C9;\n  --start-and-selected: #D4FF24;\n}\n';
    if(file.includes('test'))return `// 디자인 목업용 예시이며 실행 결과가 아닙니다.\ndescribe(${JSON.stringify(project.name)}, () => {\n  it.todo('주요 기능 동작 확인');\n  it.todo('잘못된 입력 처리 확인');\n  it.todo('접근 권한 확인');\n});\n`;
    return `// 디자인 목업용 예시입니다. 실제 생성 코드가 아닙니다.\nconst projectName = ${JSON.stringify(project.name)};\n\nexport default function ProjectPreview() {\n  return <main><h1>{projectName}</h1></main>;\n}\n`;
  }

export const progress = project => project.needsApproval ? 20 : [12, 62, 86, 100][project.phase]
export function projectStatus(project){return project.needsApproval?{text:'대표님 확인 대기',kind:'review'}:[{text:'기획 중',kind:''},{text:'개발 중',kind:''},{text:'품질·보안 검토',kind:''},{text:'작업 완료',kind:'complete'}][project.phase];}

export const viewCopy = {
  home: ['오피스', 'YOUR COMPANY, YOUR TEAM.', '대표님, 오늘도 좋은 일 만들어봐요.', '아이디어를 맡기고, 직원들의 진행 상황을 확인하세요.'],
  projects: ['프로젝트', 'WORK IN PROGRESS.', '우리 회사가 만들어가는 일.', '진행 중인 업무부터 완료한 프로젝트까지 한곳에서 살펴보세요.'],
  team: ['우리 직원', 'PEOPLE BEHIND THE WORK.', '대표님의 든든한 동료들이에요.', '각자의 성격과 전문성을 가진 AI 직원 네 명을 소개합니다.'],
  workflow: ['업무 흐름', 'HOW YOUR TEAM WORKS.', '팀의 업무가 이어지는 순간.', '하나의 일이 기획부터 개발·품질·보안까지 이어지는 과정을 살펴보세요.'],
  outputs: ['결과물 보관함', 'DELIVERED BY YOUR TEAM.', '팀의 작업을 하나씩 꺼내보세요.', '기획 문서부터 소스 코드까지, 프로젝트별로 모아뒀어요.'],
}
