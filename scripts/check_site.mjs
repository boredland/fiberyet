// Drive the built site in a real browser. Exits non-zero on the first failed expectation.
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8765';
const fail = (msg) => { console.error('FAIL:', msg); process.exit(1); };
const expect = (cond, msg) => { if (!cond) fail(msg); console.log('ok:', msg); };

const VIEWPORT = { width: 1280, height: 800 };
const PANEL_PX = 320;

const browser = await chromium.launch();
const errors = [];

// Every page the check opens reports its console errors, not just the first one.
async function open(url) {
  const p = await browser.newPage({ viewport: VIEWPORT });
  p.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  p.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await p.goto(url, { waitUntil: 'networkidle' });
  await p.waitForFunction(() => document.querySelectorAll('#districts li').length === 46, null, { timeout: 30000 });
  return p;
}

const page = await open(`${base}/#v=t&c=8.65,50.11,13`);
expect((await page.locator('#districts li').count()) === 46, '46 districts listed');
expect((await page.$eval('input[name=view]:checked', (e) => e.value)) === 't', 'view restored from hash');

await page.waitForTimeout(2000);
await page.click('input[value=e]');
expect((await page.evaluate(() => location.hash)).includes('v=e'), 'switching the radio updates the hash');

await page.click('#districts li:nth-child(3)');
await page.waitForTimeout(2000);
expect(/c=/.test(await page.evaluate(() => location.hash)), 'district click moves the camera');

// Open the page centred on a cell that is known to exist, then click the map centre.
const centre = await page.evaluate(async () => {
  const cells = await fetch('./data/cells.geojson').then((r) => r.json());
  const ring = cells.features.find((f) => !f.properties.s).geometry.coordinates[0].slice(0, 4);
  return [ring.reduce((s, p) => s + p[0], 0) / 4, ring.reduce((s, p) => s + p[1], 0) / 4];
});
const onCell = await open(`${base}/#v=t&c=${centre[0].toFixed(5)},${centre[1].toFixed(5)},16`);
await onCell.waitForTimeout(2000);
await onCell.mouse.click(PANEL_PX + (VIEWPORT.width - PANEL_PX) / 2, VIEWPORT.height / 2);
await onCell.waitForTimeout(500);
expect((await onCell.locator('.maplibregl-popup-content').count()) === 1, 'clicking a cell opens a popup');
expect(/Adressen in diesem 100-m-Feld/.test(await onCell.textContent('.maplibregl-popup-content')), 'popup describes the cell');

const again = await open(await page.evaluate(() => location.href));
expect((await again.$eval('input[name=view]:checked', (e) => e.value)) === 'e', 'reload restores the view');

expect(errors.length === 0, `no console errors on any page${errors.length ? ': ' + JSON.stringify(errors) : ''}`);
await browser.close();
