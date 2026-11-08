import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import AtelierDashboard from './pages/AtelierDashboard'
import DocumentPage from './pages/DocumentPage'
import './App.css'

// 플랫폼 로그인이 필요해지면 true로 바꿔 기존 인증 화면을 다시 활성화합니다.
const REQUIRE_PLATFORM_LOGIN = false

function App() {
  // 기존 sessionStorage 토큰으로 새로고침 후에도 화면 흐름을 복원합니다.
  const [isAuthenticated, setIsAuthenticated] = useState(() => Boolean(sessionStorage.getItem('access_token')))
  const [showSignup, setShowSignup] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)
  const [showDocuments, setShowDocuments] = useState(false)
  const [loginNotice, setLoginNotice] = useState('')

  function handleLoginSuccess() {
    setIsAuthenticated(Boolean(sessionStorage.getItem('access_token')))
    setShowSignup(false)
    setShowPipeline(false)
    setShowDocuments(false)
    setLoginNotice('')
  }

  function handleLogout() {
    sessionStorage.removeItem('access_token')
    setIsAuthenticated(false)
    setShowSignup(false)
    setShowPipeline(false)
    setShowDocuments(false)
    setLoginNotice('')
  }

  // 설정이 켜진 경우에만 기존 로그인·회원가입 화면을 먼저 표시합니다.
  if (REQUIRE_PLATFORM_LOGIN && !isAuthenticated) {
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
      onRunScenario={() => setShowPipeline(true)}
      onOpenDocuments={() => setShowDocuments(true)}
      onLogout={handleLogout}
    />
  )
}

export default App
