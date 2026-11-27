import assert from 'node:assert/strict'
import test from 'node:test'
import { teamRunPose, applyTeamRun } from '../src/components/atelier/office3dTeamRun.js'
import { createOfficeRoutes } from '../src/components/atelier/office3dLayout.js'

const routes = createOfficeRoutes({ En: 0, er: .42 })
function rig(index = 0) {
  const xyz = (x = 0, y = 0, z = 0) => ({ x, y, z, set(x, y, z) { Object.assign(this, { x, y, z }) } })
  const part = () => ({ position: xyz(), rotation: xyz() })
  return { i: index, g: part(), lower: part(), upper: part(), head: part(),
    arms: [0, 1].map(() => ({ shoulder: part(), elbow: part() })),
    legs: [0, 1].map(() => ({ thigh: part(), shin: part() })) }
}

test('다섯 명은 통로 중간에 멈추지 않고 가속·감속하며 같은 최종 위치에 도착한다', () => {
  for (let index = 0; index < 5; index++) {
    let previous = routes.teamRoute(index, 1)
    for (let time = 1.01; time < 7.3; time += .01) {
      const pose = routes.teamRoute(index, time)
      assert.ok(pose.distance > previous.distance)
      assert.ok(pose.speed > 0 && pose.speed < 4)
      assert.ok(Math.hypot(pose.x - previous.x, pose.z - previous.z) < .05)
      previous = pose
    }
    assert.equal(routes.teamRoute(index, 1).speed, 0)
    assert.equal(routes.teamRoute(index, 7.3).speed, 0)
    const atEnd = routes.teamRoute(index, 7.3), later = routes.teamRoute(index, 12)
    assert.deepEqual([later.x, later.z, later.distance], [atEnd.x, atEnd.z, atEnd.distance])
  }
})

test('발걸음은 실제 이동 거리로 결정되며 정지하면 위상이 달라도 제자리걸음하지 않는다', () => {
  for (let index = 0; index < 5; index++) {
    const resting = teamRunPose({ distance: 0, speed: 0, standing: 1, index })
    for (const distance of [1, 5, 10, 15]) {
      const pose = teamRunPose({ distance, speed: 0, standing: 1, index })
      assert.deepEqual(pose.legs, resting.legs)
      assert.equal(pose.bounce, 0)
      assert.equal(pose.activity, 0)
    }
    const moving = teamRunPose({ distance: 3, speed: 2, standing: 1, index })
    assert.deepEqual(moving, teamRunPose({ distance: 3, speed: 2, standing: 1, index }))
  }
})

test('디딘 발은 바닥 높이를 유지하고 반대 발은 무릎을 굽혀 들어 올린다', () => {
  let lifted = false
  for (let distance = 0; distance < 1.45; distance += .005) {
    const pose = teamRunPose({ distance, speed: 2, standing: 1, index: 0 })
    const cycle = distance / 1.45
    const { thigh, shin } = pose.legs[0]
    const footY = 1.09 + pose.lowerY + pose.bounce - .46 * Math.cos(thigh)
      - .5 * Math.cos(thigh + shin) - .085 * Math.sin(thigh + shin)
    assert.ok(Number.isFinite(footY) && footY >= .095, `바닥 관통: ${footY}`)
    if (cycle < .42) assert.ok(footY <= .13, `접지 발이 떠 있음: ${footY}`)
    if (footY > .25) lifted = true
  }
  assert.equal(lifted, true)
})

test('앞으로 내딛는 다리와 반대 팔이 앞으로 나오며 팔꿈치가 굽혀진다', () => {
  const agent = rig()
  applyTeamRun(agent, { elapsed: 3, end: 7.3, distance: 0, speed: 2, standing: 1 })
  assert.ok(agent.legs[0].thigh.rotation.x < 0)
  assert.ok(agent.arms[0].shoulder.rotation.x > 0)
  assert.ok(agent.arms[1].shoulder.rotation.x < 0)
  assert.ok(agent.arms.every(arm => arm.elbow.rotation.x < -.8))
  assert.ok(agent.upper.rotation.x > .08)
})

test('출발·도착 시점의 자세가 연속이고 도착 후에는 기존 협업 포즈를 그대로 사용한다', () => {
  for (let index = 0; index < 5; index++) {
    const at = time => {
      const agent = rig(index)
      agent.g.position.y = index === 1 ? -.48 : 0
      agent.legs[0].thigh.rotation.x = index === 1 ? -1.53 : -.035
      applyTeamRun(agent, routes.teamRoute(index, time))
      return agent
    }
    for (const time of [1, 7.3, 7.85]) {
      const before = at(time - .0001), after = at(time + .0001)
      assert.ok(Math.abs(before.g.position.y - after.g.position.y) < .001)
      assert.ok(Math.abs(before.legs[0].thigh.rotation.x - after.legs[0].thigh.rotation.x) < .001)
    }
    const final = rig(index), original = JSON.stringify(final)
    applyTeamRun(final, routes.teamRoute(index, 9))
    assert.equal(JSON.stringify(final), original)
  }
})
