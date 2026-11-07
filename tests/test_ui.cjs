const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

function fixture() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      textContent: 'previous run', hidden: false, disabled: true,
      children: ['previous result'],
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
      setAttribute() {},
    });
    return nodes.get(selector);
  };
  const context = vm.createContext({
    document: {
      querySelector: node, querySelectorAll: () => [],
      createElement: () => ({
        children: [], append(...children) { this.children.push(...children); }, setAttribute() {},
      }),
    },
    sessionStorage: {getItem: () => ''},
    confirm: () => true,
  });
  const source = readFileSync('app/static/app.js', 'utf8').replace('guarded(bootstrap)();', '');
  vm.runInContext(source, context);
  vm.runInContext(`
    currentThread = 'previous-thread'; currentRun = 'previous-run';
    api = async () => ({title: 'Empty thread', messages: [], runs: []});
    refreshThreads = async () => {};
  `, context);
  return {context, node};
}

function assertReset(context, node) {
  assert.equal(vm.runInContext('currentRun', context), null);
  assert.equal(node('#send').disabled, false);
  assert.equal(node('#cancel').hidden, true);
  assert.equal(node('#approval').hidden, true);
  assert.equal(node('#usage').textContent, '');
  assert.equal(node('#support-result').hidden, true);
  for (const selector of ['#sources', '#artifacts', '#events', '#approval', '#support-result']) {
    assert.equal(node(selector).children.length, 0);
  }
}

test('opening an empty thread after a pending run enables sending and clears old results', async () => {
  const {context, node} = fixture();
  await vm.runInContext("loadThread('empty-thread')", context);
  assertReset(context, node);
});

test('deleting a thread clears its run controls and result panels', async () => {
  const {context, node} = fixture();
  await node('#delete-thread').onclick();
  assert.equal(vm.runInContext('currentThread', context), null);
  assertReset(context, node);
});

test('support draft edits are included in the proposed ticket, without creating a record directly', async () => {
  const {context, node} = fixture();
  await vm.runInContext(`
    submitMessage = async text => { globalThis.submitted = text; };
    showResult({support_case: {
      category: 'Delivery', reason: 'Needs review', draft: 'Initial draft',
      ticket: {title: 'Order LS-1042', body: 'Customer complaint'}
    }});
  `, context);
  const panel = node('#support-result');
  assert.equal(panel.hidden, false);
  const draft = panel.children.find(child => child.value === 'Initial draft');
  draft.value = 'Reviewed by manager';
  node('#send').disabled = false;
  const button = panel.children.find(child => child.textContent === 'Подготовить заявку');
  await button.onclick();
  assert.match(context.submitted, /^\/ticket Order LS-1042/);
  assert.match(context.submitted, /Reviewed by manager/);
});

test('a new pending run clears the previous support card', async () => {
  const {context, node} = fixture();
  await vm.runInContext('showResult({})', context);
  assert.equal(node('#support-result').hidden, true);
  assert.equal(node('#support-result').children.length, 0);
});
