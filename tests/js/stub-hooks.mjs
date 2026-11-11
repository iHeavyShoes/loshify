// Resolve the three allowed bare specifiers to in-memory stubs (node --import ./tests/js/register.mjs).
const STUBS = {
  '@hermes/plugin-sdk': `
    export const calls = { notify: [], navigate: [], submit: [] }
    export const composer = { accept: true }
    export function atom(v) { let x = v; return { get: () => x, set: n => { x = n } } }
    export const useValue = a => a.get()
    export const host = {
      notify: o => calls.notify.push(o),
      navigate: route => calls.navigate.push(route),
      composer: { submit: (sid, text) => { calls.submit.push({ sid, text }); return composer.accept } }
    }
    const C = name => { const f = props => ({ type: name, props }); f.displayName = name; return f }
    export const Button = C('Button'), Kbd = C('Kbd'), Popover = C('Popover'), PopoverContent = C('PopoverContent'),
      PopoverTrigger = C('PopoverTrigger'), Textarea = C('Textarea'), Tip = C('Tip'), SearchField = C('SearchField'),
      Input = C('Input')
    export const icons = new Proxy({}, { get: (_, k) => C(String(k)) })
    export const KEYBINDS_AREA = 'keybinds'
    export const STATUSBAR_AREAS = { right: 'statusBar.right', left: 'statusBar.left' }
  `,
  'react/jsx-runtime': `
    export const jsx = (type, props) => ({ type, props })
    export const jsxs = jsx
  `,
  react: `export default {}`
}
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: 'stub:' + specifier, shortCircuit: true }
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
