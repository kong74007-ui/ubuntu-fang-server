// Experimental static-copy capture for the hash-locked nine-grid adapter.
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL, fileURLToPath} = require('node:url');

async function main() {
  const [projectArg, outputArg, browserArg, moduleArg] = process.argv.slice(2);
  if (!moduleArg) throw Error('project, output, browser and puppeteer module are required');
  const project = fs.realpathSync(projectArg);
  const values = JSON.parse(fs.readFileSync(path.join(project, 'variables.json'), 'utf8'));
  const puppeteer = require(path.resolve(moduleArg));
  const browser = await puppeteer.launch({headless: true, executablePath: path.resolve(browserArg),
    args: ['--allow-file-access-from-files', '--disable-gpu']});
  try {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on('request', request => {
      try {
        const url = new URL(request.url());
        if (url.protocol === 'data:') return request.continue();
        if (url.protocol !== 'file:') return request.abort();
        const relative = path.relative(project, fs.realpathSync(fileURLToPath(url)));
        if (relative.startsWith('..') || path.isAbsolute(relative)) return request.abort();
        return request.continue();
      } catch { return request.abort(); }
    });
    await page.setViewport({width: 1080, height: 1920, deviceScaleFactor: 1});
    await page.goto(pathToFileURL(path.join(project, 'index.html')).href);
    await page.evaluate(async variables => {
      const root = document.getElementById('root');
      if (root?.dataset.compositionId !== 'nine-grid-reveal') throw Error('Unsupported template');
      for (const element of document.querySelectorAll('[data-var-text]')) {
        const value = variables[element.dataset.varText];
        if (typeof value !== 'string') throw Error('Missing copy');
        element.textContent = value;
      }
      await document.fonts.ready;
      if (![...document.fonts].every(font => font.status === 'loaded')) throw Error('Font not loaded');
      for (const el of document.querySelectorAll('#grid,.fullscreen,.background')) el.style.display = 'none';
      for (const el of [document.documentElement, document.body, root]) el.style.background = 'transparent';
      for (const media of document.querySelectorAll('video,audio')) media.pause();
    }, values);
    await page.screenshot({path: path.resolve(outputArg), omitBackground: true});
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
