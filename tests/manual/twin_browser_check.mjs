// Digital Twin oynatıcısı tarayıcı kontrolü (Chromium headless).
// Kullanım: node twin_browser_check.mjs <html> <çıktı_klasörü> [genişlik] [yükseklik] [t1,t2,...]
// Çıktı: stdout'a tek satır JSON. Hata varsa süreç 0 ile biter, "errors" doluluğu çağıran tarafından denetlenir.
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';

let root = '';
try { root = execSync('npm root -g', { encoding: 'utf8' }).trim(); } catch (e) { /* yoksa */ }
const req = createRequire(path.join(root || process.cwd(), 'x.js'));
let pw;
try { pw = req('playwright'); } catch (e) { pw = createRequire(import.meta.url)('playwright'); }

const [html, outDir, W = '1600', H = '1000', times = ''] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
const browser = await pw.chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: +W, height: +H } });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });
await page.goto('file://' + path.resolve(html));
await page.waitForFunction(() => window.__twin && typeof window.__twin.getTime === 'function');
const out = { errors, shots: [], states: {} };
const tEnd = await page.evaluate(() => window.__twin.getState().tMax);
out.tMax = tEnd;

async function colors() {
  return page.evaluate(() => {
    const c = document.getElementById('scene'); const g = c.getContext('2d');
    const d = g.getImageData(0, 0, c.width, c.height).data; const seen = new Set();
    for (let i = 0; i < d.length; i += 4 * 37) seen.add((d[i] >> 3) + ',' + (d[i + 1] >> 3) + ',' + (d[i + 2] >> 3));
    return seen.size;
  });
}
out.colors = await colors();

// oynatma
await page.evaluate(() => { window.__twin.seek(0); window.__twin.setSpeed(60); });
const a = await page.evaluate(() => window.__twin.getTime());
await page.evaluate(() => window.__twin.play());
await page.waitForTimeout(1200);
const b = await page.evaluate(() => window.__twin.getTime());
await page.evaluate(() => window.__twin.pause());
out.playStart = a; out.playAfter = b;
out.fps = await page.evaluate(() => window.__twin.getState().fps);

const list = (times ? times.split(',').map(Number) : [0, tEnd / 2, tEnd]);
for (const t of list) {
  await page.evaluate((x) => window.__twin.seek(x), t);
  const f = path.join(outDir, `twin_${Math.round(t)}.png`);
  await page.screenshot({ path: f });
  out.shots.push(f);
  out.states[String(Math.round(t))] = await page.evaluate(() => window.__twin.getState());
}
await page.evaluate((x) => window.__twin.seek(x), tEnd);
out.panelCompleted = await page.evaluate(() => document.getElementById('k-comp').textContent);
out.stateEnd = await page.evaluate(() => window.__twin.getState());
out.bodyOverflowX = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
await browser.close();
console.log(JSON.stringify(out));
