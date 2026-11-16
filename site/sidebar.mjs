import { readFileSync } from 'node:fs'

const groups = []
let current = null
const overview = []

const summary = readFileSync(new URL('../gitbook/SUMMARY.md', import.meta.url), 'utf8')

for (const rawLine of summary.split('\n')) {
  const line = rawLine.trim()
  if (!line) continue

  const heading = /^##\s+(.+)$/.exec(line)
  if (heading) {
    current = { text: heading[1].trim(), items: [] }
    groups.push(current)
    continue
  }

  const entry = /^[-*]\s+\[([^\]]+)\]\(([^)]+)\)\s*$/.exec(line)
  if (!entry) continue

  const [, text, source] = entry
  // README.md and index.md both resolve to the directory route.
  const link = '/' + source
    .replace(/(^|\/)(README|index)\.md$/, '$1')
    .replace(/\.md$/, '')
  const item = { text, link: link === '//' ? '/' : link }

  if (current) current.items.push(item)
  else overview.push(item)
}

// The first entry before any group is the overview page. It stays a single
// top-level link so it aligns with the group headings instead of nesting in a
// group of its own.
const top = overview.slice(0, 1)

export const sidebar = [
  ...top,
  ...groups.filter((group) => group.items.length)
]
