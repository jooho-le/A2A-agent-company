import { useEffect, useRef, useState } from 'react'
import { hasStoredAccessToken } from '../api/productRunRequest'

const key = 'atelierAccountScreens'
const names = new Set(['login', 'signup', 'documents', 'pipeline'])
function readScreens() {
  const value = window.history.state?.[key]
  return Array.isArray(value) && value.length <= 4 && value.every(name => names.has(name)) ? value : []
}
function stateWith(screens) {
  const state = { ...window.history.state }
  if (screens.length) state[key] = screens
  else delete state[key]
  return state
}

// Only screen names enter History; credentials, drafts and server data stay out.
export default function useAccountNavigation() {
  const [screens, setScreens] = useState(readScreens)
  const traversing = useRef(false)
  useEffect(() => {
    const restore = () => { traversing.current = false; setScreens(readScreens()) }
    window.addEventListener('popstate', restore)
    window.addEventListener('hashchange', restore)
    return () => {
      window.removeEventListener('popstate', restore)
      window.removeEventListener('hashchange', restore)
    }
  }, [])
  function open(name) {
    if (!names.has(name) || traversing.current) return
    const current = readScreens()
    if (current.at(-1) === name) return
    const next = current.length < 4 ? [...current, name] : [...current.slice(0, -1), name]
    window.history[current.length < 4 ? 'pushState' : 'replaceState'](stateWith(next), '')
    setScreens(next)
  }
  function back(all = false) {
    const current = readScreens()
    if (traversing.current) return
    if (current.length) {
      traversing.current = true
      window.history.go(all ? -current.length : -1)
    }
    else setScreens([])
  }
  function reset() {
    traversing.current = false
    window.history.replaceState(stateWith([]), '', '#/home')
    setScreens([])
  }
  const selected = screens.at(-1) || null
  return { screen: ['documents', 'pipeline'].includes(selected) && !hasStoredAccessToken() ? 'login' : selected,
    open, back: () => back(), close: () => back(true), reset }
}
