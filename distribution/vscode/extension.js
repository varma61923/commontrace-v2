'use strict';
const {spawn} = require('node:child_process');

function run(command, args, cwd, {spawnImpl = spawn, timeoutMs = 20000, maxBytes = 524288} = {}) {
  return new Promise((resolve, reject) => {
    const child = spawnImpl(command, args, {cwd, shell: false, windowsHide: true});
    let output = '', bytes = 0, settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) { child.kill(); reject(error); } else resolve(output);
    };
    const timer = setTimeout(() => finish(new Error('CommonTrace command timed out')), timeoutMs);
    const append = (chunk) => {
      if (settled) return;
      bytes += chunk.length;
      if (bytes > maxBytes) finish(new Error('CommonTrace output exceeded the limit'));
      else output += chunk.toString();
    };
    child.stdout.on('data', append);
    child.stderr.on('data', append);
    child.once('error', finish);
    child.once('close', (code) => finish(code === 0 ? null : new Error(`CommonTrace exited with status ${code}`)));
  });
}

function activate(context) {
  const vscode = require('vscode');
  const channel = vscode.window.createOutputChannel('CommonTrace');
  const changed = new vscode.EventEmitter();
  const settings = (folder) => vscode.workspace.getConfiguration('commontrace', folder.uri);
  const executable = (folder) => settings(folder).get('command', 'commontrace');
  const definitions = () => !vscode.workspace.isTrusted ? [] : (vscode.workspace.workspaceFolders || []).map(folder => {
    const args = ['serve', '--dest', folder.uri.fsPath];
    if (!settings(folder).get('approvalTools', false)) args.push('--no-approval');
    return new vscode.McpStdioServerDefinition(`CommonTrace: ${folder.name}`, executable(folder), args, {}, '2.0.0');
  });
  context.subscriptions.push(channel, changed,
    vscode.lm.registerMcpServerDefinitionProvider('commontrace', {
      onDidChangeMcpServerDefinitions: changed.event, provideMcpServerDefinitions: definitions
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => changed.fire()),
    vscode.workspace.onDidGrantWorkspaceTrust(() => changed.fire()),
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration('commontrace')) changed.fire();
    }));
  const register = (id, action) => context.subscriptions.push(vscode.commands.registerCommand(id, async () => {
    if (!vscode.workspace.isTrusted) return vscode.window.showErrorMessage('Trust this workspace before running CommonTrace.');
    const folders = vscode.workspace.workspaceFolders || [];
    const folder = folders.length === 1 ? folders[0] : await vscode.window.showQuickPick(
      folders.map(value => ({label: value.name, folder: value}))).then(value => value && value.folder);
    if (!folder) return;
    try { await action(folder); }
    catch (error) { vscode.window.showErrorMessage(`${error.message}. Install commontrace[serve] and check the executable setting.`); }
  }));
  register('commontrace.initialize', async folder => {
    const choice = await vscode.window.showWarningMessage('Initialize a CommonTrace memory store in this workspace?', 'Initialize');
    if (choice !== 'Initialize') return;
    channel.appendLine(await run(executable(folder), ['init', '--dest', folder.uri.fsPath], folder.uri.fsPath));
    channel.show(true);
    changed.fire();
  });
  register('commontrace.doctor', async folder => {
    channel.appendLine(await run(executable(folder), ['doctor', '--dest', folder.uri.fsPath], folder.uri.fsPath));
    channel.show(true);
  });
  register('commontrace.copyConfig', async folder => {
    await vscode.env.clipboard.writeText(await run(executable(folder),
      ['integration', '--client', 'vscode', '--dest', folder.uri.fsPath, '--command', executable(folder),
        ...(settings(folder).get('approvalTools', false) ? ['--approval'] : [])], folder.uri.fsPath));
    vscode.window.showInformationMessage('CommonTrace MCP configuration copied.');
  });
}
module.exports = {activate, run};
