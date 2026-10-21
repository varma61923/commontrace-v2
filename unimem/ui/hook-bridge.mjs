/** Compatibility for previously installed commands. Reinstall hooks for direct Python startup. */
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const args = process.argv.slice(2);
const pythonIndex = args.indexOf('--python');
const python = pythonIndex >= 0 ? args[pythonIndex + 1] : 'python3';
const bridge = fileURLToPath(new URL('../hook_bridge.py', import.meta.url));
const child = spawn(python, ['-I', '-S', bridge, ...args], {stdio: 'inherit'});
child.on('error', () => { process.exitCode = 0; });
child.on('exit', () => { process.exitCode = 0; });
