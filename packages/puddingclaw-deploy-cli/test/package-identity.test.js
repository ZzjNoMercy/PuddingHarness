import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('packed Harness installs beside legacy CLI without replacing its command or Home', async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'harness-package-identity-'));
  try {
    const env = { ...process.env, npm_config_cache: path.join(temp, 'cache'), npm_config_offline: 'true',
      npm_config_audit: 'false', npm_config_fund: 'false', PUDDINGHARNESS_HOME: path.join(temp, 'harness-home'),
      PUDDINGCLAW_HOME: path.join(temp, 'legacy-home') };
    const npm = (args, cwd) => execFileSync('npm', args, { cwd, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const legacy = path.join(temp, 'legacy');
    await fs.mkdir(legacy);
    await fs.mkdir(env.PUDDINGCLAW_HOME);
    await fs.writeFile(path.join(env.PUDDINGCLAW_HOME, 'sentinel'), 'legacy state');
    await fs.writeFile(path.join(legacy, 'package.json'), JSON.stringify({ name: '@puddingai/puddingclaw', version: '0.0.0', bin: { puddingclaw: 'cli.js' } }));
    await fs.writeFile(path.join(legacy, 'cli.js'), '#!/usr/bin/env node\nconsole.log("legacy fixture");\n', { mode: 0o755 });
    const pack = (directory) => path.join(temp, JSON.parse(npm(['pack', '--ignore-scripts', '--json', '--pack-destination', temp], directory))[0].filename);
    const legacyTar = pack(legacy);
    const harnessTar = pack(root);
    const prefix = path.join(temp, 'installation');
    await fs.mkdir(prefix);
    npm(['install', '--prefix', prefix, '--ignore-scripts', '--no-package-lock', legacyTar, harnessTar], temp);
    const bins = path.join(prefix, 'node_modules/.bin');
    assert.equal(execFileSync(path.join(bins, 'puddingclaw'), [], { env, encoding: 'utf8' }).trim(), 'legacy fixture');
    const cli = path.join(bins, 'puddingharness');
    const version = JSON.parse(execFileSync(cli, ['version', '--json'], { env, encoding: 'utf8' }));
    assert.equal(version.cli, 'puddingharness');
    const help = execFileSync(cli, ['--help'], { env, encoding: 'utf8' });
    assert.match(help, /puddingharness init/);
    assert.doesNotMatch(help, /puddingclaw/);
    const metadata = JSON.parse(await fs.readFile(path.join(prefix, 'node_modules/@puddingai/puddingharness/package.json')));
    assert.deepEqual(metadata.bin, { puddingharness: 'src/cli.js' });
    assert.equal(metadata.private, true);
    assert.equal(metadata.repository, undefined);
    assert.equal(await fs.readFile(path.join(env.PUDDINGCLAW_HOME, 'sentinel'), 'utf8'), 'legacy state');
    assert.deepEqual(await fs.readdir(env.PUDDINGCLAW_HOME), ['sentinel']);
  } finally {
    await fs.rm(temp, { recursive: true, force: true });
  }
});
