import assert from 'node:assert/strict'
import test from 'node:test'
import { transitionOfficePose } from '../src/components/atelier/office3dPoseTransition.js'
const pose = v => ({ torso: [v, 0, 0], head: [v, 0, 0], arm: [[v, 0, 0], [v, 0, 0]], elbow: [[v, 0, 0], [v, 0, 0]], phones: .0001 })
test('작업 자세 전환은 첫 프레임에 뛰지 않고 부드럽게 수렴한다', () => {
  const start = pose(0), target = pose(1)
  let current = transitionOfficePose(start, target, 1 / 60)
  assert.ok(current.arm[0][0] > 0 && current.arm[0][0] < .1)
  for (let i = 0; i < 59; i++) current = transitionOfficePose(current, target, 1 / 60)
  assert.ok(current.arm[0][0] > .99 && current.arm[0][0] <= 1)
  assert.equal(start.arm[0][0], 0)
  assert.equal(target.arm[0][0], 1)
  assert.equal(transitionOfficePose(null, target, 0), target)
  assert.equal(transitionOfficePose(start, target, .1, false), target)
})
