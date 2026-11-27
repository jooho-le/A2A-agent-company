import { useRef, useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import AtelierDashboard from './pages/AtelierDashboard'
import DocumentPage from './pages/DocumentPage'
import { hasStoredAccessToken } from './api/productRunRequest'
import { clearProjectSelection } from './hooks/projectSelection'
import { pendingOfficeSubmissionKey } from './hooks/pendingOfficeSubmission'
import useAccountNavigation from './hooks/useAccountNavigation'
import './App.css'

function App() {
  // 토큰 존재는 인증 성공의 보장이 아닙니다. 최초 진입은 공개 오피스입니다.
  const [hasAccessToken, setHasAccessToken] = useState(hasStoredAccessToken)
  const [loginSession, setLoginSession] = useState(0)
  const [requestDraft, setRequestDraft] = useState({ name: '', requestText: '' })
  const [loginNotice, setLoginNotice] = useState('')
  const navigation = useAccountNavigation()
  const draftAccount = useRef({ authenticated: hasStoredAccessToken(), key: pendingOfficeSubmissionKey() })

  function handleLoginSuccess() {
    const nextAccount = pendingOfficeSubmissionKey()
    const previous = draftAccount.current
    if (previous.authenticated && (!previous.key || previous.key !== nextAccount)) {
      setRequestDraft({ name: '', requestText: '' })
    }
    draftAccount.current = { authenticated: true, key: nextAccount }
    // 같은 계정의 재로그인을 포함해 새 로그인 세션은 이전 선택을 이어받지 않습니다.
    clearProjectSelection()
    sessionStorage.removeItem('ai-company.pending-office-submission')
    setHasAccessToken(hasStoredAccessToken())
    setLoginSession(value => value + 1)
    setLoginNotice('')
    navigation.reset()
  }

  function openLogin() {
    setLoginNotice('')
    navigation.open('login')
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
    setRequestDraft({ name: '', requestText: '' })
    draftAccount.current = { authenticated: false, key: null }
    setLoginNotice('')
    navigation.reset()
  }

  // 로그인 취소·전자문서 복귀 시 선택한 Run과 입력 상태를 유지합니다.
  // 새 로그인 성공 시에는 key를 바꿔 이전 계정의 상태를 모두 분리합니다.
  let accountScreen = null
  if (navigation.screen === 'login' || navigation.screen === 'signup') {
    if (navigation.screen === 'signup') {
      accountScreen = (
        <SignupPage
          onBack={navigation.back}
          onClose={navigation.close}
          onSignupSuccess={(message) => {
            setLoginNotice(`${message} 로그인하여 시작해주세요.`)
            navigation.back()
          }}
        />
      )
    } else accountScreen = (
      <LoginPage
        notice={loginNotice}
        onBack={navigation.close}
        onSignup={() => {
          setLoginNotice('')
          navigation.open('signup')
        }}
        onLoginSuccess={handleLoginSuccess}
      />
    )
  } else if (navigation.screen === 'pipeline') {
    accountScreen = <PipelinePage onBack={navigation.close} />
  } else if (navigation.screen === 'documents') {
    accountScreen = <DocumentPage onBack={navigation.close} />
  }

  return (
    <>
      <div hidden={Boolean(accountScreen)}>
        <AtelierDashboard
          key={`${hasAccessToken ? 'credential-present' : 'guest'}:${loginSession}`}
          active={!accountScreen}
          onRunScenario={() => openProtected(() => navigation.open('pipeline'))}
          onOpenDocuments={() => openProtected(() => navigation.open('documents'))}
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
