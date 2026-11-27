// 기존 두 관절 리그에 연결하는 둥근 팔·다리 표면입니다.
// 원통 중간에도 충분한 면을 두어 팔꿈치·무릎이 큰 삼각형으로 접히지 않게 합니다.
export function createOfficeLimbSurface(origin, length, joint, radius, overlap = 0) {
  const positions = [], normals = [], indices = [], skinIndices = [], skinWeights = []
  const rows = 64, columns = 24, height = length + overlap
  for (let row = 0; row <= rows; row++) {
    const depth = height * row / rows
    const edge = Math.min(depth, height - depth)
    const ring = radius * Math.sqrt(Math.max(0, 1 - Math.max(0, 1 - edge / radius) ** 2))
    const normalY = depth < radius ? 1 - depth / radius : height - depth < radius ? (height - depth) / radius - 1 : 0
    const blend = Math.max(0, Math.min(1, (depth - overlap - joint + .18) / .36))
    const weight = blend * blend * (3 - 2 * blend)
    for (let column = 0; column <= columns; column++) {
      const angle = column / columns * Math.PI * 2
      positions.push(origin[0] + ring * Math.cos(angle), origin[1] + overlap - depth, origin[2] + ring * Math.sin(angle))
      normals.push(ring / radius * Math.cos(angle), normalY, ring / radius * Math.sin(angle))
      skinIndices.push(0, 1, 0, 0)
      skinWeights.push(1 - weight, weight, 0, 0)
      if (row < rows && column < columns) {
        const a = row * (columns + 1) + column, b = a + columns + 1
        indices.push(a, a + 1, b + 1, a, b + 1, b)
      }
    }
  }
  return { positions, normals, indices, skinIndices, skinWeights }
}
