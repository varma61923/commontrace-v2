/** Native transport only. All admission, learning and memory logic lives in HookKit. */
import net from 'node:net';
import path from 'node:path';
import {spawn} from 'node:child_process';
import fs from 'node:fs';

const options = {};
for (let i = 2; i < process.argv.length; i += 2) options[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
let input = '', finished = false;
const empty = {context: '', injected: []};
function format(reply) {
  const context = reply.context || '';
  if (options.format === 'cline') return JSON.stringify({cancel: false, contextModification: context});
  if (options.format === 'openhands') return JSON.stringify({decision: 'allow', additionalContext: context});
  if (options.format === 'cursor') return JSON.stringify({additional_context: context});
  if (options.format === 'kimi') return context;
  if (options.format === 'windsurf') return '{}';
  if (context && ['claude', 'codex', 'gemini'].includes(options.format)) {
    return JSON.stringify({hookSpecificOutput: {hookEventName: options.event, additionalContext: context}});
  }
  const {delivery_token, ...output} = reply;
  return JSON.stringify(output);
}
function finish(reply) {
  if (finished) return;
  finished = true;
  fs.writeSync(1, format(reply) + '\n');
}
function captureFallback(payload) {
  // A detached writer survives a host timeout. No transcript is passed in argv.
  const args = ['-m', 'commontrace.hook_kit', '--capture-only', '--dest', options.dest,
    '--agent-id', options['agent-id'], '--scope', options.scope || '{}', '--event', options.event];
  if (options.format) args.push('--format', options.format);
  const child = spawn(options.python || 'python3', args, {detached: true, stdio: ['pipe', 'ignore', 'ignore']});
  child.on('error', () => {});
  child.stdin.on('error', () => {});
  child.stdin.end(JSON.stringify(payload));
  child.unref();
}
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {input += chunk; if (input.length > 100000) {finish(empty); process.stdin.destroy();}});
process.stdin.on('end', () => {
  if (finished) return;
  try {
    const payload = JSON.parse(input);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return finish(empty);
    const scope = JSON.parse(options.scope || '{}');
    const endpoint = path.join(options.dest, 'memory', 'hooks.sock');
    const client = net.createConnection({path: endpoint});
    let response = '', connected = false;
    const timer = setTimeout(() => {client.destroy(); finish({...empty, timed_out: true});}, 25);
    client.on('connect', () => {
      connected = true;
      client.end(JSON.stringify({agent: options['agent-id'], scope, event: options.event,
                                format: options.format, payload}) + '\n');
    });
    client.on('data', chunk => {
      if (finished) return;
      response += chunk.toString();
      if (response.length > 100000) {client.destroy(); finish(empty); return;}
      if (response.includes('\n')) {
        clearTimeout(timer);
        try {
          const reply = JSON.parse(response);
          finish(reply);
          if (reply.delivery_token) {
            const ack = net.createConnection({path: endpoint});
            ack.on('error', () => {});
            ack.on('connect', () => ack.end(JSON.stringify({operation: 'ack', agent: options['agent-id'], scope,
                                                          delivery_token: reply.delivery_token}) + '\n'));
            ack.setTimeout(20, () => ack.destroy());
          }
        } catch { finish(empty); }
      }
    });
    client.on('error', () => {
      clearTimeout(timer);
      if (!connected) captureFallback(payload);
      finish(empty);
    });
    client.on('end', () => {clearTimeout(timer); if (!finished) finish(empty);});
  } catch { finish(empty); }
});
process.stdin.on('error', () => finish(empty));
