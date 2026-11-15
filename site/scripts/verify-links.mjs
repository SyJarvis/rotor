import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { base } from '../base.mjs'

const root = new URL('..', import.meta.url).pathname
const dist = join(root, 'dist')

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

// The same sources build for the domain root and for a /<repository>/ prefix,
// and the deployed build keeps .html suffixes, so routes are compared in one
// canonical form: no base, no .html, no trailing slash.
function canonical(path) {
  const trimmed = path.replace(/\.html$/, '').replace(/\/index$/, '')
  if (trimmed === '/index' || trimmed === '') return '/'
  return trimmed.replace(/\/$/, '') || '/'
}

function isAsset(path) {
  return path.startsWith('/assets/') || /\.(?!html$)[a-z0-9]+$/i.test(path)
}

const files = walk(dist)
const pages = new Set(
  files.filter((file) => file.endsWith('.html'))
    .map((file) => canonical('/' + relative(dist, file)))
)

const ids = new Map()
for (const file of files.filter((item) => item.endsWith('.html'))) {
  const route = canonical('/' + relative(dist, file))
  const html = readFileSync(file, 'utf8')
  ids.set(route, new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1])))
}

const broken = []
let checked = 0

for (const file of files.filter((item) => item.endsWith('.html'))) {
  const html = readFileSync(file, 'utf8')
  for (const match of html.matchAll(/href="(\/[^"]*)"/g)) {
    const href = match[1]
    if (href.startsWith('//')) continue
    if (base !== '/' && !href.startsWith(base)) {
      broken.push(`${relative(dist, file)} -> ${href} (outside base ${base})`)
      continue
    }
    const withinBase = base === '/' ? href : '/' + href.slice(base.length)
    if (isAsset(withinBase.split('#')[0])) continue
    const [rawPath, hash] = withinBase.split('#')
    const target = canonical(rawPath)
    if (!pages.has(target)) { broken.push(`${relative(dist, file)} -> ${href} (no page)`); continue }
    if (hash && ids.has(target) && !ids.get(target).has(decodeURIComponent(hash))) {
      broken.push(`${relative(dist, file)} -> ${href} (no anchor)`)
    }
    checked += 1
  }
}

console.log('base:', base)
console.log('internal links checked:', checked)
console.log('broken:', broken.length ? '\n' + broken.join('\n') : 'none')
if (broken.length) process.exit(1)
