import { fileURLToPath } from 'node:url'
import { base, cleanUrls } from '../base.mjs'

// Browser checks run against whatever is serving the built site: a local
// preview (`npm run preview`), a static server that emulates a /<repo>/ prefix,
// or the deployed site. Point SITE_URL at the origin and keep DOCS_BASE the same
// as during the build; routes then resolve exactly as they do in production.
export const origin = (process.env.SITE_URL || 'http://localhost:4173').replace(/\/+$/, '')

export const contentRoot = fileURLToPath(new URL('../../gitbook', import.meta.url))

export function pageUrl(route) {
  const path = route.replace(/^\/+/, '').replace(/\.html$/, '')
  return `${origin}${base}${path}${cleanUrls ? '' : '.html'}`
}
