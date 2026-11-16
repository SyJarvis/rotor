import { readFileSync, readdirSync, statSync } from 'node:fs'
import { basename, join, relative } from 'node:path'

const root = new URL('..', import.meta.url).pathname
const dist = join(root, 'dist')
const content = join(root, '..', 'gitbook')

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

// VitePress emits `JSON.parse('<js string literal>')` for page data; unwrap the
// literal, then read the raw Markdown the copy button would place on the
// clipboard.
function builtMarkdown(chunk) {
  const code = readFileSync(chunk, 'utf8')
  const open = code.indexOf('JSON.parse(') + 'JSON.parse('.length
  const quote = code[open]
  let index = open + 1
  let literal = ''
  for (; index < code.length; index += 1) {
    const character = code[index]
    if (character === '\\') { literal += character + code[index + 1]; index += 1; continue }
    if (character === quote) break
    literal += character
  }
  return JSON.parse(Function(`return ${quote}${literal}${quote}`)()).rawMarkdown
}

const sources = walk(content).filter((path) => path.endsWith('.md'))
const chunks = walk(join(dist, 'assets'))
  .filter((path) => path.endsWith('.js') && !path.endsWith('.lean.js') && path.includes('.md.'))

const failures = []
const compared = new Set()

for (const chunk of chunks) {
  // Chunk names replace path separators with underscores, keeping hyphens.
  const slug = basename(chunk).replace(/\.md\..*$/, '')
  const tail = slug.replace(/_/g, '/')
  const source = sources.find((path) => {
    const rel = relative(content, path).replace(/\.md$/, '')
    return rel === tail || `${rel}/README` === tail
  }) || (tail === 'index' ? join(content, 'README.md') : undefined)

  if (!source) {
    failures.push(`no source for ${slug}`)
    continue
  }

  compared.add(source)
  const sourceText = readFileSync(source, 'utf8')
  const built = builtMarkdown(chunk)
  if (built !== sourceText) {
    let at = 0
    while (at < built.length && at < sourceText.length && built[at] === sourceText[at]) at += 1
    failures.push(`${slug}: differs at ${at} | built ${JSON.stringify(built.slice(at, at + 40))} | source ${JSON.stringify(sourceText.slice(at, at + 40))}`)
  }
}

const expected = sources.filter((path) => !path.endsWith('SUMMARY.md'))
const missing = expected.filter((path) => !compared.has(path))

console.log('pages compared:', compared.size, '/', expected.length)
console.log('missing:', missing.length ? missing.map((p) => relative(content, p)).join(', ') : 'none')
if (failures.length) console.log(failures.join('\n'))
if (failures.length || missing.length) process.exit(1)
