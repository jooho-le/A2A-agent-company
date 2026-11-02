import { useLayoutEffect, useRef } from 'react'
import { Icon } from './AtelierArtwork'
import { employees } from './atelierMock'

function SingleRelay({ steps }) {
  const relayRef = useRef(null)
  useLayoutEffect(() => {
    const relay = relayRef.current
    function alignStops() {
      const box = relay.getBoundingClientRect()
      if (!box.width) return
      const style = getComputedStyle(relay)
      const leftBorder = parseFloat(style.borderLeftWidth) || 0
      const topBorder = parseFloat(style.borderTopWidth) || 0
      // 화면 크기와 테두리를 반영한 실제 아이콘 중심을 정지 위치로 사용합니다.
      relay.querySelectorAll('.relay-node-icon').forEach((icon, index) => {
        const rect = icon.getBoundingClientRect()
        relay.style.setProperty(`--single-relay-x-${index}`, `${rect.left - box.left - leftBorder + rect.width / 2}px`)
        if (index === 0) relay.style.setProperty('--single-relay-y', `${rect.top - box.top - topBorder + rect.height / 2}px`)
      })
    }
    alignStops()
    const observer = new ResizeObserver(alignStops)
    observer.observe(relay)
    relay.querySelectorAll('.relay-node-copy').forEach((copy) => observer.observe(copy))
    return () => observer.disconnect()
  }, [])
  return <div ref={relayRef} className="team-relay single-relay" aria-label="기린 한 명의 기획, 개발, QA, 보안 검토 순차 업무"><span className="relay-track" aria-hidden="true" /><span className="relay-packet" aria-hidden="true"><Icon name="file" /></span><ol className="single-relay-steps">{steps.map(({ name: stage }, index) => {
    const { state, label } = steps[index]
    return <li className={`relay-node${state === 'active' ? ' is-working' : state === 'done' ? ' is-done' : ''}`} key={stage} data-state={state}><span className="relay-node-icon"><Icon name={['plan', 'code', 'check', 'shield'][index]} /></span><span className="relay-node-copy"><strong>{index + 1}. {stage}</strong><small>{label}</small></span></li>
  })}</ol></div>
}


export default function AgentRelay({ project, mode, steps }) {
  if (mode === 'single') return <SingleRelay steps={steps} />
  return <div className="team-relay" data-phase={project.phase} data-waiting={String(project.needsApproval)} aria-label="기획, 개발, 품질·보안 검토 업무 전달">
          <span className="relay-track" aria-hidden="true" /><span className="relay-packet" aria-hidden="true"><Icon name="file" /></span>
          {[[employees[0].name, '기획', 'plan'], [employees[1].name, '개발', 'code'], [`${employees[2].name} · ${employees[3].name}`, '품질·보안 동시 검토', 'shield']].map(([name, label, icon], index) => <span key={icon} className={`relay-node${project.needsApproval && index === 0 ? ' is-done' : project.phase === index && !project.needsApproval ? ' is-working' : project.phase > index ? ' is-done' : ''}`}><span className="relay-node-icon"><Icon name={icon} /></span><span className="relay-node-copy"><strong>{name}</strong><small>{label}</small></span></span>)}
        </div>
}
