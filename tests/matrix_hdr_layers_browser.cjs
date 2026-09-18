// Run against the pinned HyperFrames engine, never a hand-written mask imitation.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');

async function main() {
  const [cliArg, browserPath, fixtures] = process.argv.slice(2);
  if (!cliArg || !browserPath || !fixtures) {
    throw new Error('Usage: node matrix_hdr_layers_browser.cjs <hyperframes/dist/cli.js> <chrome> <fixtures>');
  }
  const cli = path.resolve(cliArg);
  const version = JSON.parse(fs.readFileSync(path.join(path.dirname(cli), '../package.json'), 'utf8')).version;
  assert.equal(version, '0.8.34');
  const source = fs.readFileSync(cli, 'utf8');
  const start = source.indexOf('async function applyDomLayerMask(');
  const end = source.indexOf('async function removeDomLayerMask(', start);
  assert(start > 0 && end > start, 'Pinned engine DOM mask not found');
  const names = ['MEDIA_RENDER_ID_ATTR', 'RENDER_FRAME_ID_PREFIX', 'RENDER_FRAME_ID_SUFFIX',
    'DOM_LAYER_MASK_STYLE_ID', 'DOM_LAYER_MASK_HIDDEN_ATTR', 'DOM_LAYER_MASK_PREV_VISIBILITY_ATTR',
    'DOM_LAYER_MASK_PREV_PRIORITY_ATTR', 'HF_COLOR_GRADING_CANVAS_ID_PREFIX'];
  const constants = names.map(name => {
    const match = source.match(new RegExp(`${name} = ("[^"\\n]*");`));
    assert(match, `Missing engine constant ${name}`);
    return JSON.parse(match[1]);
  });
  const mask = new Function(...names, source.slice(start, end) + '\nreturn applyDomLayerMask;')(...constants);
  const puppeteer = createRequire(cli)('puppeteer-core');
  const browser = await puppeteer.launch({executablePath: browserPath, headless: true,
    args: ['--no-sandbox', '--allow-file-access-from-files']});
  const report = [];
  try {
    for (const name of ['mixed', 'last-still-hdr', 'later-video-hdr', 'sdr-only']) {
      const page = await browser.newPage();
      await page.setViewport({width: 1000, height: 600});
      await page.goto(pathToFileURL(path.join(fixtures, name, 'index.html')).href);
      await page.evaluate(async () => { await Promise.all([...document.images].map(img => img.decode())); });
      const layers = await page.evaluate(() => [...document.querySelectorAll('.fan-band img')].map(img => ({
        id: img.id, hdr: img.getAttribute('src').endsWith('.png'),
        timed: img.hasAttribute('data-start') && img.hasAttribute('data-duration'),
      })));
      assert.equal(layers.length, 18);
      assert.equal(new Set(layers.map(x => x.id)).size, 18);
      assert(layers.every(x => x.timed));
      const hdrIds = layers.filter(x => x.hdr).map(x => x.id);
      const sdrIds = layers.filter(x => !x.hdr).map(x => x.id);
      await mask(page, [...sdrIds, 'title'], [...hdrIds, 'later-hdr']);
      const visible = await page.evaluate(ids => ids.map(id => {
        const el = document.getElementById(id);
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return {id, visibility: getComputedStyle(el).visibility,
          loaded: el.complete && el.naturalWidth > 0, painted: hit === el};
      }), sdrIds);
      assert(visible.every(x => x.visibility === 'visible' && x.loaded && x.painted), JSON.stringify(visible));
      await page.screenshot({path: path.join(fixtures, name, 'dom-mask.png')});
      report.push({name, timed_images: 18, hdr_native_layers: hdrIds.length, sdr_visible: visible.length});
      await page.close();
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(fixtures, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
