// Apply the same route-level authentication used by GUI v2 Actions.
// Fail the image build if the upstream route structure changes unexpectedly.
const fs = require('node:fs')
const path = require('node:path')

const guiRoot = process.argv[2]
if (!guiRoot) throw new Error('Expected the NOMAD GUI source directory')

for (const name of ['index.tsx', '$dashboardId.tsx']) {
  const routePath = path.join(guiRoot, 'src', 'routes', 'dashboard', name)
  let source = fs.readFileSync(routePath, 'utf8')
  const authImport = "import {assertAuthentication} from '../../routeUtils'"
  const contextImport = "import {RouterContext} from '../__root'"
  const loader = '  loader: async ({context}) => {'
  const guard = '    assertAuthentication(context)'

  if (!source.includes(authImport)) {
    if (!source.includes(contextImport)) throw new Error(`Cannot locate imports in ${routePath}`)
    source = source.replace(contextImport, `${authImport}\n${contextImport}`)
  }
  if (!source.includes(guard)) {
    if (!source.includes(loader)) throw new Error(`Cannot locate loader in ${routePath}`)
    source = source.replace(loader, `${loader}\n${guard}`)
  }
  if ((source.match(/assertAuthentication\(context\)/g) || []).length !== 1) {
    throw new Error(`Unexpected authentication guard count in ${routePath}`)
  }
  if (!source.includes(`${loader}\n${guard}`)) {
    throw new Error(`Authentication guard is not at the start of the loader in ${routePath}`)
  }
  fs.writeFileSync(routePath, source)
}
