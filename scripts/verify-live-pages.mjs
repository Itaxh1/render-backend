// Operator smoke test with an isolated browser and a temporary login for an
// EXISTING account. No email is sent, no account is created, no secrets printed.
// node --env-file=.env scripts/verify-live-pages.mjs existing@example.com
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { createClient } = createRequire(new URL('../../rexy/package.json',import.meta.url))('@supabase/supabase-js');
const { chromium } = await import(process.env.QA_PLAYWRIGHT_MODULE || 'playwright');
const email = process.argv[2];
assert.ok(email, 'Existing account email required');
const api = 'https://rexy-api.baememory.com';
const web = 'https://rexy.baememory.com';
const url = process.env.SUPABASE_URL;
const publicKey = process.env.SUPABASE_PUBLISHABLE_KEY;
const admin = createClient(url,process.env.SUPABASE_SECRET_KEY,{auth:{persistSession:false,autoRefreshToken:false}});
const {data:users,error:listError} = await admin.auth.admin.listUsers({page:1,perPage:1000});
assert.ok(!listError && users.users.some(u=>u.email===email),'Target must already exist');
const {data:link,error:linkError}=await admin.auth.admin.generateLink({type:'magiclink',email});
assert.ok(!linkError,'Could not generate operator test login');
const auth=createClient(url,publicKey,{auth:{persistSession:false,autoRefreshToken:false}});
const {data:login,error:loginError}=await auth.auth.verifyOtp({type:'magiclink',token_hash:link.properties.hashed_token});
assert.ok(!loginError && login.session,'Could not verify test login');
const session=login.session;
let browser;
try {
  const headers={authorization:`Bearer ${session.access_token}`};
  const timing={};
  let profile,projects;
  for (const route of ['profile','projects']) {
    timing[route]=[];
    for(let i=0;i<5;i++) {
      const start=performance.now();
      const response=await fetch(`${api}/v1/${route}?tz=America%2FPhoenix`,{headers});
      assert.equal(response.status,200,`${route} HTTP status`);
      const data=await response.json();
      timing[route].push(Math.round(performance.now()-start));
      if(route==='profile') profile=data; else projects=data;
    }
  }
  assert.ok(profile.profile && projects.total>0,'Expected saved real account data');
  const generated=projects.projects.find(p=>p.hasDocs);
  assert.ok(generated,'Expected the real Grok document saved by verify-insights');
  const docsResponse=await fetch(`${api}/v1/projects/${generated.id}/docs`,{headers});
  const docs=await docsResponse.json();
  assert.equal(docs.state,'ready'); assert.ok(docs.docs.projectMd && docs.docs.skillMd);
  assert.equal((await fetch(`${api}/v1/profile`)).status,401);
  console.log(JSON.stringify({live_api_ms:timing,authenticated_profile:true,project_count:projects.total,saved_grok_docs:true,anonymous_denied:true}));
  browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'America/Phoenix'});
  await context.addInitScript(({url,session,publicKey,api})=>{
    localStorage.setItem(`sb-${new URL(url).hostname.split('.')[0]}-auth-token`,JSON.stringify(session));
    localStorage.setItem(`rexy:public-auth-config:${api}`,JSON.stringify({url,key:publicKey,saved:Date.now()}));
  },{url,session,publicKey,api});
  const page=await context.newPage();
  const errors=[],modelRequests=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/docs/generate')) modelRequests.push(r.url());});
  await page.goto(`${web}/?view=profile`);
  await page.locator('.pf-totals').waitFor({timeout:30_000});
  const totals=await page.locator('.pf-totals').textContent();
  assert.ok(totals.includes(profile.profile.toolCalls.toLocaleString('en-US')),'UI tool count must match backend');
  await page.getByRole('button',{name:'Projects',exact:true}).click();
  await page.locator('.pj-pitem').first().waitFor({timeout:30_000});
  assert.equal(await page.locator('.pj-pitem').count(),projects.total);
  await page.locator('.pj-pitem').filter({has:page.locator('.pj-pname',{hasText:generated.name})}).first().click();
  await page.getByRole('tab',{name:'SKILL.md',exact:true}).waitFor({timeout:30_000});
  await page.getByRole('tab',{name:'SKILL.md',exact:true}).click();
  await page.getByRole('button',{name:'Source',exact:true}).click();
  assert.equal(await page.locator('.pj-source').textContent(),docs.docs.skillMd);
  const navigation=[];
  for(let i=0;i<10;i++) {
    const name=i%2===0?'Profile':'Projects';
    const selector=name==='Profile'?'.pf-totals':'.pj-pitem';
    navigation.push(await page.evaluate(async({name,selector})=>{
      const start=performance.now();
      [...document.querySelectorAll('.nav button')].find(b=>b.textContent===name).click();
      await new Promise((resolve,reject)=>{
        let frames=0;
        function tick(){if(document.querySelector(selector)) requestAnimationFrame(resolve); else if(++frames>300) reject(Error('Cached view did not paint')); else requestAnimationFrame(tick);}
        requestAnimationFrame(tick);
      });
      return Math.round((performance.now()-start)*10)/10;
    },{name,selector}));
  }
  // Unavailable API must not blank saved content on a reload.
  await context.route(`${api}/v1/**`,route=>route.abort());
  await page.reload();
  await page.locator('.pj-pitem').first().waitFor({timeout:5000});
  await page.getByRole('button',{name:'Profile',exact:true}).click();
  await page.locator('.pf-totals').waitFor({timeout:5000});
  await page.setViewportSize({width:390,height:900});
  assert.equal(await page.locator('main').evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
  await page.screenshot({path:'/private/tmp/rexy-live-profile-mobile.png',fullPage:true});
  assert.equal(errors.length,0,errors.join('; '));
  assert.equal(modelRequests.length,0,'Opening pages must never call Grok');
  console.log(JSON.stringify({cached_navigation_ms:navigation,offline_refresh:true,ui_api_count_parity:true,exact_saved_skill_display:true,model_calls_on_load:0,page_errors:0}));
} finally {
  await browser?.close();
  await auth.auth.signOut({scope:'local'}); // Only the temporary QA session.
}
