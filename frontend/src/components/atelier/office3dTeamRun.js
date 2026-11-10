const clamp = value => Math.max(0, Math.min(1, value))
const smooth = value => { const t = clamp(value); return t * t * (3 - 2 * t) }
const mix = (a, b, t) => a + (b - a) * t
const point = (a, b, t) => ({ x: mix(a.x, b.x, t), z: mix(a.z, b.z, t) })
const distance = (a, b) => Math.hypot(a.x - b.x, a.z - b.z)

// 모서리 앞뒤만 둥글게 연결합니다. 호 길이로 샘플링하므로 통로 중간에 멈추지 않습니다.
export function roundedTeamPath(points, radius = .28) {
  const samples = [points[0]]
  for (let i = 1; i < points.length - 1; i++) {
    const previous = points[i - 1], corner = points[i], next = points[i + 1]
    const incoming = distance(previous, corner), outgoing = distance(corner, next)
    const cut = Math.min(radius, incoming * .25, outgoing * .25)
    const start = point(corner, previous, cut / Math.max(incoming, .00001))
    const end = point(corner, next, cut / Math.max(outgoing, .00001))
    samples.push(start)
    for (let j = 1; j <= 24; j++) {
      const t = j / 24
      samples.push(point(point(start, corner, t), point(corner, end, t), t))
    }
  }
  samples.push(points.at(-1))
  const lengths = [0]
  for (let i = 1; i < samples.length; i++) lengths.push(lengths.at(-1) + distance(samples[i - 1], samples[i]))
  const length = lengths.at(-1)
  function at(travelled) {
    const d = Math.max(0, Math.min(length, travelled))
    let low = 1, high = lengths.length - 1
    while (low < high) {
      const middle = (low + high) >> 1
      if (lengths[middle] < d) low = middle + 1
      else high = middle
    }
    return point(samples[low - 1], samples[low], (d - lengths[low - 1]) / Math.max(.00001, lengths[low] - lengths[low - 1]))
  }
  return { length, at }
}

// 위상은 벽시계가 아닌 실제 이동 거리에서 얻습니다. 정지 시에는 발걸음도 0입니다.
export function teamRunPose({ distance: travelled, speed, standing, index }) {
  const activity = smooth((speed - .025) / .9) * standing
  const stride = 1.45 + index * .025
  const phase = travelled / stride * Math.PI * 2 + index * .73
  const bounce = .025 * (1 - Math.cos(phase * 2)) * activity
  const lowerY = -.11 * activity
  const legs = [0, 1].map(side => {
    const cycle = ((phase / (Math.PI * 2) + side * .5) % 1 + 1) % 1
    const stance = .42, halfStep = stride * stance / 2
    const swing = clamp((cycle - stance) / (1 - stance))
    const z = cycle < stance ? mix(halfStep, -halfStep, cycle / stance) : mix(-halfStep, halfStep, smooth(swing))
    const lift = cycle < stance ? 0 : .22 * Math.sin(Math.PI * swing) ** 2
    // 기존 다리·신발 길이로 관절 각도를 구해 디딘 발의 높이를 유지합니다.
    const upper = .46, lower = Math.hypot(.5, .085)
    const y = .10 + lift - (1.09 + lowerY + bounce)
    const reach = Math.min(upper + lower - .002, Math.hypot(y, z))
    const knee = Math.acos(Math.max(-1, Math.min(1, (reach ** 2 - upper ** 2 - lower ** 2) / (2 * upper * lower))))
    const hip = Math.atan2(-z, -y) - Math.atan2(lower * Math.sin(knee), upper + lower * Math.cos(knee))
    return { thigh: mix(mix(-1.25, -.035, standing), hip, activity),
      shin: mix(mix(1.34, .08, standing), knee + Math.atan2(.085, .5), activity) }
  })
  return { activity, phase, bounce, lowerY, legs,
    lean: .025 + (.06 + .08 * clamp(speed / 3.5)) * activity, twist: Math.sin(phase) * .045 * activity,
    roll: Math.sin(phase) * .02 * activity }
}

// 도착한 뒤 기존 협업 자세로 연결해 이동 중 무릎을 꿇거나 제자리걸음하지 않게 합니다.
export function applyTeamRun(agent, travel) {
  const blend = smooth((travel.elapsed - travel.end) / .55)
  if (blend >= 1) return
  const pose = teamRunPose({ ...travel, index: agent.i })
  const weight = 1 - blend
  agent.g.position.y = mix(agent.g.position.y, pose.bounce, weight)
  agent.lower.position.y = pose.lowerY * weight
  agent.upper.position.y = mix(agent.upper.position.y, 1.36 + pose.lowerY, weight)
  agent.upper.position.z = mix(agent.upper.position.z, mix(-.51, -.03, travel.standing), weight)
  agent.lower.position.z = mix(agent.lower.position.z, mix(-.25, 0, travel.standing), weight)
  agent.upper.rotation.set(mix(agent.upper.rotation.x, pose.lean, weight * travel.standing),
    mix(agent.upper.rotation.y, pose.twist, weight), mix(agent.upper.rotation.z, pose.roll, weight))
  agent.head.rotation.x = mix(agent.head.rotation.x, -.045 - pose.lean * .25, weight * travel.standing)
  agent.head.rotation.z = mix(agent.head.rotation.z, -pose.roll * .5, weight)
  agent.legs.forEach((leg, side) => {
    leg.thigh.rotation.x = mix(leg.thigh.rotation.x, pose.legs[side].thigh, weight)
    leg.shin.rotation.x = mix(leg.shin.rotation.x, pose.legs[side].shin, weight)
    const sign = side === 0 ? -1 : 1
    // 앞으로 나가는 다리의 반대쪽 팔을 흔들고 굽힌 팔꿈치에는 약간의 시차를 줍니다.
    const swing = Math.cos(pose.phase + side * Math.PI)
    const arm = agent.arms[side]
    arm.shoulder.rotation.x = mix(arm.shoulder.rotation.x, -.12 + .44 * swing * pose.activity, weight * travel.standing)
    arm.shoulder.rotation.z = mix(arm.shoulder.rotation.z, sign * .12, weight * travel.standing)
    arm.elbow.rotation.x = mix(arm.elbow.rotation.x, -.28 - .88 * pose.activity + .12 * Math.cos(pose.phase + side * Math.PI - .35) * pose.activity, weight * travel.standing)
  })
}
