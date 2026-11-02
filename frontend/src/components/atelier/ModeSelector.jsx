export function ModeSelector({ mode, onMode }) {
  return <fieldset className="execution-modes"><legend>업무 수행 방식</legend><div className="execution-options">{[['multi', '멀티 에이전트', '네 직원이 역할을 나누어 협업'], ['single', '싱글 에이전트', '싱글 AI가 모든 단계를 순차적으로 수행']].map(([value, label, description]) => <button key={value} type="button" data-mode={value} aria-pressed={mode === value} onClick={() => onMode(value)}>{label}<small>{description}</small></button>)}</div><p className="execution-mode-note">화면 전환은 목업이며 실제 Agent Run을 생성하지 않습니다.</p></fieldset>
}

