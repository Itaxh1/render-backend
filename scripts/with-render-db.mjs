// Operator-only helper: credentials stay in memory/child environment, never argv.
import fs from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
const cli = fs.readFileSync(join(homedir(), '.render/cli.yaml'), 'utf8');
const key = cli.match(/^\s+key:\s*(.+)$/m)?.[1].trim().replace(/^"|"$/g, '');
if (!key) throw new Error('Render CLI login required');
const response = await fetch('https://api.render.com/v1/services/srv-dafvcg9t0dsc73fujsgg/env-vars', {headers:{authorization:`Bearer ${key}`}});
if (!response.ok) throw new Error(`Render configuration unavailable (${response.status})`);
const vars = await response.json();
const database = vars.find(v => v.envVar.key === 'DATABASE_URL')?.envVar.value;
if (!database) throw new Error('Production database is not configured');
const [command, ...args] = process.argv.slice(2);
if (!command) throw new Error('A command is required');
const uri = new URL(database);
const child = spawn(command,args,{shell:false,stdio:'inherit',env:{...process.env,DATABASE_URL:database,
  PGHOST:uri.hostname,PGPORT:uri.port || '5432',PGUSER:decodeURIComponent(uri.username),
  PGPASSWORD:decodeURIComponent(uri.password),PGDATABASE:uri.pathname.slice(1),
  PGSSLMODE:uri.searchParams.get('sslmode') || 'require'}});
child.on('exit',code => process.exit(code ?? 1));
