const intro = '/characters/giraffe_01_intro.png'

// 기존 PNG를 역할·목업 상태에 연결합니다. 서버 상태값과는 별개입니다.
const portraits = {
  planner: {
    working: '/characters/giraffe_02_planner_working.png',
    warning: '/characters/giraffe_03_planner_warning.png',
    done: '/characters/giraffe_04_planner_done.png',
  },
  developer: {
    working: '/characters/giraffe_05_developer_working.png',
    warning: '/characters/giraffe_06_developer_warning.png',
    done: '/characters/giraffe_07_developer_done.png',
  },
  qa: {
    working: '/characters/giraffe_08_qa_working.png',
    warning: '/characters/giraffe_09_qa_warning.png',
    done: '/characters/giraffe_10_qa_done.png',
  },
  security: {
    working: '/characters/giraffe_11_security_working.png',
    warning: '/characters/giraffe_12_security_warning.png',
    done: '/characters/giraffe_13_security_done.png',
  },
}
const roleLabels = { planner: '기획', developer: '개발', qa: 'QA', security: '보안 검토' }
const stateLabels = { working: '작업 중', warning: '문제 발견', done: '완료' }

export default function SingleGiraffe({ role, state = 'intro' }) {
  // 기본 인트로는 역할과 무관하게 같은 이미지를 사용합니다.
  const source = state === 'intro' ? intro : portraits[role]?.[state] || intro
  const label = source === intro ? '인트로' : `${roleLabels[role]} · ${stateLabels[state]}`
  return <span className="portrait single-giraffe" data-person="giraffe" data-role={role} data-state={state}>
    <span className="portrait-number">SINGLE / 01</span>
    <img src={source} alt={`싱글 AI 기린 캐릭터 · ${label}`} width={390} height={410} decoding="async" />
  </span>
}
