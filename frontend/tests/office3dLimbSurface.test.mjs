import assert from 'node:assert/strict'
import test from 'node:test'
import { createOfficeLimbSurface } from '../src/components/atelier/office3dLimbSurface.js'

for (const [name, length, joint, radius, overlap] of [['팔', .85, .44, .106, .157], ['다리', .99, .46, .115, 0]]) {
  test(`${name}의 관절 주위 면과 두 뼈의 가중치가 연속적으로 이어진다`, () => {
    const mesh = createOfficeLimbSurface([-.2, 1.1, 0], length, joint, radius, overlap)
    const vertices = mesh.positions.length / 3
    assert.equal(vertices, 65 * 25)
    assert.ok(mesh.positions.every(Number.isFinite))
    assert.ok(mesh.indices.every(index => Number.isInteger(index) && index >= 0 && index < vertices))
    for (let index = 0; index < vertices; index++) {
      assert.ok(Math.abs(Math.hypot(...mesh.normals.slice(index * 3, index * 3 + 3)) - 1) < 1e-10)
    }
    const weights = []
    for (let row = 0; row <= 64; row++) {
      const weight = mesh.skinWeights[row * 25 * 4 + 1]
      weights.push(weight)
      for (let col = 0; col <= 24; col++) {
        const offset = (row * 25 + col) * 4
        assert.ok(Math.abs(mesh.skinWeights[offset] + mesh.skinWeights[offset + 1] - 1) < 1e-10)
      }
    }
    // 한 개의 큰 면에서 급격히 꺾이지 않고 여러 고리를 거쳐 부드럽게 변합니다.
    assert.ok(weights.filter(weight => weight > 0 && weight < 1).length >= 18)
    assert.ok(weights.slice(1).every((weight, index) => weight >= weights[index] && weight - weights[index] < .08))
    assert.equal(weights[0], 0)
    assert.equal(weights.at(-1), 1)
  })
}
