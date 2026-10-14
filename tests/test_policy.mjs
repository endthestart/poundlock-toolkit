// Execute the shipped hook with synthetic events; no model, shell or network tools run.
// Substitutions mirror what pi.py embeds from a PiFixture for the coding and
// investigation fixtures, so this one policy file is exercised both ways.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const template = readFileSync(new URL('../src/poundlock_toolkit/data/pi-policy.ts', import.meta.url), 'utf8');

function install(substitutions) {
  const source = template
    .replace('__TEMPERATURE__', '0')
    .replace('__MAX_OUTPUT__', '512')
    .replace('__VERIFIER_COMMAND_JSON__', JSON.stringify(substitutions.verifier ?? ''))
    .replace('__ALLOWED_WRITE_JSON__', JSON.stringify(substitutions.allowedWrite ?? []))
    .replace('__READABLE_PREFIXES_JSON__', JSON.stringify(substitutions.readablePrefixes ?? []))
    .replace('__REPORT_CONTRACT_JSON__', JSON.stringify(substitutions.reportContract ?? ''));
  const url = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
  return { source, url };
}

// --- Coding fixture: exact verifier command, two writable/readable files. ---
const verify = 'python3 -I /fixture/verify.py /work/slugify.py';
const { url: codingUrl } = install({
  verifier: verify,
  allowedWrite: ['/work/slugify.py', '/work/RESULT.json'],
  readablePrefixes: ['/fixture/verify.py'],
});
const { default: installCoding } = await import(codingUrl);
const codingHooks = {};
installCoding({ on: (name, hook) => { codingHooks[name] = hook; } });

for (const event of [
  { toolName: 'bash', input: { command: 'another command' } },
  { toolName: 'bash', input: { command: verify + ' extra-argument' } },
  { toolName: 'bash', input: { command: verify + '\n' } },
  { toolName: 'write', input: { path: '/home/pi/.pi/agent/models.json' } },
  { toolName: 'edit', input: { path: '/fixture/limit.ts' } },
  { toolName: 'write', input: { path: '../outside.py' } },
  { toolName: 'read', input: { path: '/etc/passwd' } },
  { toolName: 'unknown', input: { path: '/work/slugify.py' } },
  { toolName: 'read', input: null },
]) assert.equal((await codingHooks.tool_call(event)).block, true);
for (const event of [
  { toolName: 'bash', input: { command: verify } },
  { toolName: 'write', input: { path: 'slugify.py' } },
  { toolName: 'edit', input: { path: '/work/RESULT.json' } },
  { toolName: 'read', input: { path: '/fixture/verify.py' } },
]) assert.equal(await codingHooks.tool_call(event), undefined);
for (let i = 0; i < 12; i++) {
  const payload = await codingHooks.before_provider_request({ payload: { temperature: 2, max_tokens: 99999 } });
  assert.equal(payload.temperature, 0); assert.equal(payload.max_tokens, 512);
}
const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
  const {default:install}=await import(${JSON.stringify(codingUrl)});
  const hooks={}; install({on:(n,h)=>hooks[n]=h});
  for(let i=0;i<13;i++) await hooks.before_provider_request({payload:{}});
`], { timeout: 5000 });
assert.equal(child.status, 43);

// --- Compaction gate off (the default): no report contract, messages pass through.
// A fresh install, since `codingHooks` above already spent its 12-request budget. ---
{
  const { url: ungatedUrl } = install({
    verifier: verify,
    allowedWrite: ['/work/slugify.py', '/work/RESULT.json'],
    readablePrefixes: ['/fixture/verify.py'],
  });
  const { default: installUngated } = await import(ungatedUrl);
  const ungatedHooks = {};
  installUngated({ on: (name, hook) => { ungatedHooks[name] = hook; } });
  const noContractPayload = await ungatedHooks.before_provider_request({
    payload: { messages: [{ role: 'user', content: 'hi' }] },
  });
  assert.deepEqual(noContractPayload.messages, [{ role: 'user', content: 'hi' }]);
}

// --- Compaction gate on: the report contract is re-asserted in the system
// role on every request, appended to an existing system message or, absent
// one, inserted ahead of the conversation -- never in a user/assistant turn
// that compaction could summarize away. ---
const contract = 'Always finish by writing RESULT.json.';
const { url: gatedUrl } = install({
  verifier: verify,
  allowedWrite: ['/work/slugify.py', '/work/RESULT.json'],
  readablePrefixes: ['/fixture/verify.py'],
  reportContract: contract,
});
const { default: installGated } = await import(gatedUrl);
const gatedHooks = {};
installGated({ on: (name, hook) => { gatedHooks[name] = hook; } });

const withoutSystem = await gatedHooks.before_provider_request({
  payload: { messages: [{ role: 'user', content: 'hi' }] },
});
assert.deepEqual(withoutSystem.messages, [
  { role: 'system', content: contract },
  { role: 'user', content: 'hi' },
]);

const withSystem = await gatedHooks.before_provider_request({
  payload: {
    messages: [
      { role: 'system', content: 'Be helpful.' },
      { role: 'user', content: 'hi' },
    ],
  },
});
assert.deepEqual(withSystem.messages, [
  { role: 'system', content: `Be helpful.\n\n${contract}` },
  { role: 'user', content: 'hi' },
]);

// --- Investigation fixture: no verifier command at all (bash fully blocked),
// a single writable output, and a whole read-only corpus prefix. ---
const { url: investigationUrl } = install({
  verifier: '',
  allowedWrite: ['/work/RESULT.json'],
  readablePrefixes: ['/fixture/corpus/'],
});
const { default: installInvestigation } = await import(investigationUrl);
const investigationHooks = {};
installInvestigation({ on: (name, hook) => { investigationHooks[name] = hook; } });

for (const event of [
  { toolName: 'bash', input: { command: 'python3 -I /fixture/verify_investigation.py' } },
  { toolName: 'bash', input: { command: '' } },
  { toolName: 'write', input: { path: 'slugify.py' } },
  { toolName: 'read', input: { path: '/fixture/answer-key.json' } },
  { toolName: 'read', input: { path: '/fixture/models.json' } },
]) assert.equal((await investigationHooks.tool_call(event)).block, true);
for (const event of [
  { toolName: 'write', input: { path: 'RESULT.json' } },
  { toolName: 'edit', input: { path: '/work/RESULT.json' } },
  { toolName: 'read', input: { path: '/fixture/corpus/catalog/entry-alpha.md' } },
  { toolName: 'read', input: { path: '/fixture/corpus/modules/epsilon-9.md' } },
]) assert.equal(await investigationHooks.tool_call(event), undefined);

console.log('Pi policy: negative tool controls, legitimate fixture paths, payload limits and request cap passed for both the coding and investigation fixtures');
