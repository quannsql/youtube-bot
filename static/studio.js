'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const labels = {pending: 'Đang chờ', processing: 'Đang xử lý', done: 'Hoàn tất', failed: 'Cần kiểm tra'};
const modes = {movie: 'Review phim', short: 'Short so sánh', long: 'Video giải thích'};
function toast(message) {const el = $('#toast'); if (!el) return; el.textContent = message; el.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => el.hidden = true, 6000);}
function formError(form, message) {let el = $('.form-error', form); if (!el) {el = document.createElement('p'); el.className = 'form-error'; el.setAttribute('role', 'alert'); form.appendChild(el);} el.textContent = message || ''; el.hidden = !message;}
async function responseJSON(response) {
  if (response.redirected && new URL(response.url).pathname === '/login') {location.assign('/login'); throw Error('Phiên đăng nhập đã hết hạn.');}
  let data;
  try {data = await response.json();} catch {
    if (!response.ok) throw Error(`Máy chủ báo lỗi HTTP ${response.status}. Mở lịch sử kiểm tra có job mới chưa trước khi bấm lại; nếu vẫn lỗi, kiểm tra log server.`);
    throw Error('Máy chủ trả về trang web thay vì kết quả thao tác. Mở lịch sử kiểm tra có job mới chưa trước khi bấm lại.');
  }
  if (!response.ok) throw Error(data.error || `Không thực hiện được thao tác (HTTP ${response.status}).`);
  return data;
}
function element(tag, text, className) {const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;}
async function cancelJob(id, button) {button.disabled = true; try {const body = new FormData(); body.set('csrf', document.body.dataset.csrf); await responseJSON(await fetch(`/cancel/${id}`, {method: 'POST', body})); toast(`Đã huỷ job #${id}.`); if ($('#review-snapshot')) location.reload(); else await pollJobs();} catch (e) {toast(e.message);} finally {button.disabled = false;}}
document.querySelectorAll('.cancel-job').forEach(button => button.addEventListener('click', () => cancelJob(button.dataset.id, button)));
document.querySelectorAll('.idea-form').forEach(form => form.addEventListener('submit', async event => {event.preventDefault(); const button = $('button[type=submit]', form), text = button.textContent; button.disabled = true; button.textContent = 'Đang đưa vào hàng đợi…'; formError(form, ''); try {const data = await responseJSON(await fetch(form.action, {method: 'POST', body: new FormData(form)})); form.reset(); toast(`Đã thêm job #${data.id} vào hàng đợi.`); await pollJobs(); $('#history').scrollIntoView({behavior: 'smooth', block: 'start'});} catch (e) {formError(form, e.message);} finally {button.disabled = false; button.textContent = text;}}));
let jobs = [], polling = false;
function renderJobs() {const body = $('#jobs-body'); if (!body) return; const query = $('#job-search').value.trim().toLocaleLowerCase('vi'), mode = $('#job-mode').value, status = $('#job-state').value;
  const filtered = jobs.filter(job => (!mode || job.mode === mode) && (!status || job.status === status) && `${job.id} ${job.idea} ${job.output_title || ''}`.toLocaleLowerCase('vi').includes(query)); body.replaceChildren();
  if (!filtered.length) {const row = element('tr'), cell = element('td', jobs.length ? 'Không có video khớp bộ lọc.' : 'Chưa có video nào. Bắt đầu bằng một clip hoặc ý tưởng của bạn.', 'empty'); cell.colSpan = 5; row.append(cell); body.append(row); return;}
  for (const job of filtered) {const row = element('tr'), title = element('td'); title.append(element('strong', job.output_title || job.idea), element('small', `#${job.id} · ${job.publish ? 'Đăng YouTube' : 'Bản nháp'}`)); if (job.error) title.append(element('small', job.error, 'job-error')); const kind = element('td', modes[job.mode] || job.mode), state = element('td'); state.append(element('span', labels[job.status] || job.status, `badge ${job.status}`)); const date = element('td'); if (job.created_at) {const parsed = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(job.created_at) ? job.created_at : `${job.created_at.replace(' ', 'T')}Z`); date.textContent = Number.isNaN(parsed.valueOf()) ? job.created_at : parsed.toLocaleString('vi-VN', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});} const result = element('td');
    if (job.review_url) {const link = element('a', 'Xem review ↗'); link.href = job.review_url; result.append(link);} else if (job.youtube_id && /^[A-Za-z0-9_-]+$/.test(job.youtube_id)) {const link = element('a', 'YouTube ↗'); link.href = `https://www.youtube.com/watch?v=${job.youtube_id}`; link.target = '_blank'; link.rel = 'noopener'; result.append(link);} else result.append(element('span', '—', 'muted'));
    if (['pending', 'processing'].includes(job.status)) {const button = element('button', 'Huỷ job', 'text-button'); button.type = 'button'; button.addEventListener('click', () => cancelJob(job.id, button)); result.append(button);} row.append(title, kind, state, date, result); body.append(row);}
}
async function pollJobs() {if (!$('#jobs-body') || polling) return; polling = true; try {jobs = await responseJSON(await fetch('/api/jobs', {cache: 'no-store'})); $('#stat-total').textContent = jobs.length; $('#stat-working').textContent = jobs.filter(j => ['pending','processing'].includes(j.status)).length; $('#stat-done').textContent = jobs.filter(j => j.status === 'done').length; $('#stat-failed').textContent = jobs.filter(j => j.status === 'failed').length; renderJobs(); $('#connection').textContent = `40 job gần nhất · Cập nhật ${new Date().toLocaleTimeString('vi-VN')} · Tự làm mới mỗi 5 giây`;} catch (e) {$('#connection').textContent = `Chưa cập nhật được: ${e.message}`;} finally {polling = false;}}
if ($('#jobs-body')) {['#job-search','#job-mode','#job-state'].forEach(id => $(id).addEventListener('input', renderJobs)); $('#refresh-jobs').addEventListener('click', pollJobs); pollJobs(); setInterval(() => {if (!document.hidden) pollJobs();}, 5000);}
const movieForm = $('#movie-form');
if (movieForm) {const input = $('#video-file'), drop = $('#drop-zone'), preview = $('#source-preview'), selected = $('#selected-file'); let objectURL;
  function clearFile() {if (objectURL) URL.revokeObjectURL(objectURL); objectURL = null; preview.removeAttribute('src'); preview.load(); input.value = ''; drop.hidden = false; selected.hidden = true;}
  function selectFile() {const file = input.files[0]; if (!file) return; if (!/\.mp4$/i.test(file.name)) {formError(movieForm, 'Hãy chọn video định dạng MP4.'); clearFile(); return;} if (file.size > Number(movieForm.dataset.maxMb) * 1024 * 1024) {formError(movieForm, `MP4 quá ${movieForm.dataset.maxMb} MB. Chọn clip ngắn hơn.`); clearFile(); return;} formError(movieForm, ''); if (objectURL) URL.revokeObjectURL(objectURL); objectURL = URL.createObjectURL(file); preview.src = objectURL; $('#file-name').textContent = file.name; $('#file-meta').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · MP4`; preview.onloadedmetadata = () => {if (Number.isFinite(preview.duration)) $('#file-meta').textContent += ` · ${Math.floor(preview.duration / 60)}:${String(Math.round(preview.duration % 60)).padStart(2,'0')}`;}; drop.hidden = true; selected.hidden = false;}
  input.addEventListener('change', selectFile); $('#remove-file').addEventListener('click', clearFile);
  ['dragenter','dragover'].forEach(name => drop.addEventListener(name, event => {event.preventDefault(); drop.classList.add('dragging');})); ['dragleave','drop'].forEach(name => drop.addEventListener(name, event => {event.preventDefault(); drop.classList.remove('dragging');})); drop.addEventListener('drop', event => {if (!event.dataTransfer.files.length) return; const files = new DataTransfer(); files.items.add(event.dataTransfer.files[0]); input.files = files.files; selectFile();});
  const rights = $('#rights-status'); function updateRights() {const needs = ['owned','licensed','legal_basis'].includes(rights.value); $('#rights-basis-label').hidden = !needs; $('#rights-basis').required = needs;} rights.addEventListener('change', updateRights); updateRights();
  $('#context-preset').addEventListener('change', event => {if (event.target.value === 'avengers-doomsday') $('#movie-title').value = 'Avengers: Doomsday';});
  movieForm.addEventListener('submit', event => {event.preventDefault(); if (!input.files.length) {formError(movieForm, 'Chọn một video MP4 trước khi bắt đầu.'); return;} const button = $('#movie-submit'), old = button.textContent, xhr = new XMLHttpRequest(); button.disabled = true; button.textContent = 'Đang upload…'; formError(movieForm, ''); $('#upload-progress').hidden = false;
    function fail(message) {formError(movieForm, message); button.disabled = false; button.textContent = old; $('#upload-progress').hidden = true;}
    xhr.open('POST', movieForm.action); xhr.upload.onprogress = event => {if (!event.lengthComputable) return; const percent = Math.round(event.loaded / event.total * 100); $('#upload-fill').style.width = `${percent}%`; $('#upload-message').textContent = percent === 100 ? 'Đã gửi file. Đang kiểm tra video…' : `Đang gửi video · ${percent}%`;};
    xhr.onload = () => {const destination = xhr.responseURL && new URL(xhr.responseURL); if (destination && destination.pathname === '/login') {location.assign('/login'); return;} if (xhr.status >= 200 && xhr.status < 300 && destination && /^\/movies\/\d+$/.test(destination.pathname)) {location.assign(destination.href); return;} let message = 'Không tạo được job. Kiểm tra file hoặc tải lại trang.'; try {message = JSON.parse(xhr.responseText).error || message;} catch {} fail(message);}; xhr.onerror = () => fail('Kết nối bị gián đoạn. Kiểm tra lịch sử trước khi upload lại để tránh tạo trùng job.'); xhr.onabort = () => fail('Upload đã bị ngắt.'); xhr.send(new FormData(movieForm));
  }); window.addEventListener('pagehide', () => {if (objectURL) URL.revokeObjectURL(objectURL);});
}
document.querySelectorAll('.narration-style-preset').forEach(select => select.addEventListener('change', () => {
  const field = $('textarea[name=narration_style]', select.closest('form'));
  if (field && select.value) field.value = select.value;
}));
document.querySelectorAll('.movie-retry-form').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault(); const button = $('button', form), old = button.textContent;
  const controller = new AbortController(), upload = $('input[type=file]', form);
  const timeout = setTimeout(() => controller.abort(), upload && upload.files.length ? 600000 : 45000);
  button.disabled = true; button.textContent = 'Đang kiểm tra…'; formError(form, '');
  try {
    // The input named "action" shadows form.action in a real browser DOM.
    const response = await fetch(form.getAttribute('action'), {method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}, signal: controller.signal});
    const destination = new URL(response.url);
    if (response.redirected && destination.pathname === '/login') {location.assign(destination.href); return;}
    // Older deployments acknowledge a queued retry by redirecting to its HTML
    // review page. Fetch follows that redirect; it must not be parsed as JSON.
    if (response.ok && response.redirected && /^\/movies\/\d+$/.test(destination.pathname)) {location.assign(destination.href); return;}
    const data = await responseJSON(response);
    if (data.ok && data.redirect_url && /^\/movies\/\d+$/.test(data.redirect_url)) {button.textContent = 'Đã vào hàng đợi'; location.assign(data.redirect_url); return;}
    throw new Error('Không tiếp tục được job. Hãy tải lại trang.');
  } catch (e) {
    formError(form, e.name === 'AbortError' ? 'Server chưa phản hồi. Mở lịch sử kiểm tra có job mới chưa trước khi bấm lại để tránh tạo trùng.' : e.message);
  } finally {clearTimeout(timeout); button.disabled = false; button.textContent = old;}
}));
if ($('#review-snapshot')) {const initial = JSON.parse($('#review-snapshot').textContent), config = JSON.parse($('#review-config').textContent); const stages = ['Phân tích nội dung','Kiểm chứng chi tiết','Nhận diện nhân vật','Viết lời bình','Duyệt kịch bản','Giọng đọc & dựng video','Hoàn tất']; let busy = false;
  async function pollReview() {if (document.hidden || busy || !['pending','processing'].includes(initial.status)) return; busy = true; try {const state = await responseJSON(await fetch(config.url, {cache:'no-store'})); if (state.status !== initial.status || state.revision !== initial.revision) {location.reload(); return;} $('#stage-text').textContent = state.stage === 'queued' ? 'Đang chờ trong hàng đợi' : stages[state.step] || state.stage; document.querySelectorAll('[data-step]').forEach(item => {const index = Number(item.dataset.step); item.classList.toggle('finished', state.step > index); item.classList.toggle('current', state.step === index && state.status === 'processing');});} catch (e) {$('#stage-text').textContent = `Chưa cập nhật: ${e.message}`;} finally {busy = false;}} pollReview(); setInterval(pollReview, 5000);
}
