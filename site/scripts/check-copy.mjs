import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { contentRoot, origin, pageUrl } from './site-url.mjs'

const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const PORT = 9337
const pages = [
  ['/reference/control-api', join(contentRoot, 'reference/control-api.md')],
  ['/guides/chat', join(contentRoot, 'guides/chat.md')]
]

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, '--disable-gpu',
  '--no-first-run', '--window-size=1440,1000',
  '--user-data-dir=/tmp/rotor-chrome-profile5', 'about:blank'
], { stdio: 'ignore' })

async function endpoint() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (response.ok) return (await response.json()).webSocketDebuggerUrl
    } catch {}
    await sleep(200)
  }
  throw new Error('Chrome did not start')
}

const socket = new WebSocket(await endpoint())
await new Promise((resolve) => socket.addEventListener('open', resolve))
let id = 0
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
})
const send = (method, params = {}, sessionId) => {
  id += 1
  const messageId = id
  return new Promise((resolve) => { pending.set(messageId, resolve); socket.send(JSON.stringify({ id: messageId, method, params, sessionId })) })
}

const { result: target } = await send('Target.createTarget', { url: 'about:blank' })
const { result: attached } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true })
const session = attached.sessionId
await send('Page.enable', {}, session)
await send('Runtime.enable', {}, session)
await send('Browser.grantPermissions', {
  origin,
  permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite']
}, session)

for (const [route, sourcePath] of pages) {
  const url = pageUrl(route)
  await send('Page.navigate', { url }, session)
  await sleep(2200)
  const evaluate = async (expression) => {
    const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, session)
    if (response.result?.exceptionDetails) return { exception: response.result.exceptionDetails.text }
    return response.result?.result?.value
  }

  const result = await evaluate(`(async () => {
    const button = [...document.querySelectorAll('.copy-page')].find((node) => node.closest('.VPDocAside'))
    button.click()
    await new Promise((resolve) => setTimeout(resolve, 600))
    let text = null
    let error = null
    try { text = await navigator.clipboard.readText() } catch (cause) { error = cause.name }
    return { label: button.textContent.trim(), length: text?.length ?? null, error, text }
  })()`)

  const source = readFileSync(sourcePath, 'utf8')
  console.log(JSON.stringify({
    url,
    label: result.label,
    clipboardMatchesSource: result.text === source,
    clipboardLength: result.length,
    sourceLength: source.length,
    error: result.error
  }))
}

await send('Target.closeTarget', { targetId: target.targetId })
socket.close()
chrome.kill()
