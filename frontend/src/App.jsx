import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import AtelierDashboard from './pages/AtelierDashboard'
import DocumentPage from './pages/DocumentPage'
import { hasStoredAccessToken } from './api/productRunRequest'
import { clearProjectSelection } from './hooks/projectSelection'
import './App.css'

function App() {
  // 토큰 존재는 인증 성공의 보장이 아닙니다. 최초 진입은 공개 오피스입니다.
  const [hasAccessToken, setHasAccessToken] = useState(hasStoredAccessToken)
  const [loginSession, setLoginSession] = useState(0)
  const [showLogin, setShowLogin] = useState(false)
  const [requestDraft, setRequestDraft] = useState({ name: '', requestText: '' })
  const [showSignup, setShowSignup] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)
  const [showDocuments, setShowDocuments] = useState(false)
  const [loginNotice, setLoginNotice] = useState('')

  function handleLoginSuccess() {
    // 같은 계정의 재로그인을 포함해 새 로그인 세션은 이전 선택을 이어받지 않습니다.
    clearProjectSelection()
    sessionStorage.removeItem('ai-company.pending-office-submission')
    setHasAccessToken(hasStoredAccessToken())
    setLoginSession(value => value + 1)
    setShowLogin(false)
    setShowSignup(false)
    setShowPipeline(false)
    setShowDocuments(false)
    setLoginNotice('')
    window.history.replaceState(null, '', '#/home')
  }

  function openLogin() {
    setLoginNotice('')
    setShowSignup(false)
    setShowLogin(true)
  }

  function openProtected(action) {
    if (!hasStoredAccessToken()) { openLogin(); return }
    action()
  }

  function handleLogout() {
    clearProjectSelection()
    sessionStorage.removeItem('ai-company.pending-office-submission')
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

  // 로그인 취소·전자문서 복귀 시 선택한 Run과 입력 상태를 유지합니다.
  // 새 로그인 성공 시에는 key를 바꿔 이전 계정의 상태를 모두 분리합니다.
  let accountScreen = null
  if (showLogin) {
    if (showSignup) {
      accountScreen = (
        <SignupPage
          onBack={() => setShowSignup(false)}
          onClose={() => { setShowLogin(false); setShowSignup(false) }}
          onSignupSuccess={(message) => {
            setLoginNotice(`${message} 로그인하여 시작해주세요.`)
            setShowSignup(false)
          }}
        />
      )
    } else accountScreen = (
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
  } else if (showPipeline) {
    accountScreen = <PipelinePage onBack={() => setShowPipeline(false)} />
  } else if (showDocuments) {
    accountScreen = <DocumentPage onBack={() => setShowDocuments(false)} />
  }

  return (
    <>
      <div hidden={Boolean(accountScreen)}>
        <AtelierDashboard
          key={`${hasAccessToken ? 'credential-present' : 'guest'}:${loginSession}`}
          active={!accountScreen}
          onRunScenario={() => openProtected(() => setShowPipeline(true))}
          onOpenDocuments={() => openProtected(() => setShowDocuments(true))}
          onLogout={handleLogout}
          onLogin={openLogin}
          hasAccessToken={hasAccessToken}
          requestDraft={requestDraft}
          onRequestDraftChange={setRequestDraft}
        />
      </div>
      {accountScreen}
    </>
  )
}

export default App
