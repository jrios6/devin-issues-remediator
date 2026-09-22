const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(0, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elementIds = new Set(Array.from(html.matchAll(/\bid="([^"]+)"/g), match => match[1]));
const elements = new Map();
function element(id) {
  assert.ok(elementIds.has(id), `Dashboard element #${id} is missing`);
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {
      innerHTML: '', textContent: '', value: '', disabled: false, attributes: {},
      setAttribute(key, value) { this.attributes[key] = value; },
      classList: {
        add(value) { classes.add(value); },
        remove(value) { classes.delete(value); },
        toggle(value, force) {
          if (force ?? !classes.has(value)) classes.add(value);
          else classes.delete(value);
        },
        contains(value) { return classes.has(value); },
      },
    });
  }
  return elements.get(id);
}
const context = vm.createContext({
  Date, AbortSignal,
  document: {
    getElementById: element, querySelectorAll: () => [],
    addEventListener() {},
  },
  fetch: async () => { throw new Error('offline'); },
});
vm.runInContext(script.slice(0, script.lastIndexOf('\nloadCfg();')), context);
const evaluate = source => vm.runInContext(source, context);

evaluate(`
  const base = {created_at: 100, pr_checks: null, acus: null, issue_title: 'Issue', pr_files: 1};
  const review = {...base, issue_number: 1, state: 'pr_opened', pr_state: 'open',
    detail: 'waiting_for_user', pr_url: 'https://example.com/pr/1', pr_checks: 'failing'};
  const waiting = {...base, issue_number: 2, state: 'running', detail: 'waiting_for_user',
    session_url: 'https://example.com/session'};
  const merged = {...base, issue_number: 3, state: 'merged', pr_state: 'merged',
    pr_url: 'https://example.com/pr/3', pr_checks: 'passing', acus: 0};
  const failed = {...base, issue_number: 4, state: 'failed',
    issue_title: '<img src=x onerror=alert(1)>', detail: '<script>alert(1)</script>'};
  cached = {tasks: [review, waiting, merged, failed],
    counts: {by_state: {pr_opened: 1, running: 1, merged: 1, failed: 1},
      totals: {}, durations: {avg_s: 120, sample_count: 2}}};
  integrationHealth = {
    github: {status: 'healthy', last_success: 100, resources: {
      'pr:1': {status: 'healthy'}, 'checks:1': {status: 'healthy'}
    }},
    devin: {status: 'pending', last_success: null, resources: {}}
  };
  pageFresh = true;
  renderTasks();
`);
assert.equal(evaluate('waitingForInput(waiting)'), true);
assert.equal(evaluate('waitingForInput(review)'), false);
assert.equal(evaluate('needsAttention(review, "ci")'), true);
assert.match(element('stats').innerHTML, /dispatch → first PR · 2 runs/);
assert.match(element('secondary-stats').innerHTML, /0.0<\/strong> · 1\/4 metered/);
assert.match(element('taskrows').innerHTML, /Waiting for input/);
assert.match(element('taskrows').innerHTML, /Review PR/);
assert.match(element('taskrows').innerHTML, /View PR/);
assert.doesNotMatch(element('taskrows').innerHTML, /<img|<script>/);
assert.match(element('taskrows').innerHTML, /&lt;img/);
assert.equal(element('tpageinfo').textContent, '1–4 of 4');

