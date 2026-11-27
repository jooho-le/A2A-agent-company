import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Icon } from './AtelierArtwork'

// 장식용 입체 오브젝트입니다. 생성 파일이나 실제 작업 상태를 표현하지 않습니다.
export function StudioObject({ kind = 'folder' }) {
  return <span className={`studio-object studio-object--${kind}`} aria-hidden="true"><span className="studio-object-shadow" /><span className="studio-object-space">
    {kind === 'folder' ? <>
      <span className="studio-folder-back" /><span className="studio-sheet studio-sheet--one" /><span className="studio-sheet studio-sheet--two" />
      <span className="studio-folder-front"><Icon name="folder" /><span /></span>
    </> : kind === 'monitor' ? <>
      <span className="studio-monitor-stand" /><span className="studio-monitor"><span className="studio-monitor-bar" /><Icon name="code" /><span className="studio-screen-lines" /></span>
      <span className="studio-monitor-note"><Icon name="file" /></span>
    </> : <>
      <span className="studio-sheet studio-sheet--one" /><span className="studio-sheet studio-sheet--two" />
      <span className="studio-document"><Icon name="file" /><span className="studio-screen-lines" /><span className="studio-document-seal" /></span>
    </>}
  </span></span>
}

export function StudioBanner({ kind, children }) {
  return <div className={`studio-banner studio-banner--${kind}`}>
    <div className="studio-banner-copy">{children}</div>
    <div className="studio-banner-art"><StudioObject kind={kind} /><span className="studio-orbit-tile studio-orbit-tile--one" /><span className="studio-orbit-tile studio-orbit-tile--two" /></div>
  </div>
}

// 모든 카드를 DOM에 유지합니다. 버튼·터치·키보드로 같은 목록을 탐색합니다.
export function StudioRail({ label, className, count, children }) {
  const id = useId()
  const ref = useRef(null)
  const [edges, setEdges] = useState({ previous: false, next: false })
  const measure = useCallback(() => {
    const node = ref.current
    if (!node) return
    const previous = node.scrollLeft > 3
    const next = node.scrollWidth - node.clientWidth - node.scrollLeft > 3
    setEdges(old => old.previous === previous && old.next === next ? old : { previous, next })
  }, [])
  useEffect(() => {
    const node = ref.current
    if (!node) return
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    for (const child of node.children) observer.observe(child)
    return () => observer.disconnect()
  }, [count, measure])
  function move(direction) {
    const node = ref.current
    const card = node?.firstElementChild
    if (!node || !card) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches || node.closest('.motion-paused')
    node.scrollBy({ left: direction * (card.getBoundingClientRect().width + 20), behavior: reduced ? 'auto' : 'smooth' })
  }
  return <div className="studio-collection">
    {count > 1 && <div className="studio-rail-controls" role="group" aria-label={`${label} 넘기기`}>
      <button type="button" aria-label={`${label} 이전`} aria-controls={id} onClick={() => move(-1)} disabled={!edges.previous}><Icon name="chevron" /></button>
      <button type="button" aria-label={`${label} 다음`} aria-controls={id} onClick={() => move(1)} disabled={!edges.next}><Icon name="chevron" /></button>
    </div>}
    <div id={id} ref={ref} className={`${className} studio-rail`} role="region" aria-label={label} tabIndex={count ? 0 : undefined} onScroll={measure}>{children}</div>
  </div>
}
