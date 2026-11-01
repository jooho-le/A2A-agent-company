import { useEffect, useRef, useState } from 'react'
import { Icon } from './AtelierArtwork'
import { viewCopy } from './atelierMock'



export function AtelierSidebar({ view, mode, count, onView, mobile, open, onClose, onRunScenario, onOpenDocuments, onLogout }) {
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
    <a className="brand" href="#/home" onClick={(event) => { event.preventDefault(); onView('home') }} aria-label="atelier 회사 홈"><Icon name="mark" /><span>atelier<span className="brand-period">.</span></span></a>
    <div className="company-name"><span className="company-monogram">J</span><span>주호의 소프트웨어 회사<small>MY AI COMPANY</small></span></div>
    <span className="nav-caption">WORKSPACE</span>
    <nav className="nav" aria-label="주 메뉴">{[['home', 'grid'], ['projects', 'folder'], ['team', 'team'], ['workflow', 'arrow'], ['outputs', 'file']].map(([name, icon]) => <button key={name} type="button" className="nav-button" data-view={name} aria-current={view === name ? 'page' : undefined} onClick={() => onView(name)}><Icon name={icon} /><span>{viewCopy[name][0]}</span>{name === 'projects' && mode !== 'single' && <span className="nav-count">{String(count).padStart(2, '0')}</span>}{name === 'team' && <span className="nav-count">{mode === 'single' ? '01' : '04'}</span>}</button>)}</nav>
    <div className="sidebar-memo"><span className="memo-label">OUR WAY OF WORKING</span><p>좋은 아이디어는 대표님이.<br />차근차근 만드는 일은 우리가.</p><span>기획 · 개발 · 품질 · 보안</span></div>
    <div className="atelier-account">
      <button type="button" className="owner-profile" aria-expanded={accountOpen} aria-controls="atelier-account-menu" onClick={() => setAccountOpen(!accountOpen)}><span className="owner-avatar">JH</span><span><strong>주호 대표님</strong><small>회사 오너 · Owner</small></span><span className="owner-tag">YOU</span></button>
      {accountOpen && <div className="atelier-account-menu" id="atelier-account-menu"><p>실제 연동 화면 · 목업과 별개</p><button type="button" onClick={onRunScenario}>Agent Pipeline 열기</button><button type="button" onClick={onOpenDocuments}>전자문서 관리</button><button type="button" onClick={onLogout}>로그아웃</button></div>}
    </div>
  </aside>
}

export function AtelierHeader({ view, drawerOpen, onMenu, motion, reducedMotion, onMotion, onNew }) {
  return <header className="topbar"><div className="breadcrumb"><button className="mobile-menu-button" id="mobile-menu" type="button" onClick={onMenu} aria-label={drawerOpen ? '회사 메뉴 닫기' : '회사 메뉴 열기'} aria-controls="company-sidebar" aria-expanded={drawerOpen}><Icon name="grid" /></button><span>주호의 회사</span><Icon name="chevron" /><strong>{viewCopy[view][0]}</strong></div><div className="topbar-actions"><span className="preview-label" title="진행률·기획안·파일은 실제 Orchestrator 결과가 아닙니다.">목업 데이터</span><button type="button" className="motion-switch" aria-pressed={motion} onClick={onMotion} disabled={reducedMotion} title={reducedMotion ? '기기의 모션 줄이기 설정을 적용했어요.' : '입체 효과와 움직임 켜기 또는 멈추기'}>{motion ? '모션 켜짐' : '모션 멈춤'}</button><button type="button" className="button compact" onClick={onNew}><Icon name="plus" />새 업무</button></div></header>
}