evaluate('setAttention("input")');
assert.equal(element('tpageinfo').textContent, '1–1 of 1');
assert.match(element('taskrows').innerHTML, /#2/);
assert.doesNotMatch(element('taskrows').innerHTML, /#1/);
evaluate('setFilter("merged")');
assert.equal(evaluate('attentionFilter'), '');
assert.match(element('taskrows').innerHTML, /Last recorded/);
assert.doesNotMatch(element('taskrows').innerHTML, /PR merged/);
evaluate('setFilter("queued")');
assert.match(element('taskrows').innerHTML, /No matching remediations/);
evaluate('setFilter("all"); toggleSort()');
assert.equal(element('issueth').attributes['aria-sort'], 'ascending');
assert.ok(element('taskrows').innerHTML.indexOf('#1') < element('taskrows').innerHTML.indexOf('#4'));

evaluate('integrationHealth.github.resources["checks:1"].status = "error"; renderTasks()');
assert.equal(evaluate('needsAttention(review, "ci")'), false);
assert.match(evaluate('ciBadge(review)'), /Unverified/);
assert.match(element('attention').innerHTML, /Failing CI · incomplete/);
evaluate('integrationHealth.github.resources["checks:1"].status = "healthy"; pageFresh = false');
assert.match(evaluate('ciBadge(review)'), /Unverified/);
evaluate('pageFresh = true; integrationHealth.github.resources["pr:1"].status = "stale"');
assert.match(evaluate('ciBadge(review)'), /Unverified/);

evaluate(`
  cached.tasks = Array.from({length: 12}, (_, i) => ({...base, issue_number: i + 1, state: 'queued'}));
  stateFilter = 'all'; taskOffset = 0; renderTasks(); tpage(1);
`);
assert.equal(element('tpageinfo').textContent, '11–12 of 12');
assert.equal(element('tnext').disabled, true);
evaluate('setAttention("failed")');
assert.equal(evaluate('taskOffset'), 0);
assert.equal(element('tpageinfo').textContent, '0–0 of 0');
assert.match(element('secondary-stats').innerHTML, /Unavailable/);

evaluate(`
  cachedEvents = Array.from({length: 120}, (_, i) => ({
    ts: 100, kind: 'status', issue_number: i + 1, message: 'Event ' + (i + 1)
  }));
  renderEvents();
`);
assert.equal((element('eventrows').innerHTML.match(/<tr>/g) || []).length, 50);
assert.match(element('eventrows').innerHTML, /Event 50</);
assert.doesNotMatch(element('eventrows').innerHTML, /Event 51</);
assert.equal(element('evmore').classList.contains('hidden'), false);
assert.equal(element('evmore').textContent, 'show 50 more · 70 older');
evaluate('evShowMore()');
assert.equal((element('eventrows').innerHTML.match(/<tr>/g) || []).length, 100);
assert.equal(element('evmore').textContent, 'show 20 more · 20 older');
evaluate('evShowMore()');
assert.equal((element('eventrows').innerHTML.match(/<tr>/g) || []).length, 120);
assert.equal(element('evmore').classList.contains('hidden'), true);
evaluate('cachedEvents = []; renderEvents()');
assert.equal(element('eventrows').innerHTML, '');
assert.equal(element('evmore').classList.contains('hidden'), true);

(async () => {
  evaluate(`
    cached.tasks = [review];
    integrationHealth.github.resources['pr:1'].status = 'healthy';
    pageFresh = true;
    setFilter('all');
  `);
  assert.equal(evaluate('needsAttention(review, "ci")'), true);
  await evaluate('refresh()');
  assert.equal(evaluate('pageFresh'), false);
  assert.equal(element('refresh-error').classList.contains('hidden'), false);
  assert.match(element('refresh-error').textContent, /Showing cached data/);
  assert.match(element('taskrows').innerHTML, /#1/);
  assert.match(element('taskrows').innerHTML, /Unverified/);
  assert.equal(evaluate('needsAttention(review, "ci")'), false);
  assert.match(element('attention').innerHTML, /Failing CI · incomplete/);

  const requests = new Set();
  let tasksResponse = evaluate('cached');
  let healthResponse = evaluate('integrationHealth');
  context.fetch = async url => {
    requests.add(url);
    return {
      ok: true,
      json: async () => url === '/api/tasks' ? tasksResponse
        : url === '/api/integrations' ? healthResponse
          : url === '/api/poller' ? {enabled: false} : {events: [], total: 0},
    };
  };
  await evaluate('refresh()');
  assert.equal(evaluate('pageFresh'), true);
  assert.equal(element('refresh-error').classList.contains('hidden'), true);
  assert.equal(requests.has('/api/integrations'), true);
  assert.equal(requests.has('/api/events?limit=500'), true);
  assert.equal(evaluate('needsAttention(review, "ci")'), true);
  assert.doesNotMatch(element('taskrows').innerHTML, /Unverified/);
  assert.doesNotMatch(element('attention').innerHTML, /incomplete/);

  tasksResponse = {tasks: [], counts: {by_state: {}, totals: {}, durations: {}}};
  healthResponse = {github: {status: 'idle', resources: {}}, devin: {status: 'idle', resources: {}}};
  await evaluate('refresh()');
  assert.equal(evaluate('pageFresh'), true);
  assert.equal(evaluate('integrationHealth.github.status'), 'idle');
  assert.equal(evaluate('integrationHealth.devin.status'), 'idle');
  assert.match(element('taskrows').innerHTML, /No issues yet/);
  evaluate('lastUpdate = Date.now() / 1000 - 31; tickUpdated()');
  assert.equal(evaluate('pageFresh'), false);
  console.log('Dashboard regression checks passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
