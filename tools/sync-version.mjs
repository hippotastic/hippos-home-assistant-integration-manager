import { readFileSync, writeFileSync } from 'node:fs'

const manifestFile = new URL('../custom_components/hippos_home_assistant_integration_manager/manifest.json', import.meta.url)
const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'))
const { version } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
    throw new Error('A release requires a three-part version in package.json')
}
manifest.version = version
writeFileSync(manifestFile, JSON.stringify(manifest, null, 2) + '\n')
