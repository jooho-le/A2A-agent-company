import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import AtelierDashboard from './pages/AtelierDashboard'
import DocumentPage from './pages/DocumentPage'
import { hasStoredAccessToken } from './api/productRunRequest'
import './App.css'

function App() {
  // 토큰 존재는 인증 성공의 보장이 아닙니다. 최초 진입은 공개 오피스입니다.
  const [hasAccessToken, setHasAccessToken] = useState(hasStoredAccessToken)
  const [showLogin, setShowLogin] = useState(false)
  const [requestDraft, setRequestDraft] = useState({ name: '', requestText: '' })
  const [showSignup, setShowSignup] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)
  const [showDocuments, setShowDocuments] = useState(false)
  const [loginNotice, setLoginNotice] = useState('')

  function handleLoginSuccess() {
    setHasAccessToken(hasStoredAccessToken())
    setShowLogin(false)
    setShowSignup(false)
    setShowPipeline(false)
    setShowDocuments(false)
    setLoginNotice('')
    window.history.replaceState(null, '', '#/home')
  }

  function openLogin() {
    setLoginNotice('로그인 후 업무 요청과 프로젝트 관리를 이용할 수 있습니다.')
    setShowSignup(false)
    setShowLogin(true)
  }

  function openProtected(action) {
    if (!hasStoredAccessToken()) { openLogin(); return }
    action()
  }

  function handleLogout() {
    sessionStorage.removeItem('access_token')
    setHasAccessToken(false)
    setShowLogin(false)
    setRequestDraft({ name: '', requestText: '' })
    setShowSignup(false)
    setShowPipeline(false)
    setShowDocuments(false)
    setLoginNotice('')
    window.history.replaceState(null, '', '#/home')
  }

  // 계정 메뉴에서 선택했을 때만 기존 로그인·회원가입 화면으로 이동합니다.
  if (showLogin) {
    if (showSignup) {
      return (
        <SignupPage
          onBack={() => setShowSignup(false)}
          onSignupSuccess={(message) => {
            setLoginNotice(`${message} 로그인하여 시작해주세요.`)
            setShowSignup(false)
          }}
        />
      )
    }

    return (
      <LoginPage
        notice={loginNotice}
        onBack={() => { setShowLogin(false); setShowSignup(false) }}
        onSignup={() => {
          setLoginNotice('')
          setShowSignup(true)
        }}
        onLoginSuccess={handleLoginSuccess}
      />
    )
  }

  if (showPipeline) {
    return <PipelinePage onBack={() => setShowPipeline(false)} />
  }

  if (showDocuments) {
    return <DocumentPage onBack={() => setShowDocuments(false)} />
  }

  return (
    <AtelierDashboard
      key={hasAccessToken ? 'credential-present' : 'guest'}
      onRunScenario={() => openProtected(() => setShowPipeline(true))}
      onOpenDocuments={() => openProtected(() => setShowDocuments(true))}
      onLogout={handleLogout}
      onLogin={openLogin}
      hasAccessToken={hasAccessToken}
      requestDraft={requestDraft}
      onRequestDraftChange={setRequestDraft}
    />
  )
}

export default App
