import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import StartupExperience from './components/StartupExperience.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <StartupExperience><App /></StartupExperience>
  </StrictMode>,
)
