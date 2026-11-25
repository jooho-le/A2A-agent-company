import { useEffect, useRef, useState } from 'react'
import aioLogo from '../../assets/aio-logo.webp'
import { Icon } from './AtelierArtwork'
import { viewCopy } from './atelierMock'

const koreaTimeZone = 'Asia/Seoul'
const dateFormatter = new Intl.DateTimeFormat('en-GB', { timeZone: koreaTimeZone, year: 'numeric', month: '2-digit', day: '2-digit' })
const weekdayFormatter = new Intl.DateTimeFormat('en-US', { timeZone: koreaTimeZone, weekday: 'long' })

export function OfficeDate() {
  const [today, setToday] = useState(() => new Date())

  useEffect(() => {
    let timer
    function scheduleMidnight() {
      clearTimeout(timer)
      const dayInMilliseconds = 24 * 60 * 60 * 1000
      const koreanTime = Date.now() + 9 * 60 * 60 * 1000
      // 한국 시간 자정이 지나면 화면을 새로고침하지 않아도 날짜와 요일을 바꿉니다.
      timer = setTimeout(() => {
        setToday(new Date())
        scheduleMidnight()
      }, dayInMilliseconds - koreanTime % dayInMilliseconds + 100)
    }
    function refresh() {
      setToday(new Date())
      scheduleMidnight()
    }
    function resume() { if (!document.hidden) refresh() }
    scheduleMidnight()
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', resume)
    return () => {
      clearTimeout(timer)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', resume)
    }
  }, [])

  const parts = dateFormatter.formatToParts(today)
  const part = (type) => parts.find((value) => value.type === type).value
  const date = `${part('year')}-${part('month')}-${part('day')}`
  return <div className="office-date"><time dateTime={date}>{date.replaceAll('-', '. ')}</time><strong>{weekdayFormatter.format(today).toUpperCase()}</strong></div>
}

export function AtelierSidebar({ view, mode, count, onView, mobile, open, onClose, onRunScenario, onOpenDocuments, onLogout, onLogin, hasAccessToken = false }) {
  const sidebarRef = useRef(null)
  const [accountOpen, setAccountOpen] = useState(false)
  useEffect(() => {
    const media = window.matchMedia('(max-width:760px)')
    const close = () => onClose()
    media.addEventListener('change', close)
    return () => media.removeEventListener('change', close)
  }, [onClose])
  useEffect(() => {
    if (!open || !mobile) return
    const sidebar = sidebarRef.current
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    sidebar.querySelector('[aria-current="page"]')?.focus()
    function keydown(event) {
      if (sidebar.querySelector('dialog[open]')) return
      if (event.key === 'Escape') { event.preventDefault(); onClose(true) }
      if (event.key === 'Tab') {
        const items = [...sidebar.querySelectorAll('a[href],button:not(:disabled)')].filter((item) => item.getClientRects().length)
        if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus() }
        else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus() }
      }
    }
    document.addEventListener('keydown', keydown)
    return () => {
      document.removeEventListener('keydown', keydown)
      document.body.style.overflow = previousOverflow
    }
  }, [open, mobile, onClose])
  return <aside ref={sidebarRef} className="sidebar" id="company-sidebar" aria-label="회사 메뉴" inert={mobile && !open ? true : undefined}>
    <a className="company-name" href="#/home" onClick={(event) => { event.preventDefault(); onView('home') }} aria-label="AIO 홈"><span className="company-monogram"><img className="aio-monogram-image" src={aioLogo} alt="" width="28" height="28" /></span><span>AIO</span></a>
    <span className="nav-caption">내 작업</span>
    <nav className="nav" aria-label="주 메뉴">{[['home', 'grid'], ['projects', 'folder'], ['workflow', 'arrow'], ['outputs', 'file']].map(([name, icon]) => <button key={name} type="button" className="nav-button" data-view={name} aria-current={view === name ? 'page' : undefined} onClick={() => onView(name)}><Icon name={icon} /><span>{viewCopy[name][0]}</span>{name === 'projects' && count > 0 && <span className="nav-count">{String(count).padStart(2, '0')}</span>}{name === 'team' && <span className="nav-count">{mode === 'single' ? '01' : '05'}</span>}</button>)}</nav>
    <button type="button" className="nav-button nav-secondary" data-view="team" aria-current={view === 'team' ? 'page' : undefined} onClick={() => onView('team')}><Icon name="team" />직원 소개</button>
    <div className="atelier-account">
      <button type="button" className="owner-profile" aria-expanded={hasAccessToken ? accountOpen : undefined} aria-controls={hasAccessToken ? 'atelier-account-menu' : undefined} onClick={() => hasAccessToken ? setAccountOpen(!accountOpen) : onLogin?.()}><span className="owner-avatar"><img className="aio-monogram-image" src={aioLogo} alt="" width="28" height="28" /></span><span><strong>{hasAccessToken ? '내 계정' : '로그인'}</strong></span></button>
      {hasAccessToken && accountOpen && <div className="atelier-account-menu" id="atelier-account-menu"><p>내 계정</p><button type="button" onClick={() => { setAccountOpen(false); onLogin?.() }}>{hasAccessToken ? '다시 로그인' : '로그인'}</button><button type="button" onClick={() => { setAccountOpen(false); onRunScenario?.() }}>새 업무 맡기기</button><button type="button" onClick={() => { setAccountOpen(false); onOpenDocuments?.() }}>전자문서 관리</button>{hasAccessToken && <button type="button" onClick={onLogout}>로그아웃</button>}</div>}
    </div>
  </aside>
}

export function AtelierHeader({ view, drawerOpen, onMenu, motion, reducedMotion, onMotion, onNew, hasWork }) {
  return <header className="topbar"><div className="breadcrumb"><button className="mobile-menu-button" id="mobile-menu" type="button" onClick={onMenu} aria-label={drawerOpen ? '회사 메뉴 닫기' : '회사 메뉴 열기'} aria-controls="company-sidebar" aria-expanded={drawerOpen}><Icon name="grid" /></button><span>우리 회사</span><Icon name="chevron" /><strong>{viewCopy[view][0]}</strong></div><div className="topbar-actions"><details className="view-options"><summary aria-label="화면 설정">···</summary><div><button type="button" className="motion-switch" aria-pressed={motion} onClick={onMotion} disabled={reducedMotion}>{motion ? '움직임 끄기' : '움직임 켜기'}</button></div></details>{(view !== 'home' || hasWork) && <button type="button" className="button compact" onClick={onNew}><Icon name="plus" />새로 만들기</button>}</div></header>
}
