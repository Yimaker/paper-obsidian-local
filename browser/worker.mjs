// Local UI gateway. The model never receives MCP tool/code or browser storage access.
import { createConnection } from '@playwright/mcp';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { chromium } from 'playwright-core';
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';

const opts = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const allowed = u => {
  try { const v = new URL(u); return v.protocol === 'https:' && !v.username && !v.password && (!v.port || v.port==='443') &&
    (v.hostname === 'cnki.net' || v.hostname.endsWith('.cnki.net') ||
     ['lib.suat-sz.edu.cn', 'idp.suat-sz.edu.cn', 'sso.suat-sz.edu.cn'].includes(v.hostname)); } catch { return false; }
};
const safeURL = value => {
  const u = new URL(value);
  if (!allowed(value)) return '';
  const q = new URLSearchParams();
  for (const k of ['dbcode','dbname','filename','doi']) if (u.searchParams.has(k)) q.set(k,u.searchParams.get(k));
  return u.origin + u.pathname + (q.size ? '?' + q : '');
};
const scrub = s => String(s).replace(/(?:sk-[A-Za-z0-9_-]+|Bearer\s+\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]+)/gi, '[redacted]')
  .replace(/(?:cookie|token|password|密码|口令|authorization)\s*[:=]\s*[^\s,;]+/gi, '[redacted]');
