'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const Module = require('node:module');
const {run, activate} = require('../extension');

test('executes without shell expansion and preserves paths with spaces', async () => {
  const text = await run(process.execPath, ['-e', 'console.log(process.argv[1])', '$(no-shell); spaces'], process.cwd());
  assert.equal(text.trim(), '$(no-shell); spaces');
});
test('output and execution time are bounded', async () => {
  await assert.rejects(run(process.execPath, ['-e', "console.log('x'.repeat(4096))"], process.cwd(), {maxBytes: 64}), /limit/);
  await assert.rejects(run(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], process.cwd(), {timeoutMs: 50}), /timed out/);
});
test('errors terminate child and settle once', async () => {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  let kills = 0; child.kill = () => kills++;
  const result = run('binary', [], '.', {spawnImpl: () => child});
  child.emit('error', new Error('not found')); child.emit('close', 1);
  await assert.rejects(result, /not found/); assert.equal(kills, 1);
});
test('native MCP provider isolates folders, respects trust, and exposes no approval by default', () => {
  let provider; const commands = new Map(); const subscriptions = [];
  const disposable = {dispose() {}};
  const api = {
    EventEmitter: class { event = () => {}; fire() {} dispose() {} },
    McpStdioServerDefinition: class {constructor(label, command, args) {Object.assign(this, {label, command, args});}},
    window: {createOutputChannel: () => disposable},
    workspace: {isTrusted: true, workspaceFolders: [
      {name: 'One', uri: {fsPath: '/one space'}}, {name: 'Two', uri: {fsPath: '/two'}}],
      getConfiguration: () => ({get: (_key, fallback) => fallback}),
      onDidChangeWorkspaceFolders: () => disposable, onDidGrantWorkspaceTrust: () => disposable,
      onDidChangeConfiguration: () => disposable},
    lm: {registerMcpServerDefinitionProvider: (_id, value) => {provider = value; return disposable;}},
    commands: {registerCommand: (id, fn) => {commands.set(id, fn); return disposable;}}
  };
  const original = Module._load;
  Module._load = function(name, ...args) {return name === 'vscode' ? api : original.call(this, name, ...args);};
  try {activate({subscriptions});} finally {Module._load = original;}
  assert.equal(commands.size, 3);
  const definitions = provider.provideMcpServerDefinitions();
  assert.deepEqual(definitions.map(item => item.args), [
    ['serve', '--dest', '/one space', '--no-approval'], ['serve', '--dest', '/two', '--no-approval']]);
  api.workspace.isTrusted = false;
  assert.deepEqual(provider.provideMcpServerDefinitions(), []);
  subscriptions.forEach(value => value.dispose());
});
