import { useEffect } from 'react'

// 프레임당 표시용 변수만 갱신합니다. 업무 상태나 React 콘텐츠는 수정하지 않습니다.
export default function useStudioMotion(rootRef, enabled, view) {
  useEffect(() => {
    const root = rootRef.current
    if (!root || view === 'home') return
    const cards = [...root.querySelectorAll('[data-studio-tilt]')].filter(node => !node.closest('[hidden]'))
    let frame = 0
    let current = null
    const fine = window.matchMedia('(hover: hover) and (pointer: fine)')
    function reset() {
      if (!current) return
      for (const name of ['--studio-rx', '--studio-ry', '--studio-x', '--studio-y']) current.style.removeProperty(name)
      current = null
    }
    function move(event) {
      if (!enabled || !fine.matches) return
      const card = event.target.closest('[data-studio-tilt]')
      cancelAnimationFrame(frame)
      if (!card || !root.contains(card)) { reset(); return }
      const { clientX, clientY } = event
      frame = requestAnimationFrame(() => {
        if (!card.isConnected) return
        if (current !== card) reset()
        current = card
        const box = card.getBoundingClientRect()
        const x = Math.max(0, Math.min(1, (clientX - box.left) / box.width))
        const y = Math.max(0, Math.min(1, (clientY - box.top) / box.height))
        card.style.setProperty('--studio-rx', `${(0.5 - y) * 8}deg`)
        card.style.setProperty('--studio-ry', `${(x - 0.5) * 10}deg`)
        card.style.setProperty('--studio-x', `${x * 100}%`)
        card.style.setProperty('--studio-y', `${y * 100}%`)
      })
    }
    function leave(event) { if (current && !(event.relatedTarget instanceof Node && current.contains(event.relatedTarget))) { cancelAnimationFrame(frame); reset() } }
    const observer = enabled ? new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) {
        entry.target.classList.add('studio-visible'); observer.unobserve(entry.target)
      }
    }, { root: root.querySelector('.content'), threshold: .06 }) : null
    for (const card of cards) {
      card.classList.add('studio-reveal')
      if (!enabled) card.classList.add('studio-visible')
      else observer.observe(card)
    }
    root.addEventListener('pointermove', move, { passive: true })
    root.addEventListener('pointerout', leave)
    return () => {
      cancelAnimationFrame(frame); reset(); observer?.disconnect()
      root.removeEventListener('pointermove', move); root.removeEventListener('pointerout', leave)
      for (const card of cards) card.classList.remove('studio-reveal', 'studio-visible')
    }
  }, [rootRef, enabled, view])
}
