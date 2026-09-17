import fs from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
if (!process.env.PGDATABASE) throw new Error('Run with with-render-db.mjs');
process.umask(0o077);
const root=join(homedir(),'.rexy-backups');
fs.mkdirSync(root,{recursive:true,mode:0o700});
const dir=fs.mkdtempSync(join(root,'session-repair-'));
const file=join(dir,'pre-repair.dump');
console.log(JSON.stringify({backup:file}));
const dump=spawnSync('/opt/homebrew/opt/libpq/bin/pg_dump',[
  '--format=custom','--schema=public','--schema=private','--schema=auth','--schema=supabase_migrations',
  '--file',file],{stdio:'inherit',env:process.env});
if(dump.status!==0) process.exit(dump.status??1);
const verify=spawnSync('/opt/homebrew/opt/libpq/bin/pg_restore',['--file=/dev/null',file],{stdio:'inherit'});
if(verify.status!==0) process.exit(verify.status??1);
console.log(JSON.stringify({backup:file,bytes:fs.statSync(file).size,archive_read_verified:true}));
