// GitHub Pages serves a project site from /<repository>/, while local builds and
// previews run from the domain root. The deployment workflow sets DOCS_BASE, and
// the build and its verification scripts both derive their paths from here.

const raw = (process.env.DOCS_BASE || '').replace(/^\/+|\/+$/g, '')

export const base = raw ? `/${raw}/` : '/'

// A non-root base means the output is served by GitHub Pages, which is a plain
// static host without extensionless URL rewriting, so links must keep the .html
// suffix there. Local preview keeps the shorter URLs.
export const cleanUrls = base === '/'
