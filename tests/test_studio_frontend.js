// Execute the real dashboard handlers with DOM/network doubles: no APIs.
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/studio.js'), 'utf8');

async function submitRetry(mode, upload = false, action = 'resume') {
  let handler, error, timer;
  const button = {disabled: false, textContent: 'Tiếp tục từ bước đã lưu'};
  const destinations = [], requests = [], delays = [];
  let jsonReads = 0;
  const form = {
    // HTMLFormElement named controls override native properties. The real
    // template has <input name="action">, so form.action is this input.
    action: {value: action, toString() {return '[object HTMLInputElement]';}},
    getAttribute(name) {assert.equal(name, 'action'); return '/movies/18/retry';},
    querySelector(selector) {
      if (selector === 'button') return button;
      if (selector === '.form-error') return error || null;
      if (selector === 'input[type=file]') return upload ? {files: [{}]} : null;
      return null;
    },
    appendChild(node) {error = node;},
    addEventListener(name, callback) {assert.equal(name, 'submit'); handler = callback;},
  };
  const context = {
    URL, AbortController,
    FormData: class {constructor(value) {assert.equal(value, form);}},
    document: {
      querySelector: () => null,
      querySelectorAll: selector => selector === '.movie-retry-form' ? [form] : [],
      createElement: () => ({setAttribute(name, value) {this[name] = value;}}),
    },
    location: {assign: value => destinations.push(value)},
    setTimeout(callback, ms) {timer = callback; delays.push(ms); return 1;},
    clearTimeout() {},
    async fetch(url, options) {
      requests.push({url, options});
      if (mode === 'timeout') return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), {name: 'AbortError'})));
      });
      const redirect = mode === 'redirect' || mode === 'login' || mode === 'review-error';
      const html = redirect || mode === 'html-error' || mode === 'unexpected-html';
      return {
        url: redirect ? `https://studio.example/${mode === 'login' ? 'login' : 'movies/19'}` : new URL(url, 'https://studio.example').href,
        redirected: redirect,
        ok: ['success', 'redirect', 'login', 'unexpected-html'].includes(mode),
        status: mode === 'html-error' || mode === 'review-error' ? 502 : mode === 'error' ? 409 : 200,
        json: async () => {
          jsonReads++;
          if (html) throw new SyntaxError('Unexpected token <');
          return mode === 'success' ? {ok: true, job_id: 19, redirect_url: '/movies/19'} : {error: 'Không còn video gốc'};
        },
      };
    },
  };
  vm.runInNewContext(source, context, {filename: 'studio.js'});
  const pending = handler({preventDefault() {}});
  // The previous bug threw here before reaching fetch: this form has no error element.
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/movies/18/retry');
  assert.equal(requests[0].options.headers.Accept, 'application/json');
  assert.equal(error.role, 'alert');
  assert.equal(button.disabled, true);
  if (mode === 'timeout') timer();
  await pending;
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Tiếp tục từ bước đã lưu');
  assert.equal(delays[0], upload ? 600000 : 45000);
  if (mode === 'success') {
    assert.deepEqual(destinations, ['/movies/19']);
    assert.equal(error.hidden, true);
  } else if (mode === 'redirect' || mode === 'login') {
    assert.deepEqual(destinations, [`https://studio.example/${mode === 'login' ? 'login' : 'movies/19'}`]);
    assert.equal(error.hidden, true);
    assert.equal(jsonReads, 0); // successful review/login pages contain HTML
  } else {
    assert.deepEqual(destinations, []);
    assert.equal(error.hidden, false);
    const expected = mode === 'timeout' ? 'kiểm tra có job mới'
      : mode === 'html-error' || mode === 'review-error' ? 'HTTP 502'
      : mode === 'unexpected-html' ? 'kiểm tra có job mới' : 'Không còn video gốc';
    assert.ok(error.textContent.includes(expected), error.textContent);
  }
  assert.equal(requests.length, 1); // no automatic retry after an uncertain response
}

(async () => {
  await submitRetry('success');
  await submitRetry('error');
  await submitRetry('timeout');
  await submitRetry('success', true);
  await submitRetry('redirect');
  await submitRetry('login');
  await submitRetry('html-error');
  await submitRetry('unexpected-html');
  await submitRetry('review-error');
  await submitRetry('success', false, 'script');
  await submitRetry('success', false, 'voice');
  process.stdout.write('11 dashboard retry behaviours passed\n');
})().catch(error => {process.stderr.write(String(error.stack)); process.exitCode = 1;});
