import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

function git(root, args) {
  return execFileSync('git', ['-C', root, ...args], {encoding:'utf8', maxBuffer:32*1024*1024});
}

export async function cleanSourceRevision(root) {
  const canonical = await fs.realpath(root);
  if (await fs.realpath(git(root,['rev-parse','--show-toplevel']).trim()) !== canonical) {
    throw new Error('source must be the repository root');
  }
  if (git(root,['status','--porcelain=v1','--untracked-files=all']).trim()) {
    throw new Error('runtime build requires a clean committed source checkout');
  }
  const revision = git(root,['rev-parse','HEAD']).trim();
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('invalid source commit');
  return revision;
}

export async function verifyCommittedStage(root, prefix, stage, files, revision) {
  const tree = new Map(git(root,['ls-tree','-r','--full-tree','-z',revision]).split('\0').filter(Boolean).map(row => {
    const [metadata, name] = row.split('\t');
    const [mode,type,digest] = metadata.split(' ');
    return [name,{mode,type,digest}];
  }));
  for (const item of files) {
    const record = tree.get(`${prefix}/${item.path}`);
    if (!record || record.type !== 'blob' || !['100644','100755'].includes(record.mode)) {
      throw new Error(`staged file is not a committed regular source file: ${prefix}/${item.path}`);
    }
    const data = await fs.readFile(path.join(stage,item.path));
    const blob = crypto.createHash('sha1').update(`blob ${data.length}\0`).update(data).digest('hex');
    const digest = crypto.createHash('sha256').update(data).digest('hex');
    if (blob !== record.digest || digest !== item.sha256) {
      throw new Error(`staged file does not match source commit: ${prefix}/${item.path}`);
    }
  }
}
