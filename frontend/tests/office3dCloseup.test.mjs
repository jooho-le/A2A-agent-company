import assert from 'node:assert/strict'
import test from 'node:test'
import { createAgentCloseupCamera } from '../src/components/atelier/office3dPlannerCamera.js'
import { orchestratorHairPoint } from '../src/components/atelier/office3dAppearance.js'

function fixture() {
  const object = () => ({ visible: true })
  const desks = Array.from({ length: 5 }, object)
  const props = Array.from({ length: 5 }, object)
  const uv = { count: 1, getY: () => .5, setY() {} }
  const geometry = { clone: () => ({ getAttribute: () => uv }) }
  const office = { agents: Array.from({ length: 5 }, () => ({ g: object() })), props, cup: object(),
    workspace: { children: [{ ...object(), name: 'shared_planter' }], getObjectByName: name => desks[Number(name.split('_')[1])] },
    backdrop: { ...object(), geometry, clone: () => ({ ...object(), geometry }) }, root: { add() {} } }
  return { office, desks, props, camera: createAgentCloseupCamera(office, { P: class {} }) }
}

for (const index of [0, 2, 3, 4]) {
  test(`${index} 근접 보기에서 다른 캐릭터·책상·노트북을 숨기고 정확히 복원한다`, () => {
    const { office, desks, props, camera } = fixture()
    props[1].visible = false
    camera.focus(index)
    assert.equal(office.workspace.children[0].visible, false)
    for (let i = 0; i < 5; i++) {
      assert.equal(office.agents[i].g.visible, i === index)
      assert.equal(desks[i].visible, i === index)
      assert.equal(props[i].visible, i === index && i !== 1)
    }
    camera.hide()
    assert.ok(office.agents.every(agent => agent.g.visible) && desks.every(desk => desk.visible))
    assert.equal(office.workspace.children[0].visible, true)
    assert.equal(props[1].visible, false)
    camera.hide()
    assert.equal(props[1].visible, false)
  })
}
test('QA에서 보안으로 전환해도 앞자리 좌석의 가시성이 남지 않는다', () => {
  const { office, desks, camera } = fixture()
  camera.focus(2); camera.focus(3)
  assert.deepEqual(office.agents.map(agent => agent.g.visible), [false, false, false, true, false])
  assert.deepEqual(desks.map(desk => desk.visible), [false, false, false, true, false])
  camera.hide()
  assert.equal(office.cup.visible, true)
})
test('깻잎 앞머리 곡면은 유한하며 눈·볼 영역으로 내려오지 않는다', () => {
  for (let u = 0; u <= 1; u += .02) for (let v = 0; v <= 1; v += .05) {
    const point = orchestratorHairPoint(u, v)
    assert.ok(Object.values(point).every(Number.isFinite))
    if (Math.cos(u * Math.PI * 2) > .4) assert.ok(point.y > .16)
  }
})

test('오케스트레이터 앞·옆·뒷머리는 같은 정수리와 닫힌 이음선을 공유한다', () => {
  for (let v = 0; v <= 1; v += .025) {
    const start = orchestratorHairPoint(0, v), end = orchestratorHairPoint(1, v)
    assert.ok(Math.hypot(start.x - end.x, start.y - end.y, start.z - end.z) < 1e-10)
    for (let u = 0; u <= 1; u += .01) {
      const point = orchestratorHairPoint(u, v)
      assert.ok(Object.values(point).every(Number.isFinite))
      if (v === 0) assert.ok(Math.hypot(point.x, point.z + .035) < 1e-7)
    }
  }
})

test('연결된 머리는 앞쪽 눈을 가리지 않고 뒤쪽은 어깨까지 이어진다', () => {
  assert.ok(orchestratorHairPoint(.5, 1).y < -1)
  for (let u = 0; u <= 1; u += .01) {
    const point = orchestratorHairPoint(u, 1)
    if (Math.cos(u * Math.PI * 2) > .4) assert.ok(point.y >= .18)
  }
})
