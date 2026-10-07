/** Install locked dependencies and start the isolated synthetic sample. */
import { spawnSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 12)) {
  console.error('Use Node.js 22.12+ and install uv before running this sample.');
  process.exit(1);
}
const check = spawnSync('uv', ['--version'], { cwd: repo, stdio: 'ignore' });
if (check.error || check.status !== 0) {
  console.error('uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and retry.');
  process.exit(1);
}
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
for (const [command, args] of [[npm, ['ci']], ['uv', ['sync', '--project', 'server', '--frozen']]]) {
  const result = spawnSync(command, args, { cwd: repo, stdio: 'inherit' });
  if (result.error || result.status !== 0) {
    console.error('Dependency setup failed; the sample was not started.');
    process.exit(result.status || 1);
  }
}
console.log('Starting synthetic sample. No credentials, schedulers or paid model calls.');
const child = spawn('uv', ['run', '--project', 'server', '--frozen', 'python', 'scripts/start-sample.py', ...process.argv.slice(2)], { cwd: repo, stdio: 'inherit' });
child.on('error', () => { console.error('Could not start the sample.'); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = signal ? 1 : code ?? 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
