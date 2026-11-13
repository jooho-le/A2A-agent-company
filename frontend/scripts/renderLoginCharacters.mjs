// Run explicitly from frontend: node scripts/renderLoginCharacters.mjs
// Offline Chrome/WebGL export only. Does not start Vite or call any API.
import { mkdtemp, writeFile, readFile, rm, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'

const dir = await mkdtemp(join(tmpdir(), 'a2a-login-poses-'))
try {
  const source = fileURLToPath(new URL('./loginCharacterScene.js', import.meta.url))
  const entry = join(dir, 'entry.js')
  await writeFile(entry, `import { renderLoginCharacters } from ${JSON.stringify(source)};
    const result = document.createElement('pre'); result.id = 'portraits';
    try { result.textContent = JSON.stringify(renderLoginCharacters()) }
    catch(error) { result.textContent = JSON.stringify({error:String(error)}) }
    document.body.append(result);`)
  const build = await rolldown({ input: entry })
  await build.write({ file: join(dir, 'render.js'), format: 'iife' })
  await build.close()
  await writeFile(join(dir, 'render.html'), '<!doctype html><meta charset="utf-8"><body><script src="render.js"></script></body>')
  const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  const { stdout } = await promisify(execFile)(chrome, [
    '--headless', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--disable-extensions', '--host-resolver-rules=MAP * 0.0.0.0',
    '--enable-unsafe-swiftshader', `--user-data-dir=${join(dir, 'profile')}`, '--dump-dom', '--virtual-time-budget=3000',
    pathToFileURL(join(dir, 'render.html')).href,
  ], { timeout: 45000, maxBuffer: 20 * 1024 * 1024 })
  const raw = stdout.match(/<pre id="portraits">([^<]+)<\/pre>/)?.[1]
  if (!raw) throw new Error('Portrait renderer did not return images')
  const images = JSON.parse(raw.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&gt;', '>').replaceAll('&lt;', '<'))
  if (images.error) throw new Error(images.error)
  const out = fileURLToPath(new URL('../public/characters/', import.meta.url))
  await mkdir(out, { recursive: true })
  const roles = ['planner', 'developer', 'qa', 'security', 'orchestrator']
  for (const role of roles) {
    if (!images[role]?.startsWith('data:image/png;base64,')) throw new Error(`Missing ${role}`)
  }
  for (const role of roles) {
    const path = join(out, `login-${role}.png`)
    await writeFile(path, Buffer.from(images[role].split(',')[1], 'base64'))
    const png = await readFile(path)
    if (png.readUInt32BE(16) !== 768 || png.readUInt32BE(20) !== 960) throw new Error('Unexpected image dimensions')
    console.log(`Rendered login-${role}.png (768×960)`)
  }
} finally {
  await rm(dir, { recursive: true, force: true })
}
