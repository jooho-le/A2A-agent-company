import { useEffect, useRef } from 'react'
import SavedProjectDetail from './SavedProjectDetail'
import { Icon } from './AtelierArtwork'
import { ActivityList, Portrait, RequestForm } from './AtelierCards'
import { agentStates, artifactContent, employees } from './atelierMock'

export default function AtelierDialog({ modal, project, projects, activities, onClose, onCreate, onApprove, onRevision, onFeedback, onDownload, returnFocusRef, rootRef, onLogin, presentation = {} }) {
  const dialogRef = useRef(null)
  useEffect(() => {
    const dialog = dialogRef.current
    const root = rootRef.current
    const trigger = returnFocusRef.current || document.activeElement
    dialog.showModal()
    return () => {
      dialog.close()
      queueMicrotask(() => {
        if (root?.querySelector('dialog[open]')) return
        const target = trigger?.isConnected && !trigger.closest('[hidden]') ? trigger : root?.querySelector('#page-title')
        target?.focus({ preventScroll: true })
      })
    }
  }, [rootRef, returnFocusRef])
  const employee = presentation.detailEmployee || employees[modal.index]
  const planner = presentation.staff?.[0] || employees[0]
  const detailState = presentation.detailState || (modal.type === 'employee' ? agentStates(project)[modal.index] : null)
  const titles = { 'saved-project': ['PROJECT DETAILS', '프로젝트 상세'], new: ['NEW ASSIGNMENT', '직원들에게 어떤 일을 맡길까요?'], plan: ['APPROVAL REQUEST', '대표님, 이 계획으로 진행할까요?'], revision: ['FEEDBACK TO YOUR TEAM', '어떤 부분을 바꾸면 좋을까요?'], employee: [`EMPLOYEE / ${employee?.role.toUpperCase()}`, `${employee?.name} · ${employee?.job}`], worklog: ['PROJECT WORK LOG', project.name], file: ['EXAMPLE DELIVERABLE', project.files[modal.index]?.split('/').pop()] }
  const [eyebrow, title] = titles[modal.type]
  function submitFeedback(event) {
    event.preventDefault()
    const value = event.currentTarget.elements.feedback.value.trim()
    if (!value) { event.currentTarget.elements.feedback.focus(); return }
    onFeedback(value)
  }
  function backdrop(event) {
    if (event.target !== event.currentTarget) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose()
  }
  return <dialog ref={dialogRef} id={`${modal.type}-dialog`} aria-labelledby="atelier-dialog-title" onCancel={(event) => { event.preventDefault(); onClose() }} onClose={() => { if (!dialogRef.current?.open) onClose() }} onClick={backdrop}>
    <div className="dialog-header"><div><p className="eyebrow">{eyebrow}</p><h2 id="atelier-dialog-title">{title}</h2></div><button type="button" className="close-button" onClick={onClose} aria-label="창 닫기"><Icon name="close" /></button></div>
    {modal.type === 'saved-project' && <SavedProjectDetail key={modal.projectId} projectId={modal.projectId} onLogin={onLogin} />}
    {modal.type === 'new' && <RequestForm modal mode={presentation.mode} planner={planner} onCreate={onCreate} onClose={onClose} />}
    {modal.type === 'plan' && <><div className="dialog-body"><div className="sender"><span className="mini-avatar">{planner.initial}</span><div><strong>{planner.name}<small>{planner.job} · {planner.role}</small></strong></div></div><p className="dialog-description">{project.name}{'\n'}{presentation.mode === 'single' ? '요청하신 내용을 이렇게 정리했어요. 승인하면 같은 기린이 개발을 이어갑니다.' : '요청하신 내용을 이렇게 정리했어요. 확인해주시면 개발 에이전트에게 넘길게요.'}</p><h3 className="detail-section-title">이렇게 만들 예정이에요</h3><ul className="scope-list">{project.scope.map((item, index) => <li key={index}><Icon name="check" /><span>{item}</span></li>)}</ul><p className="dialog-note">{presentation.mode === 'single' ? '승인 후 같은 기린이 개발 목업을 시작합니다.' : '승인 후 개발 에이전트가 구현을 시작합니다. 목업 상태만 바뀝니다.'}</p></div><div className="dialog-footer"><button type="button" className="button" onClick={onRevision}>수정 요청</button><button type="button" className="button start" onClick={onApprove}>승인하고 시작<Icon name="arrow" /></button></div></>}
    {modal.type === 'revision' && <form onSubmit={submitFeedback}><div className="dialog-body"><div className="blue-field"><label className="field-label" htmlFor="revision-text">{planner.name}에게 전달할 피드백</label><textarea id="revision-text" name="feedback" placeholder="추가하거나 바꾸고 싶은 내용을 적어주세요." maxLength={1600} rows={5} required /></div><p className="dialog-note">요청한 프로젝트가 다시 기획 중 상태로 바뀝니다.</p></div><div className="dialog-footer"><button type="button" className="button" onClick={onClose}>취소</button><button type="submit" className="button start">수정 요청 전달<Icon name="arrow" /></button></div></form>}
    {modal.type === 'employee' && <div className="dialog-body"><div className="employee-detail">{presentation.detailPortrait || <Portrait employee={employee} index={modal.index} />}<div><span className="eyebrow">{employee.id === 'orchestrator' ? 'Orchestrator 리더' : `${employee.role} AI`}{employee.leadership ? ` · ${employee.leadership.title}` : ''}</span><p className="quote">{employee.quote}</p><p className="personality">{employee.personality}</p></div></div><p className="dialog-description">{employee.purpose}</p><h3 className="detail-section-title">현재 맡은 업무</h3><p className="dialog-description">{project.name} · {detailState.label}{'\n'}{detailState.task}</p>{presentation.controls}<h3 className="detail-section-title">이런 일을 해요</h3><ul className="scope-list">{employee.responsibilities.map((item) => <li key={item}><Icon name="check" /><span>{item}</span></li>)}</ul><p className="dialog-note">이름·성격·작업 상태는 디자인 목업용 설정입니다.</p></div>}
    {modal.type === 'worklog' && <div className="dialog-body"><p className="dialog-description">{project.description}</p><h3 className="detail-section-title">프로젝트 파일</h3>{project.files.map((file) => <span className="file-chip" key={file}>{file}</span>)}{!project.files.length && <p className="dialog-description">아직 등록된 파일이 없어요.</p>}<h3 className="detail-section-title">직원 업무 기록</h3>{activities.some((item) => item.projectId === project.id) ? <ActivityList projects={projects} activities={activities.filter((item) => item.projectId === project.id)} staff={presentation.staff} /> : <p className="dialog-description">아직 작업 기록이 없어요.</p>}<p className="dialog-note">디자인 시연용 예시 기록입니다.</p></div>}
    {modal.type === 'file' && <div className="dialog-body"><p className="dialog-description">{project.name}{'\n'}{project.files[modal.index]}</p><pre className="code-preview"><code>{artifactContent(project, modal.index)}</code></pre><p className="dialog-note">디자인 목업용 예시입니다. 실제 생성 코드나 검증 결과가 아닙니다.</p><div className="output-actions"><button type="button" className="button" onClick={() => onDownload(modal.index)}><Icon name="down" />예시 파일 다운로드</button></div></div>}
  </dialog>
}
