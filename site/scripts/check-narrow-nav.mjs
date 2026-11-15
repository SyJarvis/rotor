import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { contentRoot, origin, pageUrl } from './site-url.mjs'

const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const PORT = 9339

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, '--disable-gpu',
  '--no-first-run', '--window-size=420,900',
  '--user-data-dir=/tmp/rotor-chrome-profile7', 'about:blank'
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
await send('Emulation.setDeviceMetricsOverride', { width: 420, height: 900, deviceScaleFactor: 1, mobile: true }, session)
await send('Browser.grantPermissions', { origin, permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'] }, session)

const evaluate = async (expression) => {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, session)
  if (response.result?.exceptionDetails) return { exception: response.result.exceptionDetails.text }
  return response.result?.result?.value
}

async function open(path) {
  await send('Page.navigate', { url: pageUrl(path) }, session)
  await sleep(2200)
}

async function copyViaMenu() {
  return evaluate(`(async () => {
    const details = document.querySelector('.page-outline-menu')
    details.open = true
    await new Promise((resolve) => setTimeout(resolve, 200))
    const panel = document.querySelector('.page-outline-panel')
    const copy = panel.querySelector('.copy-page')
    const links = [...panel.querySelectorAll('.outline-link')]
    const last = links.at(-1)
    const afterLinks = last ? copy.getBoundingClientRect().top >= last.getBoundingClientRect().bottom : true
    copy.click()
    await new Promise((resolve) => setTimeout(resolve, 600))
    let text = null
    try { text = await navigator.clipboard.readText() } catch (error) { text = 'error:' + error.name }
    return { label: copy.textContent.trim(), links: links.length, afterLinks, text }
  })()`)
}

await open('/guides/chat')
const first = await copyViaMenu()
const chatSource = readFileSync(join(contentRoot, 'guides/chat.md'), 'utf8')
console.log(JSON.stringify({
  page: 'guides/chat',
  label: first.label,
  links: first.links,
  copyAfterLinks: first.afterLinks,
  matchesSource: first.text === chatSource
}))

// Navigate through an in-page link, then copy again: the clipboard must follow
// the new route rather than reusing the previous page.
const navigated = await evaluate(`(async () => {
  const link = [...document.querySelectorAll('a[href$="/guides/accounts"], a[href$="/guides/accounts.html"]')][0]
  if (!link) return { navigated: false, links: [...document.querySelectorAll('a')].length }
  const expected = new URL(link.href).pathname
  link.click()
  await new Promise((resolve) => setTimeout(resolve, 1800))
  return { navigated: location.pathname === expected, path: location.pathname, title: document.title }
})()`)
const second = await copyViaMenu()
const accountsSource = readFileSync(join(contentRoot, 'guides/accounts.md'), 'utf8')
console.log(JSON.stringify({
  navigation: navigated,
  label: second.label,
  links: second.links,
  copyAfterLinks: second.afterLinks,
  matchesNewPage: second.text === accountsSource,
  stillOldPage: second.text === chatSource
}))

await send('Target.closeTarget', { targetId: target.targetId })
socket.close()
chrome.kill()
