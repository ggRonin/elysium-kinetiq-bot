// Решатель Vercel BotID: исполняет c.js на заглушках браузера и печатает JSON для заголовка x-is-human.
// Использование: node botid.js <User-Agent> < c.js
const vm = require('vm');

const UA = process.argv[2];
const HOST = 'elysium.kinetiq.xyz';

const GL = {
  37445: 'Google Inc. (NVIDIA)',
  37446: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)',
};
const glctx = {
  getExtension: n => n === 'WEBGL_debug_renderer_info' ? { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 } : null,
  getParameter: p => GL[p] ?? null,
};
const quiet = { log() {}, debug() {}, info() {}, warn() {}, error() {} };

function createElement(tag) {
  if (tag === 'canvas') return { getContext: k => /webgl/.test(k) ? glctx : null };
  if (tag === 'iframe') return { style: {}, contentWindow: { Error, Object, console: quiet }, setAttribute() {}, remove() {} };
  return { style: {}, setAttribute() {}, remove() {} };
}

const win = {
  document: {
    currentScript: { src: `https://${HOST}/c.js` },
    body: { appendChild: e => e, removeChild: e => e },
    head: { appendChild: e => e },
    createElement,
    hidden: false,
    visibilityState: 'visible',
  },
  navigator: {
    userAgent: UA, webdriver: false, language: 'en-US', languages: ['en-US', 'en'],
    platform: 'Win32', hardwareConcurrency: 12, deviceMemory: 8,
  },
  location: { host: HOST, origin: `https://${HOST}`, href: `https://${HOST}/testnet-faucet` },
  outerWidth: 1920, outerHeight: 1040, innerWidth: 1920, innerHeight: 945, devicePixelRatio: 1,
  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24 },
  crypto: globalThis.crypto, TextEncoder, TextDecoder, Uint8Array,
  btoa: s => Buffer.from(s, 'binary').toString('base64'),
  atob: s => Buffer.from(s, 'base64').toString('binary'),
  setTimeout, clearTimeout, setInterval, clearInterval,
  console: quiet,
  V_C: [],
};
win.window = win; win.self = win; win.top = win; win.globalThis = win;

let done = false;
win.V_C.push = async f => {
  if (done) return;
  done = true;
  try {
    const r = typeof f === 'function' ? await f() : f;
    process.stdout.write(JSON.stringify(r));
    process.exit(0);
  } catch (e) {
    console.error('solver: ' + e.message);
    process.exit(1);
  }
};

let code = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { code += c; });
process.stdin.on('end', () => {
  vm.createContext(win);
  try {
    vm.runInContext(code, win, { timeout: 10000 });
  } catch (e) {
    console.error('c.js: ' + e.message);
    process.exit(1);
  }
  setTimeout(() => { console.error('solver: timeout'); process.exit(2); }, 10000);
});
