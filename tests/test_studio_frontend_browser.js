// Regression test using the HTML rendered by Flask and a real browser DOM.
// All requests are intercepted locally; no worker, Railway or AI APIs run.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let chromium;
try {({chromium} = require('playwright'));} catch {
  process.stdout.write('Playwright unavailable\n'); process.exit(77);
}
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const script = fs.readFileSync(path.join(__dirname, '../static/studio.js'), 'utf8');

(async () => {
  let browser;
  try {browser = await chromium.launch({channel: 'chrome', headless: true});} catch (error) {
    if (/Executable doesn't exist|distribution 'chrome' is not found/.test(error.message)) {
      process.stdout.write('Chrome unavailable\n'); process.exit(77);
    }
    throw error;
  }
  try {
    for (const action of ['resume', 'script', 'voice', 'restore']) {
      const page = await browser.newPage();
      const posts = [];
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.method() === 'POST') {
          posts.push(request);
          if (url.pathname !== fixture.review_path + '/retry') {
            await route.fulfill({status: 404, contentType: 'text/html', body: '<h1>Not Found</h1>'});
            return;
          }
          await route.fulfill({contentType: 'application/json', body: JSON.stringify({ok: true, job_id: 999, redirect_url: '/movies/999'})});
        } else if (url.pathname === fixture.review_path) {
          await route.fulfill({contentType: 'text/html', body: action === 'restore' ? fixture.restore_html : fixture.review_html});
        } else if (url.pathname === '/static/studio.js') {
          await route.fulfill({contentType: 'application/javascript', body: script});
        } else if (url.pathname === '/movies/999') {
          await route.fulfill({contentType: 'text/html', body: '<h1>Queued job</h1>'});
        } else {
          await route.fulfill({status: 200, body: ''});
        }
      });
      await page.goto('https://studio.example' + fixture.review_path);
      const expectedAction = action === 'restore' ? 'resume' : action;
      const form = page.locator('.movie-retry-form').filter({has: page.locator(`input[name="action"][value="${expectedAction}"]`)});
      // Prove the collision in the actual DOM, not just a simplified form double.
      assert.equal(await form.evaluate(node => node.action instanceof HTMLInputElement), true);
      if (action === 'restore') {
        await form.locator('input[type=file]').setInputFiles({name: 'clip.mp4', mimeType: 'video/mp4', buffer: Buffer.from('test-video')});
      }
      const sent = page.waitForRequest(request => request.method() === 'POST', {timeout: 5000});
      await form.locator('button').click();
      const request = await sent;
      assert.equal(new URL(request.url()).pathname, fixture.review_path + '/retry', action);
      assert.equal(request.headers().accept, 'application/json');
      assert.ok(request.postData().includes(`name="action"\r\n\r\n${expectedAction}\r\n`));
      await page.waitForURL('https://studio.example/movies/999', {timeout: 5000});
      assert.equal(posts.length, 1);
      await page.close();
    }
    process.stdout.write('4 real DOM retry behaviours passed\n');
  } finally {await browser.close();}
})().catch(error => {process.stderr.write(String(error.stack)); process.exitCode = 1;});
