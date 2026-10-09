import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import DashboardPage from './pages/DashboardPage'
import './App.css'

function App() {
  // 기존 sessionStorage 토큰으로 새로고침 후에도 화면 흐름을 복원합니다.
  const [isAuthenticated, setIsAuthenticated] = useState(() => Boolean(sessionStorage.getItem('access_token')))
  const [showSignup, setShowSignup] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)
  const [loginNotice, setLoginNotice] = useState('')

  function handleLoginSuccess() {
    setIsAuthenticated(Boolean(sessionStorage.getItem('access_token')))
    setShowSignup(false)
    setShowPipeline(false)
    setLoginNotice('')
  }

  function handleLogout() {
    sessionStorage.removeItem('access_token')
    setIsAuthenticated(false)
    setShowSignup(false)
    setShowPipeline(false)
    setLoginNotice('')
  }

  // 인증 전에는 Dashboard와 Pipeline 화면을 표시하지 않습니다.
  if (!isAuthenticated) {
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

  return (
    <DashboardPage onRunScenario={() => setShowPipeline(true)} onLogout={handleLogout} />
  )
}

export default App
