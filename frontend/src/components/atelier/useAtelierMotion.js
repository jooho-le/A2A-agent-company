import { useCallback, useEffect, useSyncExternalStore } from 'react'

export function useMediaQuery(query) {
  const subscribe = useCallback((listener) => {
    const media = window.matchMedia(query)
    media.addEventListener('change', listener)
    return () => media.removeEventListener('change', listener)
  }, [query])
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches, () => false)
}

// React가 관리하는 내용은 바꾸지 않고 화면 안의 CSS 변수와 표시용 클래스만 갱신합니다.
export default function useAtelierMotion(rootRef, { enabled, view, project, projectCount, onStep }) {
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const all = (selector) => [...root.querySelectorAll(selector)]
    let frame = 0
    let pointerFrame = 0
    let activeStep = -1
    let tilted = null
    let pointedScene = null
    const fine = window.matchMedia('(hover: hover) and (pointer: fine)')
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target) }
      })
    }, { threshold: 0.06, rootMargin: '0px 0px -28px 0px' })
    const selectors = ['.request-composer', '.work-section > .section-heading', '.agent-grid .agent-card', '.approval-panel', '.brief-panel', '.activity-item', '.project-card', '.staff-profile', '.output-card', '.handoff-heading']
    selectors.forEach((selector) => all(selector).forEach((element, index) => {
      if (element.closest('[hidden]')) return
      element.classList.add('scroll-reveal')
      element.style.setProperty('--reveal-delay', `${Math.min(index, 3) * 65}ms`)
      if (!enabled) element.classList.add('is-visible')
      else observer.observe(element)
    }))
    function resetTilt() {
      tilted?.style.removeProperty('--badge-x'); tilted?.style.removeProperty('--badge-y'); tilted = null
      pointedScene?.style.removeProperty('--desk-pointer-x'); pointedScene?.style.removeProperty('--desk-pointer-y'); pointedScene = null
    }
    function updateScroll() {
      frame = 0
      if (view !== 'workflow') return
      const steps = all('[data-handoff-step]')
      const stage = root.querySelector('.handoff-office .office-diorama')
      if (!steps.length || !stage) return
      const targetY = innerWidth <= 600 ? Math.min(innerHeight * 0.63, 490) : innerHeight * 0.5
      let nearest = 0
      let distance = Infinity
      steps.forEach((step, index) => {
        const box = step.getBoundingClientRect()
        const delta = Math.abs(box.top + box.height * 0.42 - targetY)
        const offset = (box.top + box.height * 0.42 - targetY) / innerHeight
        step.style.setProperty('--step-angle', enabled && innerWidth > 900 ? `${Math.max(-6, Math.min(6, offset * -7)).toFixed(2)}deg` : '0deg')
        step.style.setProperty('--step-enter-y', enabled && innerWidth > 900 ? `${Math.max(-15, Math.min(15, offset * 22)).toFixed(2)}px` : '0px')
        if (delta < distance) { nearest = index; distance = delta }
      })
      const last = steps.at(-1).getBoundingClientRect()
      if (scrollY + innerHeight >= document.documentElement.scrollHeight - 4 && last.top < innerHeight && last.bottom > 0) nearest = steps.length - 1
      if (nearest !== activeStep) { activeStep = nearest; onStep(nearest) }
      const first = steps[0].getBoundingClientRect()
      const amount = Math.max(0, Math.min(1, (targetY - first.top) / (last.bottom - first.top)))
      root.querySelector('.handoff-steps').style.setProperty('--handoff-progress', amount.toFixed(3))
      stage.style.setProperty('--desk-turn', enabled ? `${(-5 + amount * 14).toFixed(2)}deg` : '0deg')
      stage.style.setProperty('--desk-lean', enabled ? `${(amount * 3).toFixed(2)}deg` : '0deg')
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(updateScroll) }
    function pointerMove(event) {
      if (!enabled || !fine.matches) return
      cancelAnimationFrame(pointerFrame)
      const card = event.target.closest('.agent-card')
      const host = event.target.closest('.request-composer,.handoff-sticky')
      const scene = host?.querySelector('.office-diorama')
      const { clientX: x, clientY: y } = event
      pointerFrame = requestAnimationFrame(() => {
        resetTilt()
        if (card?.isConnected) {
          const box = card.getBoundingClientRect(); tilted = card
          card.style.setProperty('--badge-x', `${((0.5 - (y - box.top) / box.height) * 10).toFixed(2)}deg`)
          card.style.setProperty('--badge-y', `${(((x - box.left) / box.width - 0.5) * 14).toFixed(2)}deg`)
        }
        if (scene?.isConnected) {
          const box = host.getBoundingClientRect(); pointedScene = scene
          const dx = Math.max(-0.5, Math.min(0.5, (x - box.left) / box.width - 0.5))
          const dy = Math.max(-0.5, Math.min(0.5, (y - box.top) / box.height - 0.5))
          scene.style.setProperty('--desk-pointer-x', `${(-dy * 9).toFixed(2)}deg`)
          scene.style.setProperty('--desk-pointer-y', `${(dx * 11).toFixed(2)}deg`)
          host.style.setProperty('--spot-x', `${(dx + 0.5) * 100}%`)
          host.style.setProperty('--spot-y', `${(dy + 0.5) * 100}%`)
        }
      })
    }
    function focus(event) { event.target.closest('.scroll-reveal')?.classList.add('is-visible') }
    function visibility() { root.classList.toggle('page-idle', document.hidden); if (document.hidden) resetTilt() }
    root.addEventListener('pointermove', pointerMove, { passive: true })
    root.addEventListener('pointerleave', resetTilt)
    root.addEventListener('focusin', focus)
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule, { passive: true })
    window.addEventListener('blur', resetTilt)
    document.addEventListener('visibilitychange', visibility)
    schedule()
    return () => {
      observer.disconnect(); cancelAnimationFrame(frame); cancelAnimationFrame(pointerFrame); resetTilt()
      root.removeEventListener('pointermove', pointerMove)
      root.removeEventListener('pointerleave', resetTilt)
      root.removeEventListener('focusin', focus)
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('blur', resetTilt)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [rootRef, enabled, view, project, projectCount, onStep])
}
