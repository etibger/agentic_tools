import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html = fs.readFileSync(new URL('../docs/assets/dashboard-demo.html', import.meta.url), 'utf8');
const source = html.match(/<script>([\s\S]+)<\/script>/)[1];
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, { textContent: '', innerHTML: '' });
    return elements.get(id);
  }
};
vm.runInNewContext(source, { document });
const get = id => elements.get(id);
assert.match(get('count').textContent, /2 accepted · 4 remaining/);
const activePlot = get('plot').innerHTML;
get('future').onclick();
assert.notEqual(get('plot').innerHTML, activePlot, 'active task range advances with the observation clock');
get('done').onclick();
const completed = { plot: get('plot').innerHTML, clock: get('clock').textContent };
const freshness = get('freshness').textContent;
get('future').onclick();
assert.equal(get('plot').innerHTML, completed.plot, 'completed chart range is frozen');
assert.equal(get('clock').textContent, completed.clock, 'completed duration is frozen');
assert.notEqual(get('freshness').textContent, freshness, 'observation freshness advances separately');
assert.match(get('count').textContent, /6 accepted · 0 remaining/);
console.log('Dashboard state checks passed: active clock, completed clock, count and freshness separation.');
