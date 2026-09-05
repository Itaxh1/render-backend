// Test-only dependency substitution: never alter HOME or scan real transcripts.
import os from 'node:os';
import { syncBuiltinESMExports } from 'node:module';
if (!process.env.REXY_SMOKE_TRANSCRIPTS) throw new Error('Smoke fixture path is missing');
os.homedir = () => process.env.REXY_SMOKE_TRANSCRIPTS;
syncBuiltinESMExports();
