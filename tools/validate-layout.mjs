import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const readJson = (path) => JSON.parse(readFileSync(new URL(path, root), 'utf8'))
const components = new URL('custom_components/', root)
const [domain, ...others] = readdirSync(components, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'))
    .map((entry) => entry.name)
assert.equal(others.length, 0, 'Marketplace expects a single integration')
assert.match(domain, /^[a-z][a-z0-9_]+$/)

const manifest = readJson(`custom_components/${domain}/manifest.json`)
const hacs = readJson('hacs.json')
const pkg = readJson('package.json')
assert.equal(manifest.domain, domain)
assert.equal(manifest.name, hacs.name)
assert.equal(manifest.version, pkg.version)
assert.match(pkg.version, /^\d+\.\d+\.\d+$/)
assert.match(hacs.homeassistant, /^\d{4}\.\d+\.\d+$/)

for (const path of ['README.md', 'LICENSE', `custom_components/${domain}/__init__.py`, `custom_components/${domain}/config_flow.py`]) {
    assert.ok(existsSync(new URL(path, root)), `Missing ${path}`)
}

const en = readJson(`custom_components/${domain}/translations/en.json`)
const de = readJson(`custom_components/${domain}/translations/de.json`)
function keys(value, prefix = '') {
    if (typeof value === 'string') {
        assert.ok(value.trim(), `Empty translation ${prefix}`)
        const placeholders = Array.from(value.matchAll(/\{([a-z_]+)\}/g), (match) => match[1])
        return [[prefix, placeholders.sort()]]
    }
    return Object.keys(value).sort().flatMap((name) => keys(value[name], `${prefix}.${name}`))
}
assert.deepEqual(keys(de), keys(en), 'Translations must use the same keys and placeholders')
console.log(`Marketplace layout and translations verified for ${domain} ${pkg.version}.`)
