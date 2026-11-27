import { useState } from 'react'
import { humanAgents } from './humanAgents'
import { roleLabel } from './roleLabels'
import { Icon } from './AtelierArtwork'

const stories = [
  { index: 4, title: '한 팀의 흐름을 조율해요.', note: '필요한 직원에게 일을 전달해요.' },
  { index: 0, title: '생각을 계획으로.', note: '필요한 기능과 순서를 정리해요.' },
  { index: 1, title: '계획을 진짜 기능으로.', note: '코드를 만들고 고쳐요.' },
  { index: 2, title: '제대로 작동하는지.', note: '요구사항과 예외 상황을 검사해요.' },
  { index: 3, title: '안심하고 쓸 수 있게.', note: '접근 권한과 취약점을 살펴봐요.' },
]

export default function TeamStoryDeck() {
  const [active, setActive] = useState(0)
  const story = stories[active]
  const person = humanAgents[story.index]
  function change(direction) { setActive(value => Math.max(0, Math.min(stories.length - 1, value + direction))) }
  return <section className="team-story-deck" aria-label="직원 역할 소개 슬라이드" tabIndex={0} onKeyDown={event => {
    if (event.target !== event.currentTarget) return
    if (event.key === 'ArrowRight') { event.preventDefault(); change(1) }
    if (event.key === 'ArrowLeft') { event.preventDefault(); change(-1) }
  }}>
    <div className="team-story-slide" data-story-role={person.id} key={person.id}>
      <div className="team-story-copy" aria-live="polite"><span className="team-story-role">역할 소개 · {roleLabel(person.id)}</span><h3>{story.title}</h3><p>{story.note}</p></div>
      <div className="team-story-portrait" aria-hidden="true"><span /><img src={person.portrait} alt="" /></div>
      <span className="team-story-count" aria-hidden="true">{String(active + 1).padStart(2, '0')}<small> / 05</small></span>
    </div>
    <div className="team-story-navigation">
      <div className="team-story-dots" role="group" aria-label="직원 선택">{stories.map(({ index }, page) => <button type="button" key={index} aria-label={`${roleLabel(humanAgents[index].id)} 소개`} aria-current={page === active ? 'true' : undefined} onClick={() => setActive(page)} />)}</div>
      <div><button type="button" onClick={() => change(-1)} disabled={active === 0} aria-label="이전 역할"><Icon name="chevron" /></button><button type="button" onClick={() => change(1)} disabled={active === stories.length - 1} aria-label="다음 역할"><Icon name="chevron" /></button></div>
    </div>
  </section>
}
