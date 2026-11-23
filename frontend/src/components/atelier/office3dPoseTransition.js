// 실제 상태는 즉시 갱신하고, 관절 자세만 약 0.7초에 걸쳐 부드럽게 연결합니다.
export function transitionOfficePose(previous, target, delta, enabled = true) {
  if (!previous || !enabled) return target
  const amount = 1 - Math.exp(-Math.max(0, delta) * 5)
  const blend = (a, b) => Array.isArray(b)
    ? b.map((value, index) => blend(a[index], value)) : a + (b - a) * amount
  return { ...target,
    torso: blend(previous.torso, target.torso), head: blend(previous.head, target.head),
    arm: blend(previous.arm, target.arm), elbow: blend(previous.elbow, target.elbow),
    phones: blend(previous.phones, target.phones) }
}