let context, server, client, controls = [], blocked = '', blockedOrigin='', downloads = [], mode = 'metadata';
let steps = 0, exports = opts.exports || 0, fulltexts = opts.fulltexts || 0;
let endnoteSelected=false;
const pending = new Set();
async function start() {
  context = await chromium.launchPersistentContext(opts.profile, {
    channel:'msedge', headless:false, chromiumSandbox:true, acceptDownloads:true, serviceWorkers:'block',
    viewport:{width:1280,height:850}
  });
  await context.route('**/*', async route => {
    const request=route.request();
    if (!allowed(request.url())) {
      if (request.isNavigationRequest()) { blocked='domain_blocked'; blockedOrigin=new URL(request.url()).origin; }
      return route.abort();
    }
    return route.continue();
  });
  await context.routeWebSocket(/.*/, ws => ws.close());
  const setup = page => {
    // Disable MCP console collection before it subscribes. Console output and
    // error URLs are unnecessary page data and may contain authentication data.
    const on=page.on.bind(page);
    page.on=(event,listener)=>['console','pageerror'].includes(event)?page:on(event,listener);
    page.consoleMessages=async()=>[];
    page.pageErrors=async()=>[];
    page.on('dialog', d => d.dismiss().catch(()=>{}));
    page.on('download', d => {
      const task=(async()=>{
      try {
        const filename = path.basename(d.suggestedFilename()).replace(/[<>:"/\\|?*]/g,'_');
        const ext = path.extname(filename).toLowerCase();
        const sourceOK=allowed(d.url()) || (d.url().startsWith('blob:') && allowed(d.url().slice(5)) && allowed(page.url()));
        if (!sourceOK || (mode==='metadata' ? !['.enw','.txt','.net'].includes(ext) : !['.pdf','.caj'].includes(ext))) {
          await d.cancel(); blocked='unexpected_download'; return;
        }
        if (mode==='metadata' ? ++exports>1 : ++fulltexts>1) { await d.cancel(); blocked='download_limit'; return; }
        const target=path.join(opts.downloads,`${Date.now()}-${filename}`);
        await d.saveAs(target);
        const stat=await fs.stat(target);
        if (stat.size>opts.max_bytes) { blocked='download_size_limit'; return; }
        downloads.push({path:target, filename, bytes:stat.size, source_url:safeURL(page.url()),
          downloaded_at:new Date().toISOString(), kind:mode});
        const manifest=path.join(opts.downloads,'manifest.json');
        const old=JSON.parse(await fs.readFile(manifest,'utf8').catch(()=>'[]'));
        await fs.writeFile(manifest+'.tmp',JSON.stringify([...old,downloads.at(-1)]));
        await fs.rename(manifest+'.tmp',manifest);
      } catch { blocked='download_failed'; }
      })();
      pending.add(task); task.finally(()=>pending.delete(task));
    });
  };
  context.pages().forEach(setup); context.on('page',setup);
  context.on('close',()=>process.exit(0));
  server=await createConnection({snapshot:{mode:'none'}, saveSession:false, webmcp:false,
    outputDir:opts.downloads, timeouts:{action:8000,navigation:30000}}, async()=>context);
  const [a,b]=InMemoryTransport.createLinkedPair();
  await server.connect(a); client=new Client({name:'paper-cnki-local',version:'1.0.0'});
  await client.connect(b);
}
async function observe() {
  await Promise.all([...pending]);
  const pages=context.pages().filter(p=>!p.isClosed());
  const page=pages.at(-1) || await context.newPage();
  if (page.url()==='about:blank') return {url:'', text:'', controls:[],downloads};
  if (!allowed(page.url())) return {blocked:'domain_blocked',blocked_origin:blockedOrigin||new URL(page.url()).origin,url:'',controls:[],downloads};
  const result=await page.evaluate(() => {
    const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility!=='hidden';
    // innerText omits hidden menus and input values. Never inspect input.value.
    const text=(document.body?.innerText||'').replace(/\s+/g,' ').slice(0,5000);
    const cs=[];
    for (const el of document.querySelectorAll('a,button,input,textarea,select,[role="button"],[onclick],[title],span,div')) {
      if (!visible(el) || el.disabled) continue;
      const tag=el.tagName.toLowerCase();
      const type=(el.getAttribute('type')||'').toLowerCase();
      const label=(el.innerText||el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.getAttribute('title')||
        el.querySelector('img')?.getAttribute('alt')||
        (tag==='input' && ['button','submit'].includes(type)?el.getAttribute('value'):'')||'').trim().slice(0,180);
      if (['span','div'].includes(tag) && (!/^(引用|导出|更多引用格式|EndNote|检索|搜索|PDF下载|CAJ下载)$/.test(label.replace(/\s/g,'')) || getComputedStyle(el).cursor!=='pointer')) continue;
      const search=['input','textarea'].includes(tag) && ['text','search',''].includes(type) &&
        /search|keyword|检索|搜索|主题|篇名/i.test([el.id,el.name,label].join(' '));
      if (/登录|登陆|注册|支付|购买|充值|协议|同意|咨询|验证码|认证|logout|login|扫码|批量|全选|整刊|整期|我是作者|CAJViewer|下载中心/i.test(label)) continue;
      if (['input','textarea'].includes(tag) && !search && !['button','submit'].includes(type)) continue;
      if (!label && !search) continue;
      if (tag==='a' && !/^https?:/.test(el.href)) {
        // CNKI UI buttons also use javascript links; allowed only by their label.
        if (!/检索|搜索|引用|导出|EndNote|PDF|CAJ|下载|更多引用格式/i.test(label)) continue;
      }
      const ui=/检索|搜索|引用|导出|EndNote|PDF|CAJ|下载|更多引用格式|学术期刊|篇名|主题|下一页/i.test(label.replace(/\s/g,''));
      if (!search && tag!=='select' && !ui && !(tag==='a' && /\/(doi|kcms|kcms2|kns)\//i.test(el.href))) continue;
      const ref='r'+cs.length; el.setAttribute('data-paper-ref',ref);
      cs.push({ref,label,kind:search?'search':tag==='select'?'select':'click',
        paper_link:tag==='a' && /\/article\/abstract|\/doi\//.test(el.href),
        download:/PDF|CAJ|全文下载|下载全文/i.test(label),
        options:tag==='select'?Array.from(el.options).map(o=>({label:o.label,value:o.value})):undefined});
      if (cs.length>=40) break;
    }
    const login=Array.from(document.querySelectorAll('input[type=password]')).some(visible);
    const challenge=Array.from(document.querySelectorAll('iframe')).some(e=>visible(e)&&/captcha|verify|验证码/i.test(e.title+' '+e.src));
    const header=Array.from(document.querySelectorAll('header,.header,#header,.org-name,#orgname,#userInfo')).filter(visible).map(e=>e.innerText).join(' ').slice(0,1500);
    const institution=['深圳理工大学','深圳大学','深圳技术大学'].find(n=>header.includes(n)) || '';
    return {text,controls:cs,login,challenge,institution};
  });
  result.text=scrub(result.text); result.controls=result.controls.map(c=>({...c,label:scrub(c.label)}));
  controls=result.controls;
  return {...result,url:safeURL(page.url()),blocked,blocked_origin:blockedOrigin,endnote_selected:endnoteSelected,downloads:[...downloads]};
}
async function execute(a) {
  if (++steps>opts.max_steps) throw Error('step_limit');
  if (a.action==='observe') return observe();
  if (a.action==='navigate') {
    if (!allowed(a.url)) throw Error('domain_blocked');
    const result=await client.callTool({name:'browser_navigate',arguments:{url:a.url}});
    if(result.isError) {
      const code=JSON.stringify(result).match(/net::ERR_[A-Z_]+/)?.[0] || 'navigation_failed';
      return {url:safeURL(a.url),text:'',controls:[],navigation_error:code,blocked,blocked_origin:blockedOrigin,downloads:[...downloads]};
    }
  } else if (['click','type','submit','select'].includes(a.action)) {
    const c=controls.find(c=>c.ref===a.ref);
    if (!c) throw Error('stale_ref');
    if (/^导出$/.test(c.label) && !endnoteSelected) throw Error('select_endnote_required');
    if (c.download && !opts.download_one) throw Error('permission_unconfirmed');
    if (c.download && fulltexts>=1) throw Error('download_limit');
    if (['type','submit'].includes(a.action) && c.kind!=='search') throw Error('input_forbidden');
    if (a.action==='select' && (c.kind!=='select' || !c.options.some(o=>o.value===a.value && /EndNote/i.test(o.label)))) throw Error('select_forbidden');
    mode=c.download?'fulltext':'metadata';
    const selector=JSON.stringify(`[data-paper-ref="${c.ref}"]`);
    let operation=['type','submit'].includes(a.action)?`fill(${JSON.stringify(opts.query)})`:
      a.action==='select'?`selectOption(${JSON.stringify(a.value)})`:'click()';
    const submit=a.action==='submit'?`await target.locator(${selector}).press('Enter');`:'';
    const r=await client.callTool({name:'browser_run_code_unsafe',arguments:{code:`async (page) => { const target=page.context().pages().at(-1); await target.locator(${selector}).${operation}; ${submit} }`}});
    if (r.isError) throw Error('ui_action_failed');
    if (/^EndNote$/i.test(c.label) || a.action==='select') endnoteSelected=true;
  } else throw Error('action_forbidden');
  // Only wait for a finite browser load state, never poll an internal endpoint.
  await context.pages().at(-1)?.waitForLoadState('domcontentloaded',{timeout:5000}).catch(()=>{});
  return observe();
}
try {
  await start();
  const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  for await (const line of lines) {
    try {
      const action=JSON.parse(line);
      if(action.action==='close') break;
      process.stdout.write(JSON.stringify({ok:true,view:await execute(action)})+'\n');
    } catch(e) { process.stdout.write(JSON.stringify({ok:false,error:/^[a-z_]+$/.test(e.message)?e.message:'browser_error'})+'\n'); }
  }
} finally { await client?.close().catch(()=>{}); await server?.close().catch(()=>{}); await context?.close().catch(()=>{}); }
