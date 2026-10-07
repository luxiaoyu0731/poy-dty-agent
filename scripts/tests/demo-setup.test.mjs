import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, chmodSync, readFileSync, rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const entry = new URL('../try-demo.mjs', import.meta.url);
function fixture(run) {
  const root=mkdtempSync(path.join(os.tmpdir(),'poy-demo-setup-'));
  try { run(root); } finally { rmSync(root,{recursive:true,force:true}); }
}
function command(root,name,body) { const p=path.join(root,name); writeFileSync(p,'#!/bin/sh\n'+body);chmodSync(p,0o700); }
test('missing uv stops before installing dependencies',{skip:process.platform==='win32'},()=>fixture(root=>{
  const result=spawnSync(process.execPath,[entry.pathname],{env:{...process.env,PATH:root},encoding:'utf8'});
  assert.equal(result.status,1);assert.match(result.stderr,/uv is required/);assert.doesNotMatch(result.stdout,/Starting/);
}));
test('failed npm setup never starts a sample or syncs Python',{skip:process.platform==='win32'},()=>fixture(root=>{
  const log=path.join(root,'calls');
  command(root,'uv','printf "uv %s\\n" "$*" >> "$CALL_LOG"\nexit 0\n');
  command(root,'npm','printf "npm %s\\n" "$*" >> "$CALL_LOG"\nexit 9\n');
  const result=spawnSync(process.execPath,[entry.pathname],{env:{...process.env,PATH:root,CALL_LOG:log},encoding:'utf8'});
  assert.equal(result.status,9);assert.equal(readFileSync(log,'utf8'),'uv --version\nnpm ci\n');
}));
test('setup uses both lockfiles before isolated launcher',{skip:process.platform==='win32'},()=>fixture(root=>{
  const log=path.join(root,'calls');
  for(const name of ['uv','npm'])command(root,name,`printf "${name} %s\\n" "$*" >> "$CALL_LOG"\nexit 0\n`);
  const result=spawnSync(process.execPath,[entry.pathname,'--web-port','15176'],{env:{...process.env,PATH:root,CALL_LOG:log},encoding:'utf8'});
  assert.equal(result.status,0);assert.equal(readFileSync(log,'utf8'),'uv --version\nnpm ci\nuv sync --project server --frozen\nuv run --project server --frozen python scripts/start-sample.py --web-port 15176\n');
}));
