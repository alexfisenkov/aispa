// Black-box tests of the delivered inline script, without an extra DOM dependency.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../skills/aispa/assets/dashboard.html'),'utf8');
const script=html.match(/<script id="aispa-app">([\s\S]*?)<\/script>/)[1];

class Element {
 constructor(tag='div',text=''){this.tag=tag;this._text=text;this.children=[];this.style={};this.events={};this.attributes={};this.className='';this.hidden=false;this.value='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this._text='';this.children=[...items];}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
 set innerHTML(value){throw new Error('Unsafe HTML assignment');}
 addEventListener(name,fn){this.events[name]=fn;}
 setAttribute(name,value){this.attributes[name]=value;}
 getAttribute(name){return this.attributes[name];}
 focus(options){this.ownerDocument.activeElement=this;this.focusOptions=options;}
 click(){return this.events.click?.();}
 change(value){this.value=value;return this.events.change?.call(this);}
}
function descend(el,id){if(el.id===id)return el;for(const child of el.children){if(typeof child==='object'){const found=descend(child,id);if(found)return found;}}return null;}
async function fixture(search='',data=sample(),options={}){
 const elements={};for(const match of html.matchAll(/\bid="([^"]+)"/g)){elements[match[1]]=new Element();elements[match[1]].id=match[1];}
 const staticEls=[];for(const match of html.matchAll(/<[^>]*\bdata-i18n="([^"]+)"[^>]*>/g)){
  const el=new Element();el.setAttribute('data-i18n',match[1]);staticEls.push(el);
 }
 const responseStatus=data=>data?.calculation?.state==='calculating'?202:data?.calculation?.state==='source_error'?409:200;
 const intervals=[],timeouts=new Map(),requests=[];let response=data,error=null,clock=0,calls=0,timerId=0,status=responseStatus(data),holdNext=false,release=null,activeRequests=0,maxActiveRequests=0;
 const listeners={};
 const document={getElementById(id){return elements[id]||Object.values(elements).map(e=>descend(e,id)).find(Boolean)||null;},
  createElement(tag){const el=new Element(tag);el.ownerDocument=document;return el;},createTextNode(text){return new Element('#text',String(text));},
  documentElement:new Element('html'),hidden:false,addEventListener(name,fn){(listeners[name]=listeners[name]||[]).push(fn);},
  querySelectorAll(selector){return selector==='[data-i18n]'?staticEls:[];}};
 const context={document,location:{search},URLSearchParams,Date,Set,Array,Number,AbortController,
  performance:{now:()=>clock},setInterval:(fn,delay)=>{intervals.push({fn,name:fn.name,delay,cleared:false});return intervals.length;},
  clearInterval:id=>{if(intervals[id-1])intervals[id-1].cleared=true;},
  setTimeout:(fn,delay)=>{const id=++timerId;timeouts.set(id,{fn,delay,due:clock+delay});return id;},clearTimeout:id=>timeouts.delete(id),
  window:{AISPA_ARCHIVE:options.archive,matchMedia:()=>({matches:!!options.systemDark,addEventListener(){}})},
  fetch:async(url)=>{calls++;requests.push(url);activeRequests++;maxActiveRequests=Math.max(maxActiveRequests,activeRequests);
   try{if(holdNext){holdNext=false;await new Promise(resolve=>{release=resolve;});}if(error)throw Error('network');
    return {ok:status>=200&&status<300,status,json:async()=>response};}finally{activeRequests--;}}};
 if(options.storage)context.localStorage=options.storage;
 vm.runInNewContext(script,context);
 async function settle(){for(let i=0;i<8;i++)await Promise.resolve();}
 await settle();
 return {el:id=>document.getElementById(id),text:id=>document.getElementById(id).textContent,
  staticText:key=>staticEls.find(el=>el.getAttribute('data-i18n')===key)?.textContent,
  htmlElement:document.documentElement,activeElement:()=>document.activeElement,
  async update(data){response=data;error=null;status=responseStatus(data);await elements.refresh.click();await settle();},
  async fail(){error=true;await elements.refresh.click();await settle();},
  advance(ms){clock+=ms;intervals.forEach(entry=>{if(entry.name==='tick'&&!entry.cleared)entry.fn();});},
  async setHidden(value){document.hidden=value;(listeners.visibilitychange||[]).forEach(fn=>fn());await settle();},
  fetchCount:()=>calls,intervalCount:()=>intervals.filter(entry=>!entry.cleared).length,
  requests:()=>requests.slice(),maxActiveRequests:()=>maxActiveRequests,activeRequests:()=>activeRequests,
  reply(data,code=200){response=data;status=code;error=null;},holdNext(){holdNext=true;},
  disconnect(){error=true;},async release(){assert.ok(release,'No held request to release');release();release=null;await settle();},
  timerDelays:()=>[...timeouts.values()].map(timer=>timer.delay),timerCount:()=>timeouts.size,
  refreshIntervals:()=>intervals.filter(entry=>!entry.cleared&&entry.name!=='tick').length,
  tickIntervals:()=>intervals.filter(entry=>!entry.cleared&&entry.name==='tick').length,
  async nextTimer(){const next=[...timeouts.entries()].sort((a,b)=>a[1].due-b[1].due)[0];
   assert.ok(next,'Pending generation must schedule an addressed fast poll');timeouts.delete(next[0]);clock=next[1].due;next[1].fn();await settle();},
  settle};
}
function sample(status='running'){
 return {version:'0.1.0-alpha.2',status,title:'Тестовый проект',stage:'build',displayStage:'build',
  observedAt:'2026-10-19T12:00:00Z',updatedAt:'2026-10-19T11:59:59Z',elapsedSeconds:120,
  gates:{coverage:true,plan:true,acceptance:status==='accepted'},remainingAssessed:status==='accepted',
  requirements:[{id:'R01',text:'Отправка формы',scope:'active',kind:'requested',core:true,implementation:'unfinished'},
   {id:'R02',text:'Экспорт',scope:'deferred',kind:'requested',core:false,implementation:'unplanned',decision:{quote:'Экспорт отложить'}},
   {id:'A01',text:'Черновик',scope:'active',kind:'addition',parent:'R01',implementation:'unplanned'}],
  tasks:[{id:'T01',title:'Форма',wave:1,requirements:['R01'],dependsOn:[],status:'running',elapsedSeconds:30,activeSince:'2026-10-19T11:59:30Z'},
   {id:'T02',title:'Черновик',wave:2,requirements:['A01'],dependsOn:['T01'],status:'pending',elapsedSeconds:null}],
  remaining:{checks:[],blockedTasks:[],excluded:[],unmapped:[]},decisions:[],
  estimate:{reason:'insufficient_data',sampleCount:0},activity:null};
}
function archivedSample(status='accepted'){
 const d=sample(status);
 d.archive=true;d.run='fixture';d.archivedAt='2026-10-19T12:00:01Z';d.verified=status==='accepted'||status==='delivered_with_remaining';
 d.events=[];d.remaining.inherited=[];
 d.requirements.forEach(r=>{r.core=!!r.core});
 d.tasks.forEach(t=>{t.proofCurrent=false});
 if(status==='accepted'){
  d.requirements=[{id:'R01',text:'Отправка формы',scope:'active',kind:'requested',core:true,implementation:'implemented'}];
  d.tasks=[{id:'T01',title:'Форма',wave:1,requirements:['R01'],dependsOn:[],status:'completed',
   proofCurrent:true,elapsedSeconds:30}];
  d.gates={coverage:true,plan:true,acceptance:true};d.remainingAssessed=true;
 }
 return d;
}

function pendingSnapshot(last=null,state='calculating'){
 return {calculation:{id:'fixture-generation',state,startedAt:'2026-10-26T01:00:00Z'},
  lastSnapshot:last?{view:last,capturedAt:'2026-10-26T00:59:00Z',basis:{journalSha256:'a'.repeat(64)},freshness:'historical'}:null,
  ...(state==='source_error'?{error:{code:'snapshot_source_error'}}:{})};
}
function descendants(element){return [element,...element.children.filter(x=>typeof x==='object').flatMap(descendants)];}
test('pending generation fast polls its addressed ID and becomes current without another generation',async()=>{
 const d=sample('accepted'),f=await fixture('',pendingSnapshot(d));
 assert.equal(f.refreshIntervals(),0,'A fixed refresh interval still starts polling independently of the request lifecycle');
 assert.equal(f.timerCount(),1,'One pending generation must own exactly one next poll');
 assert.ok(f.timerDelays()[0]>0&&f.timerDelays()[0]<=1000,'Pending generation did not receive a bounded fast first poll');
 f.reply({...d,calculation:{id:'fixture-generation',state:'ready',startedAt:'2026-10-26T01:00:00Z'},
  snapshot:{capturedAt:'2026-10-26T01:00:02Z',freshness:'current',basis:{journalSha256:'b'.repeat(64)}}});
 await f.nextTimer();
 assert.equal(f.requests()[1],'status.json?calculation=fixture-generation');
 assert.match(f.text('sourceLabel'),/Живые данные/);assert.match(f.text('label'),/приняты/);
 assert.equal(f.timerCount(),1,'Ready transition accumulated refresh timers');
 f.reply(d);await f.nextTimer();assert.equal(f.requests()[2],'status.json');
});
test('repeated pending backs off within bounds and keeps one poll and one request',async()=>{
 const d=sample(),f=await fixture('',pendingSnapshot(d)),delays=[];
 for(let i=0;i<9;i++){
  assert.equal(f.timerCount(),1,'Pending polling accumulated timers');
  const delay=f.timerDelays()[0];delays.push(delay);
  assert.ok(delay>=100&&delay<=10000,'Pending backoff escaped its resource bounds');
  await f.nextTimer();assert.equal(f.requests().at(-1),'status.json?calculation=fixture-generation');
 }
 assert.ok(delays.some(delay=>delay>delays[0]),'Long calculations never backed off from fast polling');
 assert.ok(delays.every((delay,index)=>index===0||delay>=delays[index-1]),'Pending backoff reset without a new generation');
 f.holdNext();await f.nextTimer();assert.equal(f.activeRequests(),1);
 const before=f.fetchCount();
 for(let i=0;i<8;i++)await f.el('refresh').click();
 assert.equal(f.fetchCount(),before,'Manual refresh overlapped the pending request');
 assert.equal(f.maxActiveRequests(),1);assert.equal(f.timerCount(),1,'In-flight request retained extra polling timers');
 f.reply(d);await f.release();assert.equal(f.timerCount(),1);assert.equal(f.maxActiveRequests(),1);
});
test('expired generations restart normally and source errors reconnect without stale current PASS',async()=>{
 const d=sample('accepted'),f=await fixture('',pendingSnapshot(d));
 f.reply({error:{code:'snapshot_generation_expired'}},410);await f.nextTimer();
 assert.match(f.text('sourceLabel'),/ошибка источника/i);assert.doesNotMatch(f.text('gates'),/✓/);
 assert.equal(f.timerCount(),1);f.reply(pendingSnapshot(d));await f.nextTimer();
 assert.equal(f.requests().at(-1),'status.json','Expired generation ID leaked into a new calculation');
 f.reply(pendingSnapshot(d,'source_error'),409);await f.nextTimer();
 assert.match(f.text('sourceLabel'),/ошибка источника/i);assert.doesNotMatch(f.text('label'),/приняты/);
 assert.equal(f.timerCount(),1);f.disconnect();await f.nextTimer();
 assert.match(f.text('sourceLabel'),/нет связи/i);assert.equal(f.timerCount(),1);
 assert.doesNotMatch(f.text('gates'),/✓/);f.reply(d);await f.nextTimer();
 assert.equal(f.requests().at(-1),'status.json');assert.match(f.text('sourceLabel'),/Живые данные/);
 assert.match(f.text('label'),/приняты/);assert.equal(f.maxActiveRequests(),1);
});
test('retained deferred and running lifecycle stays visible in RU EN historical task summaries',async()=>{
 const d=sample();Object.assign(d.tasks[0],{status:'blocked',deferred:true,phaseStale:true,phase:'blocked'});
 Object.assign(d.tasks[1],{status:'running',phase:'implementation',phaseLabel:'Реализация'});
 for(const state of ['calculating','source_error','disconnected']){
  const f=await fixture('',d);
  if(state==='disconnected')await f.fail();else await f.update(pendingSnapshot(d,state));
  assert.match(f.text('task-summary-T01'),/Отложено в остатки/,'Historical freshness erased the known deferred lifecycle');
  assert.match(f.text('task-summary-T02'),/Реализация/,'Historical freshness erased the known running phase');
  assert.match(f.text('task-summary-T01'),/последний снимок/);
  f.el('language').change('en');
  assert.match(f.text('task-summary-T01'),/Deferred to remaining/);assert.match(f.text('task-summary-T02'),/Implementation/);
  assert.match(f.text('task-summary-T01'),/snapshot/);assert.doesNotMatch(f.text('gates'),/✓/);
 }
});
test('ETA keeps normal word boundaries at narrow widths in the delivered CSS',()=>{
 const css=html.match(/<style>([\s\S]*?)<\/style>/)[1],effective={};let order=0;
 for(const rule of css.matchAll(/([^{}]+)\{([^{}]+)\}/g)){
  for(const selector of rule[1].split(',').map(value=>value.trim())){
   if(!['#eta','.time-summary #eta','.time-summary strong','strong'].includes(selector))continue;
   const specificity=(selector.match(/#/g)||[]).length*100+(selector.match(/\./g)||[]).length*10+(selector.includes('strong')?1:0);
   for(const declaration of rule[2].split(';')){const colon=declaration.indexOf(':');if(colon<0)continue;
    const property=declaration.slice(0,colon).trim(),value=declaration.slice(colon+1).trim(),previous=effective[property];
    if(!previous||specificity>=previous.specificity)effective[property]={value,specificity,order:order++};}
  }
 }
 assert.equal(effective['overflow-wrap']?.value||'normal','normal','ETA can split ordinary words at arbitrary characters');
 assert.equal(effective['word-break']?.value||'normal','normal');
 assert.equal(effective['white-space']?.value||'normal','normal');
});
test('first calculation is observable without fabricated snapshot or PASS',async()=>{
 const f=await fixture('',pendingSnapshot());
 assert.match(f.text('sourceLabel'),/считается/i);
 assert.doesNotMatch(f.text('label'),/приняты|готов/i);
 assert.equal(f.text('runElapsed'),'—');assert.doesNotMatch(f.text('gates'),/✓/);
 f.el('language').change('en');assert.match(f.text('sourceLabel'),/calculating/i);
});
test('calculating retains a historical snapshot without current acceptance or ticking clocks',async()=>{
 const d=sample('accepted'),f=await fixture('',d);
 assert.match(f.text('label'),/приняты/);
 await f.update(pendingSnapshot(d));
 assert.match(f.text('sourceLabel'),/считается/i);
 assert.equal(f.text('title'),d.title);assert.doesNotMatch(f.text('label'),/приняты/);
 assert.doesNotMatch(f.text('gates'),/✓/);
 const before=f.text('time-T01');f.advance(6000);assert.equal(f.text('time-T01'),before);
 assert.match(f.text('refreshText'),/26\.10\.2026.*08:59|2026-10-26T00:59:00Z|26\.10\.2026.*00:59/);
});
test('source failure and disconnected transport are distinct until a verified reconnect',async()=>{
 const d=sample('accepted'),f=await fixture('',d);
 await f.update(pendingSnapshot(d,'source_error'));
 assert.match(f.text('sourceLabel'),/ошибка источника/i);
 assert.doesNotMatch(f.text('gates'),/✓/);
 await f.fail();assert.match(f.text('sourceLabel'),/нет связи/i);
 assert.doesNotMatch(f.text('label'),/приняты/);
 await f.update(d);assert.match(f.text('sourceLabel'),/Живые данные/);assert.match(f.text('label'),/приняты/);
});
test('descendant-confirmed result links to confirmer while old phase receipts stay historical',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=true;d.tasks[0].phaseStale=true;
 d.tasks[0].proofProvenance={kind:'descendant',task:'T02',attempt:'descendant-attempt',recordedAt:'2026-10-26T01:00:00Z'};
 d.tasks[0].phaseSteps=[{id:'critic_1',label:'Критик · круг 1',status:'done',historical:true,
  basis:'old-basis',basisCurrent:false,recordedAt:'2026-10-19T12:00:00Z'}];
 d.tasks[1].status='completed';d.tasks[1].proofCurrent=true;
 const f=await fixture('',d);
 assert.match(f.text('waves'),/Результат подтверждён последующей задачей/);
 assert.doesNotMatch(f.el('task-T01').className,/proof-stale/);
 const nodes=descendants(f.el('task-T01'));
 assert.ok(nodes.some(n=>n.tag==='a'&&n.attributes.href==='#task-T02'));
 assert.ok(nodes.some(n=>n.tag==='time'&&n.attributes.datetime==='2026-10-19T12:00:00Z'));
 assert.match(f.text('task-T01'),/Историческ/);
 f.el('language').change('en');assert.match(f.text('waves'),/Result confirmed by a later task/);
});
test('hero distinguishes calculating and source errors from disconnected transport in both languages',async()=>{
 const d=sample('accepted');
 for(const [state,ru,en] of [['calculating','Рассчитывается','Calculating'],['source_error','Ошибка источника','Source error'],['disconnected','Нет связи','Disconnected']]){
  const f=await fixture('',d);
  if(state==='disconnected')await f.fail();else await f.update(pendingSnapshot(d,state));
  assert.equal(f.text('statusShort'),ru,`Hero conflated ${state} with transport failure`);
  f.el('language').change('en');assert.equal(f.text('statusShort'),en);
  assert.doesNotMatch(f.text('label'),/Accepted|requirements accepted/);
 }
});
test('retained criterion outcomes are neutral history in calculating source_error and disconnected then current on reconnect',async()=>{
 const d=sample('accepted');d.tasks[0].status='completed';d.tasks[0].proofCurrent=true;
 d.tasks[0].criteriaDetails=[{text:'Saved exact criterion',status:'passed',source:'checked_report',checkedBy:'acceptor'}];
 function resultChip(f,text){return descendants(f.el('task-T01')).find(n=>n.className.startsWith('status-chip')&&text.test(n.textContent));}
 for(const state of ['calculating','source_error','disconnected']){
  const f=await fixture('',d);
  assert.equal(resultChip(f,/Пройдено/).className,'status-chip passed');
  if(state==='disconnected')await f.fail();else await f.update(pendingSnapshot(d,state));
  const historical=resultChip(f,/Пройдено/);
  assert.equal(historical.className,'status-chip historical',`Retained criterion still advertises current PASS in ${state}`);
  assert.match(historical.textContent,/последний снимок|на дату снимка/);
  f.el('language').change('en');
  const translated=resultChip(f,/Passed/);assert.equal(translated.className,'status-chip historical');assert.match(translated.textContent,/snapshot/);
  await f.update(d);assert.equal(resultChip(f,/Passed/).className,'status-chip passed');
 }
});
test('descendant criterion explains its real proof source and repair phase copy is translated',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=true;
 d.tasks[0].proofProvenance={kind:'descendant',task:'T02',attempt:'current-descendant'};
 d.tasks[0].criteriaDetails=[{text:'Ancestor criterion',status:'passed',source:'descendant_proof',checkedBy:'critic'}];
 const f=await fixture('',d);
 assert.match(f.text('task-T01'),/По подтверждению последующей задачей/);
 assert.doesNotMatch(f.text('task-T01'),/Отдельная проверка критерия ещё не записана/);
 f.el('language').change('en');assert.match(f.text('task-T01'),/Confirmed by a later task/);
 const repairing=sample();repairing.tasks[0].phase='repair';repairing.tasks[0].phaseLabel='Исправление';
 await f.update(repairing);assert.match(f.text('waves'),/Repair/);assert.doesNotMatch(f.text('waves'),/Исправление/);
});
test('logo, workshop label and AISpa title share an accessible external channel link',async()=>{
 const header=html.match(/<header>([\s\S]*?)<\/header>/)[1];
 const links=[...header.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/g)];
 assert.equal(links.length,1);
 const [,attributes,content]=links[0];
 const attrs=Object.fromEntries([...attributes.matchAll(/([\w-]+)="([^"]*)"/g)].map(m=>[m[1],m[2]]));
 assert.equal(attrs.href,'https://t.me/masterskaya_video');
 assert.equal(attrs.target,'_blank');
 assert.deepEqual(new Set(attrs.rel.split(/\s+/)),new Set(['noopener','noreferrer']));
 assert.equal(attrs.tabindex,undefined);
 assert.match(content,/<div class="logo"><svg\b[^>]*aria-hidden="true"/);
 assert.match(content,/<small>AI мастерская<\/small>/);
 assert.match(content,/<h1>AISpa<\/h1>/);
 assert.doesNotMatch(content,/\bid="version"/);
 assert.match(header,/<\/a>\s*<span class="pill" id="version">/);
 assert.match(html,/\.brand-link:focus-visible\{[^}]*outline:2px solid var\(--ink\)/);
 const f=await fixture();
 assert.match(f.staticText('workshopLink'),/Telegram-канал, новая вкладка/);
 f.el('language').change('en');
 assert.match(f.staticText('workshopLink'),/Telegram channel, new tab/);
});
test('live query cannot turn running data into accepted',async()=>{
 const f=await fixture('?status=accepted');assert.equal(f.text('label'),'В работе');assert.equal(f.fetchCount(),1);
});
test('demo is explicit and does not fetch live data',async()=>{
 const f=await fixture('?demo&status=accepted');assert.equal(f.fetchCount(),0);assert.match(f.text('sourceLabel'),/Демо/);assert.match(f.text('label'),/приняты/);
});
test('real stage build is mapped to the supplied stage strip',async()=>{
 const f=await fixture();assert.equal(f.text('stageName'),'Задачи и волны');assert.match(f.text('stageText'),/6 из 7/);
});
test('current task and elapsed time update independently of polling',async()=>{
 const f=await fixture();assert.match(f.text('currentActivity'),/T01/);assert.match(f.text('waves'),/Форма/);assert.equal(f.text('time-T01'),'30 с');
 f.advance(2000);assert.equal(f.text('time-T01'),'32 с');assert.equal(f.text('runElapsed'),'2 мин 02 с');
});
test('pause freezes task and run clocks',async()=>{
 const d=sample('paused');d.estimate={reason:'paused'};const f=await fixture('',d);f.advance(4000);
 assert.equal(f.text('time-T01'),'30 с');assert.equal(f.text('runElapsed'),'2 мин 00 с');assert.equal(f.text('eta'),'После возобновления');
});
test('insufficient samples do not produce an invented estimate',async()=>{
 const f=await fixture();assert.equal(f.text('eta'),'Пока мало данных');assert.match(f.text('etaNote'),/двух/);
});
test('a pending task is labelled not started rather than zero seconds',async()=>{
 const f=await fixture();assert.equal(f.text('time-T02'),'Ещё не начата');
});
test('estimate is an explicitly approximate interval',async()=>{
 const d=sample();d.estimate={reason:'estimate',sampleCount:3,lowerSeconds:600,upperSeconds:1200};const f=await fixture('',d);
 assert.equal(f.text('eta'),'≈ 10–20 мин');assert.match(f.text('etaNote'),/ориентир/);
});
test('actual remaining keys resolve blocker notes and owner decisions',async()=>{
 const d=sample();d.tasks[0].status='blocked';d.tasks[0].note='Нет тестового доступа';
 d.remaining={checks:[{requirement:'R01',criterion:'Уведомление',note:'Не отправлено'}],blockedTasks:['T01'],excluded:['R02']};
 const f=await fixture('',d);const text=f.text('remaining');assert.match(text,/Нет тестового доступа/);assert.match(text,/Экспорт отложить/);assert.match(text,/Уведомление — Не отправлено/);
});
test('running without recorded remains does not claim that none exist',async()=>{
 const f=await fixture();assert.match(f.text('remaining'),/ещё не подведены/);assert.doesNotMatch(f.text('remaining'),/Остатков нет/);
});
test('connection failure revokes accepted header and gate badges, even after filtering',async()=>{
 const f=await fixture('',sample('accepted'));assert.match(f.text('label'),/приняты/);await f.fail();
 assert.equal(f.text('label'),'Состояние не подтверждено');assert.doesNotMatch(f.text('gates'),/✓/);
 f.el('tabs').children[1].click();assert.equal(f.text('label'),'Состояние не подтверждено');
 assert.equal(f.text('eta'),'Не подтверждено');
});
test('stale connection freezes clocks instead of running forever',async()=>{
 const f=await fixture();f.advance(19000);assert.notEqual(f.text('label'),'Состояние не подтверждено','Offline came before 20 seconds without a reply');
 f.advance(2000);assert.equal(f.text('label'),'Состояние не подтверждено');assert.equal(f.text('time-T01'),'30 с');
});
test('filter buttons survive a refresh and keep the selected filter',async()=>{
 const f=await fixture();const button=f.el('tabs').children[2];button.click();await f.update(sample());
 assert.equal(f.el('tabs').children[2],button);assert.equal(button.attributes['aria-pressed'],'true');
 assert.match(f.text('requirements'),/Черновик/);assert.doesNotMatch(f.text('requirements'),/Отправка формы/);
});
test('user content is text and cannot become HTML',async()=>{
 const d=sample();d.title='<img src=x onerror=alert(1)>';d.requirements[0].text='<script>bad()</script>';
 const f=await fixture('',d);assert.equal(f.text('title'),d.title);assert.match(f.text('requirements'),/<script>bad\(\)<\/script>/);
});
test('unknown status is not passed off as a known result',async()=>{
 const f=await fixture('',sample('unsupported'));assert.equal(f.text('label'),'Состояние не определено');
});
test('current work names the recorded critic phase',async()=>{
 const d=sample();d.tasks[0].phase='critic2';d.tasks[0].phaseLabel='Второй критик';
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/Второй критик/);assert.match(f.text('waves'),/Второй критик/);
});
test('phase clocks advance with the task clock and freeze on pause or lost connection',async()=>{
 const d=sample();d.tasks[0].phaseTiming={phase:'repair',elapsedSeconds:8,activeSince:d.observedAt};
 const f=await fixture('',d);assert.equal(f.text('phase-time-T01'),'8 с');
 assert.match(f.text('currentActivity'),/Правка/);assert.match(f.text('waves'),/Вся задача/);
 f.advance(2000);assert.equal(f.text('phase-time-T01'),'10 с');assert.equal(f.text('phase-time-T01'),'10 с');
 const paused=sample('paused');paused.tasks[0].phaseTiming={phase:'repair',elapsedSeconds:8,activeSince:paused.observedAt};
 const p=await fixture('',paused);p.advance(2000);assert.equal(p.text('phase-time-T01'),'8 с');
 await f.fail();f.advance(2000);assert.equal(f.text('phase-time-T01'),'8 с');assert.match(f.text('waves'),/Фаза на момент снимка/);
});
test('phase row reflects supplied states: repair may be skipped, second review is done',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].phaseSteps=[
  {id:'implementation',label:'Реализация',status:'done'},
  {id:'critic_1',label:'Первый критик',status:'done'},
  {id:'repair',label:'Правка',status:'skipped'},
  {id:'critic_2',label:'Второй критик',status:'done'},
  {id:'task_acceptance',label:'Приёмка задачи',status:'done'}];
 const f=await fixture('',d);const row=f.el('task-phases-T01').children[0];
 assert.equal(row.attributes.role,'list');assert.equal(row.children.length,5);
 assert.equal(row.children[2].attributes['aria-label'],'Правка — не требуется');
 assert.equal(row.children[3].attributes['aria-label'],'Второй критик — завершено');
 assert.doesNotMatch(f.text('waves'),/Фаза: Правка/);
});
test('without backend phase data, the dashboard invents neither phase steps nor a phase clock',async()=>{
 const f=await fixture();assert.equal(f.el('phase-time-T01'),null);
 assert.doesNotMatch(f.text('waves'),/Первый критик|Второй критик|Приёмка задачи/);
 assert.equal(f.el('stats'),null);
});
test('completed task with stale proof is not labelled accepted',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=false;
 d.tasks[0].phaseSteps=[{id:'task_acceptance',label:'Приёмка задачи',status:'stale'}];
 const f=await fixture('',d);assert.match(f.text('waves'),/Проверку нужно обновить/);
 assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 0 \/ 1/);
 assert.doesNotMatch(f.text('waves'),/Задача принята/);
});
test('source change after first PASS hides superseded second review and its clock',async()=>{
 const d=sample();d.tasks[0].phaseStale=true;d.tasks[0].phaseLabel='Критик · круг 2';
 d.tasks[0].phaseTiming={phase:'critic_2',elapsedSeconds:18,activeSince:d.observedAt};
 d.tasks[0].phaseSteps=[{id:'critic_1',label:'Критик · круг 1',status:'stale'},
  {id:'repair',label:'Исправление',status:'skipped'},
  {id:'critic_2',label:'Критик · круг 2',status:'stale'}];
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/Проверки устарели/);
 assert.doesNotMatch(f.text('currentActivity'),/Сейчас: Критик/);
 assert.match(f.text('waves'),/Проверки устарели/);
 assert.equal(f.el('phase-time-T01'),null);assert.equal(f.el('active-phase-time-T01'),null);
 f.advance(2000);assert.equal(f.text('time-T01'),'32 с');
 await f.fail();assert.match(f.text('waves'),/Проверки устарели · последний снимок/);
});
test('demo illustrates repair and second review as examples only',async()=>{
 const f=await fixture('?demo');assert.equal(f.fetchCount(),0);
 assert.match(f.text('sourceLabel'),/Демо-данные/);
 assert.match(f.text('waves'),/Правка/);assert.match(f.text('waves'),/Второй критик/);
 assert.match(f.text('waves'),/Препятствие/);assert.match(f.text('waves'),/Ожидает/);
 assert.equal(f.text('phase-time-T03'),'1 мин 30 с');
 const t02=f.el('task-phases-T02').children[0];
 assert.equal(t02.children[2].attributes['aria-label'],'Правка — не требуется');
 assert.equal(t02.children[3].attributes['aria-label'],'Второй критик — не требуется');
 const accepted=await fixture('?demo&status=accepted');
 let skipped=0,reviewed=0;
 for(const wave of accepted.el('waves').children)for(const task of wave.children[3].children){
  const steps=accepted.el('task-phases-'+task.id.slice(5)).children[0];
  const repairSkipped=steps.children[2].attributes['aria-label']==='Правка — не требуется';
  assert.equal(steps.children[3].attributes['aria-label'],repairSkipped?'Второй критик — не требуется':'Второй критик — завершено');
  assert.equal(steps.children[4].attributes['aria-label'],'Приёмка задачи — завершено');
  if(repairSkipped)skipped++;else reviewed++;
 }
 assert.equal(skipped,3);assert.equal(reviewed,4);
});
test('approved brand geometry is inline and long titles can wrap on narrow screens',()=>{
 assert.match(html,/<svg[^>]*viewBox="0 0 256 256"[^>]*aria-hidden="true"[^>]*focusable="false"/);
 assert.match(html,/<path d="M122 211V84L173 159L224 84V211"/);
 assert.match(html,/<small>AI мастерская<\/small><h1>AISpa<\/h1>/);
 assert.equal((html.match(/<h1\b/g)||[]).length,1);
 assert.ok((html.match(/<h2\b/g)||[]).length>=6);
 assert.match(html,/h1,h2\{margin:0\}/);
 assert.match(html,/\.task-body b,.dec b,.rem b,.activity-title/);
 assert.match(html,/overflow-wrap:anywhere/);
 assert.match(html,/@media\(max-width:650px\)/);
});
test('stale task proof is visibly awaiting a new check',async()=>{
 const d=sample();d.requirements[0].implementation='needs_check';
 const f=await fixture('',d);assert.match(f.text('requirements'),/Нужна повторная проверка/);
});
test('inherited unfinished work is visible independently of current tasks',async()=>{
 const d=sample();d.remaining.inherited=[{id:'C-example',status:'outstanding',text:'Экспорт из прошлого прогона'}];
 const f=await fixture('',d);assert.match(f.text('remaining'),/Остатки прошлых прогонов/);assert.match(f.text('remaining'),/Экспорт из прошлого прогона/);
});
test('optional history is collapsed and renders event text safely',async()=>{
 const empty=await fixture();assert.equal(empty.el('historyPanel').hidden,true);
 const d=sample();d.events=[{type:'verification-executed',at:d.observedAt,text:'<img src=x onerror=bad()>'}];
 const f=await fixture('',d);assert.equal(f.el('historyPanel').hidden,false);
 assert.match(html,/<details class="card history" id="historyPanel" hidden>/);
 assert.match(f.text('events'),/Проверка выполнена/);
 assert.match(f.text('events'),/<img src=x onerror=bad\(\)>/);
 assert.equal(f.text('historyCount'),'1');
});
test('owner question and paused wait are distinct and do not invent wait time',async()=>{
 const owner=sample();owner.waiting={kind:'owner_question',note:'Утвердить границу до seal?',since:owner.observedAt};
 const f=await fixture('',owner);assert.equal(f.el('waitingPanel').hidden,false);
 assert.equal(f.text('waitingKind'),'Вопрос владельцу');assert.match(f.text('waitingNote'),/до seal/);
 f.advance(4000);assert.equal(f.text('runElapsed'),'2 мин 00 с');
 assert.equal(f.text('time-T01'),'30 с');
 const paused=sample('paused');paused.waiting={kind:'paused',note:'Ожидаем доступ',since:paused.observedAt};
 const p=await fixture('',paused);assert.equal(p.text('waitingKind'),'Ожидание');
 p.advance(60000);assert.equal(p.text('runElapsed'),'2 мин 00 с');
 assert.doesNotMatch(p.text('waitingSince'),/мин|час/);
});
test('locale and theme controls work with missing or blocked storage',async()=>{
 const f=await fixture();f.el('language').change('en');f.el('theme').change('dark');
 assert.equal(f.text('label'),'In progress');assert.equal(f.staticText('checks'),'Checks');
 assert.equal(f.htmlElement.attributes['data-theme'],'dark');assert.equal(f.htmlElement.lang,'en');
 const blocked={getItem(){throw Error('disabled')},setItem(){throw Error('disabled')}};
 const b=await fixture('',sample(),{storage:blocked,systemDark:true});
 assert.equal(b.htmlElement.attributes['data-theme'],'dark');b.el('language').change('en');
 assert.equal(b.text('label'),'In progress');
});
test('saved locale and theme are restored and can switch back to system',async()=>{
 const values=new Map([['aispa.locale','en'],['aispa.theme','light']]);
 const storage={getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)};
 const f=await fixture('',sample(),{storage,systemDark:true});
 assert.equal(f.text('label'),'In progress');assert.equal(f.htmlElement.attributes['data-theme'],'light');
 f.el('theme').change('system');assert.equal(f.htmlElement.attributes['data-theme'],'dark');
 assert.equal(values.get('aispa.theme'),'system');
});
test('embedded accepted archive is explicitly historical and never fetches or ticks',async()=>{
 const archive=archivedSample();archive.events=[{type:'init',at:'2026-10-19T12:00:00Z',text:'Начало'}];
 const f=await fixture('?status=running&demo',sample(),{archive});
 assert.equal(f.fetchCount(),0);assert.equal(f.intervalCount(),0);
 assert.equal(f.text('label'),'Принято на дату снимка');assert.match(f.text('sourceLabel'),/Архивный снимок/);
 assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 1 \/ 1 · на дату снимка/);
 assert.match(f.text('waves'),/Реализовано · на дату снимка/);
 assert.equal(f.el('refresh').hidden,true);assert.equal(f.text('runElapsed'),'2 мин 00 с');
 f.advance(90000);assert.equal(f.text('runElapsed'),'2 мин 00 с');
 await f.el('refresh').click();assert.equal(f.fetchCount(),0);
});
test('invalid archive does not fall through to live or claim acceptance',async()=>{
 const f=await fixture('?status=accepted',{...sample(),status:'accepted'},{archive:{archive:true,status:'accepted'}});
 assert.equal(f.fetchCount(),0);assert.equal(f.intervalCount(),0);
 assert.equal(f.text('label'),'Состояние недоступно');assert.match(f.text('sourceLabel'),/недоступен/);
});
test('execution and inherited deadlines remain visible without a false accepted archive',async()=>{
 const archive=archivedSample();archive.status='delivered_with_remaining';archive.executionPolicy=true;
 archive.remaining.execution=[{id:'E01',event:'host-final-handoff',status:'pending',deadline:'2026-10-19T12:30:00Z'}];
 archive.remaining.inheritedExecution=[{id:'CE-1',event:'engine-finish',status:'failed',deadline:'2026-10-18T12:30:00Z'}];
 const f=await fixture('',sample(),{archive});
 assert.match(f.text('remaining'),/Сроки исполнения/);
 assert.match(f.text('remaining'),/Финальная передача/);
 assert.match(f.text('remaining'),/Ожидает подтверждения/);
 assert.match(f.text('remaining'),/Сроки прошлых прогонов/);
 assert.match(f.text('remaining'),/Срок не соблюдён/);
 f.el('language').change('en');
 assert.match(f.text('remaining'),/Awaiting confirmation/);
 assert.match(f.text('remaining'),/Deadline missed/);
 const forged={...archive,status:'accepted'};
 const invalid=await fixture('',sample(),{archive:forged});
 assert.doesNotMatch(invalid.text('label'),/Все действующие требования приняты/);
 const absent={...archive,remaining:{...archive.remaining}};delete absent.remaining.execution;
 const missing=await fixture('',sample(),{archive:absent});
 assert.match(missing.text('sourceLabel'),/недоступен|некоррект|Невер|Не удалось|не подтвержд/i);
});
test('terminal archive rejects empty arrays, missing proof, broken gates and dates',async()=>{
 const bad=[
  {...archivedSample(),requirements:[],tasks:[]},
  {...archivedSample(),archivedAt:'2026-02-30T12:00:00Z'},
  {...archivedSample(),gates:{coverage:true,plan:true,acceptance:false}},
  {...archivedSample(),verified:false},
  {...archivedSample(),tasks:[{...archivedSample().tasks[0],proofCurrent:false}]},
  {...archivedSample(),tasks:[{...archivedSample().tasks[0],requirements:[]}]},
 ];
 for(const archive of bad){const f=await fixture('',sample(),{archive});
  assert.equal(f.text('label'),'Состояние недоступно');assert.equal(f.fetchCount(),0);}
});
test('running archive uses exporter fields and labels work as historical',async()=>{
 const archive=archivedSample('running');const f=await fixture('',sample(),{archive});
 assert.equal(f.fetchCount(),0);assert.equal(f.text('label'),'В работе · на дату снимка');
 assert.match(f.text('sourceLabel'),/Архивный снимок/);
 assert.match(f.text('waves'),/Была в работе · на дату снимка/);
});
test('offline and demo acceptance stay qualified; global stale preserves scoped task proof',async()=>{
 const live=sample('accepted');live.tasks[0].status='completed';live.tasks[0].proofCurrent=true;
 const f=await fixture('',live);await f.fail();
 assert.equal(f.text('label'),'Состояние не подтверждено');
 assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 0 \/ 1 · последний снимок/);
 assert.match(f.text('waves'),/Реализовано · последний снимок/);
 const demo=await fixture('?demo&status=accepted');
 assert.match(demo.text('label'),/пример/);assert.match(demo.text('waves'),/Реализовано · пример/);
 const stale=sample('stale');stale.tasks[0].status='completed';stale.tasks[0].proofCurrent=true;
 const s=await fixture('',stale);
 assert.match(s.text('label'),/устарела/);assert.match(s.text('waves'),/РеализованоПроверено/);
});
test('light faint text and touch controls meet stated size and contrast',()=>{
 const faint=html.match(/--faint:(#[0-9A-Fa-f]{6})/)[1];
 function lum(hex){const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255);
  return rgb.map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4)
   .reduce((sum,v,i)=>sum+v*[0.2126,0.7152,0.0722][i],0);}
 function contrast(a,b){const [hi,lo]=[lum(a),lum(b)].sort((x,y)=>y-x);return (hi+0.05)/(lo+0.05);}
 assert.ok(contrast(faint,'#FFFFFF')>=4.5);assert.ok(contrast(faint,'#F4F5F6')>=4.5);
 assert.match(html,/\.pref\{height:44px/);assert.match(html,/\.btn\{height:44px/);assert.match(html,/\.tab\{[^}]*height:44px/);
 assert.match(html,/\.task-body b\{font-size:16px/);assert.match(html,/\.dec b\{[^}]*font-size:16px/);
});


test('stable version and prerelease labels follow the actual version',async()=>{
 const d=sample();d.version='0.1.0';const f=await fixture('',d);
 assert.equal(f.text('version'),'0.1.0');
 d.version='0.1.0+build-test';await f.update(d);assert.equal(f.text('version'),d.version);
 d.version='0.1.0-alpha.10';await f.update(d);assert.equal(f.text('version'),'Альфа · '+d.version);
 d.version='0.2.0-rc.1';await f.update(d);assert.match(f.text('version'),/^Предварительная версия/);
 f.el('language').change('en');assert.match(f.text('version'),/^Prerelease/);
});
test('light completion is distinct from task and project acceptance',async()=>{
 const d=sample();d.tasks=[{id:'T01',title:'Текст подсказки',requirements:['R01'],wave:1,
 status:'completed',proofCurrent:true,tier:'light',tierReason:'Только текст',completionKind:'verified_completion',
 perTaskReview:'skipped_light',elapsedSeconds:12,
 phaseSteps:[{id:'implementation',label:'Реализация',status:'done'},
 {id:'verified_completion',label:'Выполнение проверено',status:'done'},
 {id:'critic_1',label:'Первый критик',status:'skipped'}]}];
 const f=await fixture('',d);assert.match(f.text('waves'),/Выполнение проверено/);
 assert.match(f.text('waves'),/Лёгкая задача/);assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 1 \/ 1/);
 assert.doesNotMatch(f.text('waves'),/Задача принята|задач принято/);assert.equal(f.text('label'),'В работе');
 d.tasks[0].proofCurrent=false;await f.update(d);assert.match(f.text('waves'),/Проверку нужно обновить/);
 assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 0 \/ 1/);
 d.tasks[0].tier='heavy';d.tasks[0].completionKind=null;d.tasks[0].status='running';
 await f.update(d);assert.match(f.text('waves'),/Тяжёлая задача/);assert.doesNotMatch(f.text('waves'),/Без отдельных критиков/);
});
test('standard and lean tasks name their review route',async()=>{
 const d=sample();d.tasks=[{id:'T01',title:'Экран отчёта',requirements:['R01'],wave:1,
 status:'completed',proofCurrent:true,tier:'standard',tierReason:'Обычный модуль',completionKind:'verified_completion',
 perTaskReview:'skipped_standard',elapsedSeconds:12,phaseSteps:[]},
 {id:'T02',title:'Финал',requirements:['R01'],wave:2,status:'completed',proofCurrent:true,tier:'heavy',
 reviewPolicy:'lean',completionKind:'critic_acceptance',elapsedSeconds:20,phaseSteps:[]}];
 const f=await fixture('',d);const text=f.text('waves');
 assert.match(text,/Обычная задача · Без критика задачи · общий обзор версии/);
 assert.match(text,/Тяжёлая задача · PASS критика закрывает задачу/);assert.match(text,/РеализованоПроверено/);
 f.el('language').change('en');assert.match(f.text('waves'),/Standard task · No task reviewer/);
 assert.match(f.text('waves'),/A passing critic closes the task/);
});
test('mode changes are shown with time, direction and the owner words',async()=>{
 const d=sample();d.checkMode='fast';d.checkModeLabel='Быстро';
 d.checkModeHistory=[{mode:'max',previous:'balanced',at:'2026-10-22T10:00:00Z',text:'Проверь всё тщательно'},
  {mode:'fast',previous:'max',at:'2026-10-22T11:00:00Z',text:'Теперь быстро'}];
 d.remaining={skippedChecks:['Проверка задач критиками','Тестовый контракт RED']};
 const f=await fixture('',d);
 assert.equal(f.el('modeChangesPanel').hidden,false);
 const text=f.text('modeChanges');
 assert.match(text,/Сбалансированно → Максимум/);assert.match(text,/«Проверь всё тщательно»/);
 assert.match(text,/Максимум → Быстро/);assert.match(text,/«Теперь быстро»/);
 assert.ok(text.indexOf('Теперь быстро')<text.indexOf('Проверь всё'));
 assert.match(f.text('remaining'),/Пропущенные проверки/);assert.match(f.text('remaining'),/Тестовый контракт RED/);
 f.el('language').change('en');
 assert.match(f.text('modeChanges'),/Balanced → Maximum/);assert.match(f.text('remaining'),/RED test contract/);
 const none=await fixture('',sample());assert.equal(none.el('modeChangesPanel').hidden,true);
});
test('fast mode says the waves are not checked against the brief and names skipped scenarios',async()=>{
 const d=sample();d.checkMode='fast';d.checkModeLabel='Быстро';
 d.remaining={skippedChecks:['Подробные сценарии (только main и error)']};
 const f=await fixture('',d);
 assert.match(f.text('wavesNote'),/В быстром режиме волны не сверяются с брифом/);
 assert.match(f.text('remaining'),/Подробные сценарии \(только main и error\)/);
 f.el('language').change('en');
 assert.match(f.text('wavesNote'),/In fast mode waves are not checked/);
 assert.match(f.text('remaining'),/Detailed scenarios \(main and error only\)/);
 const plain=await fixture('',sample());assert.match(plain.text('wavesNote'),/После каждой волны проверяется весь бриф/);
});
test('check mode, fast tasks and deferred tasks are named',async()=>{
 const d=sample();d.checkMode='max';d.checkModeLabel='Максимум';
 d.tasks=[{id:'T01',title:'Быстрая',requirements:['R01'],wave:1,status:'completed',proofCurrent:true,tier:'fast',
  plannedTier:'heavy',tierReason:'Режим «быстро»',completionKind:'verified_completion',perTaskReview:'skipped_fast',elapsedSeconds:5,phaseSteps:[]},
  {id:'T02',title:'Сложная',requirements:['R01'],wave:2,status:'blocked',proofCurrent:false,tier:'heavy',deferred:true,
   elapsedSeconds:9,phaseSteps:[]}];
 const f=await fixture('',d);
 assert.equal(f.el('checkModeBox').hidden,false);assert.equal(f.el('checkModeName').textContent,'Максимум');
 assert.match(f.text('waves'),/Быстрая задача · Без проверок по ходу · итоговая приёмка/);
 assert.match(f.text('waves'),/Отложено в остатки/);
 f.el('language').change('en');
 assert.equal(f.el('checkModeName').textContent,'Maximum');
 assert.match(f.text('waves'),/Fast task · No in-flight checks · final acceptance/);
 assert.match(f.text('waves'),/Deferred to remaining/);
 const old=await fixture('',sample());assert.equal(old.el('checkModeBox').hidden,true);
});
test('history shows an unknown event by its own text and keeps the newest rows',async()=>{
 const d=sample();d.events=[];
 for(let i=0;i<20;i++)d.events.push({type:'e'+i,at:d.observedAt,text:'Событие: e'+i});
 d.events.push({type:'brief-review-recorded',at:d.observedAt,text:'Записана независимая сверка брифа'});
 const f=await fixture('',d);
 assert.match(f.text('events'),/Записана независимая сверка брифа/);
 assert.match(f.text('events'),/Событие: e19/);
 assert.doesNotMatch(f.text('events'),/Событие: e19 · Событие: e19/);
 assert.equal(f.text('historyCount'),'16');
});
test('preparation stage names the real work and the age of the last journal action',async()=>{
 const d=sample();d.stage='brief';d.displayStage='brief';d.tasks=[d.tasks[0]];d.tasks[0].status='pending';
 d.currentWork={title:'Независимая сверка брифа',note:'Уточнение брифа: план сохранён, задачи продолжатся после пересогласования',
  lastActionAt:'2026-10-19T11:56:06Z',lastAction:'Записана выжимка брифа'};
 const f=await fixture('',d);const text=f.text('currentActivity');
 assert.match(text,/Независимая сверка брифа/);assert.match(text,/план сохранён/);
 assert.doesNotMatch(text,/Ожидает запуска/);
 assert.equal(f.el('last-action'),null);assert.equal(f.el('journalNotice').hidden,true);
});
test('legacy status without currentWork on a preparation stage does not claim a task is waiting to start',async()=>{
 const d=sample();d.stage='brief';d.displayStage='brief';d.tasks[0].status='pending';
 const f=await fixture('',d);const text=f.text('currentActivity');
 assert.doesNotMatch(text,/Ожидает запуска/);assert.match(text,/Бриф/);
 assert.equal(f.el('last-action'),null);
});
test('a new attempt shows the previous one and labels the clock as all attempts',async()=>{
 const d=sample();d.tasks[0].attemptNumber=2;
 d.tasks[0].attemptNote='Попытка 2 · прошлая: критик ×2, блок → новая по словам владельца';
 const f=await fixture('',d);
 assert.match(f.text('waves'),/Попытка 2 · прошлая: критик ×2, блок/);
 assert.match(f.text('waves'),/Всего по всем попыткам/);
 assert.match(f.text('waves'),/Всего по всем попыткам/);
 const old=await fixture('',sample());assert.doesNotMatch(old.text('waves'),/Всего по всем попыткам/);
});
test('missing estimate data explains when it will appear',async()=>{
 const f=await fixture();assert.equal(f.text('etaNote'),'Оценка появится после двух завершённых задач.');
 const d=sample();d.estimate={reason:'insufficient_data',sampleCount:1};
 const one=await fixture('',d);assert.match(one.text('etaNote'),/после второй/);
});

test('assigned pending worker is visible without claiming implementation or inventing a clock',async()=>{
 const d=sample();d.tasks[0].status='pending';d.tasks[0].elapsedSeconds=null;delete d.tasks[0].activeSince;
 d.tasks[0].assignment={role:'worker',roleLabel:'Исполнитель',runtime:'codex',model:'fixture-model',assignedAt:d.observedAt,status:'assigned'};
 const f=await fixture('',d);assert.match(f.text('waves'),/Исполнитель назначен/);
 assert.match(f.text('waves'),/Назначен: Исполнитель · codex · fixture-model/);
 assert.equal(f.text('time-T01'),'Старт реализации не отмечен');
 f.advance(5000);assert.equal(f.text('time-T01'),'Старт реализации не отмечен');
 d.tasks[0].status='running';d.tasks[0].elapsedSeconds=0;d.tasks[0].activeSince=d.observedAt;
 d.tasks[0].phaseLabel='Реализация';d.tasks[0].phaseTiming={phase:'implementation',elapsedSeconds:0,activeSince:d.observedAt};
 await f.update(d);f.advance(3000);assert.equal(f.text('time-T01'),'3 с');
 assert.equal(f.text('phase-time-T01'),'3 с');assert.match(f.text('waves'),/Реализация/);
});

test('clean-clone verification remains visible and timed with no running tasks',async()=>{
 const d=sample();d.tasks=[];d.activity={kind:'clean-verification',startedAt:'2026-10-19T11:59:50Z'};
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/Проверка в чистом клоне/);
 assert.equal(f.text('verification-time'),'10 с');f.advance(2000);assert.equal(f.text('verification-time'),'12 с');
 await f.fail();f.advance(10000);assert.doesNotMatch(f.text('currentActivity'),/Проверка в чистом клоне/);
 const en=await fixture('',d);en.el('language').change('en');assert.match(en.text('currentActivity'),/Verification in a clean clone/);
});

test('unknown activity cannot hide current preparation and invalid timestamps cannot invent a clock',async()=>{
 const d=sample();d.tasks=[];d.displayStage='spec';d.activity={kind:'future-kind'};
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/Спецификация/);
 d.activity={kind:'clean-verification',startedAt:'invalid'};await f.update(d);
 assert.equal(f.text('verification-time'),'Время не записано');
});

test('reported preparation interval runs only from explicit progress and stops on end or pause',async()=>{
 const d=sample();d.tasks=[];d.workProgress={phase:'discovery',label:'Изучение проекта',startedAt:d.observedAt,activeSince:d.observedAt,elapsedSeconds:10};
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/Изучение проекта/);
 assert.match(f.text('currentActivity'),/Начало этапа отмечено агентом/);
 f.advance(2000);assert.equal(f.text('work-progress-time'),'12 с');
 d.status='paused';d.workProgress.elapsedSeconds=12;delete d.workProgress.activeSince;await f.update(d);
 assert.equal(f.text('work-progress-time'),'12 с');f.advance(4000);assert.equal(f.text('work-progress-time'),'12 с');assert.match(f.text('currentActivity'),/Пауза/);
 d.status='running';d.workProgress.activeSince=d.observedAt;await f.update(d);f.advance(1000);
 assert.equal(f.text('work-progress-time'),'13 с');
 d.workProgress.finishedAt=d.observedAt;delete d.workProgress.activeSince;await f.update(d);
 assert.equal(f.el('work-progress-time'),null);
});

test('reported work and task assignment stay text-only for untrusted labels',async()=>{
 const d=sample();d.workProgress={phase:'discovery',label:'<img src=x onerror=bad()>',elapsedSeconds:0,activeSince:d.observedAt};
 d.tasks[0].assignment={roleLabel:'<script>bad()</script>',runtime:'codex',model:'<b>model</b>'};
 const f=await fixture('',d);assert.match(f.text('currentActivity'),/<img src=x onerror=bad\(\)>/);
 assert.match(f.text('waves'),/<script>bad\(\)<\/script>/);
});

test('compact hero contains work and both clocks before visible checks stages and tasks',()=>{
 const hero=html.indexOf('<section class="hero-row">'),stages=html.indexOf('<section class="card stages-card">'),waves=html.indexOf('<section class="card waves-card"');
 assert.ok(hero<html.indexOf('id="gates"')&&html.indexOf('id="gates"')<stages&&stages<waves&&waves<html.indexOf('id="requirements"'));
 const overview=html.slice(hero,html.indexOf('</section>',hero));
 for(const id of ['currentActivity','overviewCounts','runElapsed','eta']){
  assert.ok(overview.includes('id="'+id+'"'));assert.equal((html.match(new RegExp('id="'+id+'"','g'))||[]).length,1);
 }
 assert.doesNotMatch(html,/<section class="card activity-card"/);
 assert.match(html,/\.task-summary\{display:grid/);assert.match(html,/@media\(max-width:480px\)/);
});

test('gate details distinguish missing active failed and stale proof without inventing a pass',async()=>{
 const d=sample();d.gates={coverage:false,plan:false,acceptance:false};
 d.gateDetails={coverage:{status:'running',completed:1,total:3,reason:'recorded_work'},plan:{status:'failed',completed:1,total:2,reason:'checked_report'},acceptance:{status:'stale',completed:0,total:2,reason:'basis_changed'}};
 const f=await fixture('',d);assert.match(f.text('gates'),/Выполняется/);assert.match(f.text('gates'),/Есть замечания/);assert.match(f.text('gates'),/Нужна повторная проверка/);assert.match(f.text('gates'),/1 \/ 3/);assert.doesNotMatch(f.text('gates'),/✓/);
 d.gateDetails.coverage.status='passed';await f.update(d);assert.doesNotMatch(f.text('gates'),/✓/);
 d.gates.coverage=true;await f.update(d);assert.match(f.text('gates'),/✓/);await f.fail();assert.doesNotMatch(f.text('gates'),/✓/);
});

test('requirements reveal partial work and distinct final acceptance inside task cards',async()=>{
 const d=sample();const p={status:'partial',completed:1,total:2,counts:{completed:1,queued:1},acceptance:{status:'pending'}};
 d.requirements[0].progress=p;d.tasks[0].requirementsDetails=[{...d.requirements[0]}];
 d.tasks[0].criteriaDetails=[{text:'Письмо доставлено',status:'passed',source:'checked_report'},{text:'Повтор не создаёт дубль',status:'pending',source:'task_state'}];
 const f=await fixture('',d);assert.match(f.text('requirements'),/Частично выполнено/);assert.match(f.text('waves'),/Отправка формы/);assert.match(f.text('waves'),/1 \/ 2/);assert.match(f.text('waves'),/Письмо доставлено/);assert.match(f.text('waves'),/По сохранённому отчёту проверки/);assert.doesNotMatch(f.text('requirements'),/Принято по брифу/);
 p.acceptance.status='passed';await f.update(d);assert.match(f.text('requirements'),/Принято по брифу/);
});

test('expanded task requirements survive polling and wave review appears separately',async()=>{
 const d=sample();d.waveDetails=[{wave:1,review:{status:'failed',reason:'checked_checkpoint'}}];
 const f=await fixture('',d),task=f.el('task-T01');assert.equal(task.tag,'details');assert.equal(task.open,false);
 task.open=true;task.events.toggle();await f.update(d);assert.equal(f.el('task-T01').open,true);assert.match(f.text('waves'),/Сверка волны: Есть замечания/);
});

test('planning ETA is visibly an assumption before completed samples exist',async()=>{
 const d=sample();d.estimate={reason:'estimate',basis:'plan_assumption',confidence:'low',sampleCount:0,lowerSeconds:600,upperSeconds:1800};
 const f=await fixture('',d);assert.equal(f.text('eta'),'≈ 10–30 мин');assert.match(f.text('etaNote'),/Предварительный ориентир по плану/);
 f.el('language').change('en');assert.match(f.text('etaNote'),/Preliminary estimate/);
});

test('healthy polling is distinguished from a journal with no new events',async()=>{
 const d=sample();d.updatedAt='2026-10-19T11:50:00Z';const f=await fixture('',d);
 assert.equal(f.el('journalNotice').hidden,false);assert.match(f.text('journalNotice'),/Соединение работает/);
 d.updatedAt=d.observedAt;await f.update(d);assert.equal(f.el('journalNotice').hidden,true);
});

test('malformed nested payload never replaces the last good snapshot',async()=>{
 for(const mutate of [d=>d.decisions=[null],d=>d.tasks[0].criteriaDetails=[null],d=>d.waveDetails=[null],d=>d.requirements[0].progress={status:'partial',completed:5,total:2}]){
  const f=await fixture(),bad=sample();bad.title='BAD CANDIDATE';mutate(bad);await f.update(bad);
  assert.equal(f.text('title'),'Тестовый проект');assert.equal(f.text('label'),'Состояние не подтверждено');assert.doesNotMatch(f.text('gates'),/✓/);
 }
});

test('a compatible viewer identifies both its own version and the unchanged project runtime',async()=>{
 const d=sample();d.dashboardVersion='0.3.2';d.version='0.3.0';d.viewerMode='compatible-view';
 d.autonomy={mayAsk:false,briefingClosedAt:d.observedAt};
 const f=await fixture('',d);assert.equal(f.text('version'),'0.3.2 · проект 0.3.0');assert.equal(f.el('autonomyNote').hidden,false);assert.match(f.text('autonomyNote'),/самостоятельно/);
});

test('worker verification before a critic is explicitly provisional and not a green pass',async()=>{
 const d=sample();d.tasks[0].criteriaDetails=[{text:'Проверить вывод',status:'reported',source:'checked_report',checkedBy:'worker',provisional:true}];
 const f=await fixture('',d);assert.match(f.text('waves'),/Проверено исполнителем/);assert.match(f.text('waves'),/Независимая проверка ещё впереди/);
 const criteria=f.el('task-T01').children[1].children.at(-1);assert.equal(criteria.children[1].children[1].className,'status-chip reported');
});
test('a historical wave checkpoint does not look like a fresh review of current code',async()=>{
 const d=sample();d.waveDetails=[{wave:1,review:{status:'passed',reason:'accepted_checkpoint',basisCurrent:false}}];
 const f=await fixture('',d);assert.match(f.text('waves'),/Пройдена на границе волны/);
 assert.equal(f.el('waves').children[0].children[2].children[1].className,'status-chip historical');
});
test('conditional ETA separates estimated work from unknown blocked waiting',async()=>{
 const d=sample();d.estimate={reason:'estimate',basis:'mixed',sampleCount:2,confidence:'low',lowerSeconds:600,upperSeconds:1800,waitingForBlockers:true};
 const f=await fixture('',d);assert.match(f.text('eta'),/10–30 мин работы \+ ожидание/);assert.match(f.text('etaNote'),/Время ожидания снятия блокеров неизвестно/);assert.match(f.text('etaNote'),/разных классов/);
});

test('completed stale task keeps implementation separate from verification in both languages',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=false;d.tasks[0].phaseStale=true;
 d.tasks[0].phaseSteps=[{id:'implementation',label:'Реализация',status:'stale'},{id:'verified_completion',label:'Выполнение проверено',status:'stale'}];
 const original=JSON.stringify(d);const f=await fixture('',d);const task=f.el('task-T01');
 assert.match(task.children[0].textContent,/РеализованоПроверку обновить/);
 assert.match(task.children[1].textContent,/Проверку нужно обновить/);
 assert.equal(f.el('task-phases-T01').children[0].children[0].className,'phase-step recorded');
 assert.doesNotMatch(task.textContent,/Проверка актуальна|Задача принята/);assert.equal(JSON.stringify(d),original);
 f.el('language').change('en');
 assert.match(f.text('task-T01'),/ImplementedRefresh verification/);
});

test('stale requirements preserve implementation inside cards and in the shared registry',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=false;d.requirements[0].implementation='needs_check';
 d.requirements[0].progress={status:'stale',completed:0,total:1,acceptance:{status:'pending'}};
 d.tasks[0].requirementsDetails=[d.requirements[0]];
 const f=await fixture('',d);for(const id of ['requirements','task-T01']){
  assert.match(f.text(id),/Реализовано задач: 1 \/ 1 · актуально проверено: 0 \/ 1/);
  assert.match(f.text(id),/Проверку нужно обновить/);
 }
 assert.match(f.text('requirements'),/Реализовано/);assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 0 \/ 1/);
 assert.equal(f.el('waves').children[0].children[1].attributes['aria-valuenow'],'1');
 assert.equal(f.el('waves').children[0].children[1].attributes['aria-label'],'Реализованные задачи волны');
 assert.match(f.text('tabs'),/Требуют внимания/);
});

test('a stale running task and an unfinished shared requirement are never labelled fully implemented',async()=>{
 const d=sample();d.tasks[0].phaseStale=true;d.tasks[1].requirements=['R01'];d.tasks[1].status='completed';d.tasks[1].proofCurrent=false;
 d.requirements[0].progress={status:'stale',completed:0,total:2,acceptance:{status:'pending'}};
 const f=await fixture('',d);assert.match(f.text('task-T01'),/Проверки устарели/);
 assert.doesNotMatch(f.el('task-T01').children[0].textContent,/Реализовано/);
 assert.match(f.text('requirements'),/Частично реализовано/);assert.match(f.text('requirements'),/Реализовано задач: 1 \/ 2/);
});

test('offline completed history keeps its work but withdraws current verification',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=true;const f=await fixture('',d);
 await f.fail();assert.match(f.text('task-T01'),/Реализовано · последний снимокНет подтверждения/);
 assert.doesNotMatch(f.el('task-T01').children[0].textContent,/Проверка актуальна/);
 assert.match(f.text('waves'),/Реализовано: 1 \/ 1 · актуально проверено: 0 \/ 1/);
});

test('attention filter includes stale proof or stale acceptance even when implementation is recorded',async()=>{
 const d=sample();d.tasks[0].status='completed';d.tasks[0].proofCurrent=true;
 d.requirements[0].implementation='implemented';d.requirements[0].progress={status:'completed',completed:1,total:1,acceptance:{status:'stale'}};
 const f=await fixture('',d);f.el('tabs').children[3].click();
 assert.match(f.text('requirements'),/Отправка формы/);assert.match(f.text('requirements'),/Итоговую приёмку нужно обновить/);
 d.requirements[0].progress.status='stale';d.requirements[0].progress.acceptance.status='pending';await f.update(d);assert.match(f.text('requirements'),/Отправка формы/);
 d.requirements[0].progress.status='partial';d.requirements[0].implementation='unfinished';await f.update(d);assert.match(f.text('requirements'),/Отправка формы/);
 d.requirements[0].progress.status='completed';d.requirements[0].implementation='implemented';d.requirements[0].progress.acceptance.status='passed';await f.update(d);assert.doesNotMatch(f.text('requirements'),/Отправка формы/);
 await f.fail();assert.match(f.text('requirements'),/Отправка формы/);assert.match(f.text('requirements'),/Актуальность проверки не подтверждена/);
});


test('task disclosure keeps phase timing assignment and criteria out of the concise summary',async()=>{
 const d=sample();const t=d.tasks[0];t.phase='critic_1';t.phaseLabel='Первый критик';
 t.phaseTiming={phase:'critic_1',elapsedSeconds:8,activeSince:d.observedAt};
 t.phaseSteps=[{id:'implementation',status:'done'},{id:'critic_1',status:'current'}];
 t.assignment={roleLabel:'Исполнитель',runtime:'codex',model:'fixture-model'};
 t.criteriaDetails=[{text:'Письмо доставлено',status:'pending',source:'task_state'}];
 const f=await fixture('',d),task=f.el('task-T01'),summary=task.children[0],detail=task.children[1];
 assert.equal(task.open,false);assert.equal(summary.tag,'summary');
 assert.match(summary.textContent,/Форма/);assert.match(summary.textContent,/Первый критик/);assert.match(summary.textContent,/30 с/);
 assert.doesNotMatch(summary.textContent,/codex|fixture-model|Требования:|Письмо доставлено|Интервал этапа|8 с|✓/);
 for(const text of ['codex','fixture-model','Письмо доставлено','Отправка формы','Интервал этапа','8 с'])assert.ok(detail.textContent.includes(text));
 assert.equal(f.el('task-phases-T01').children[0].attributes.role,'list');
});

test('overview names actual work and reports implemented blocked and pending counts',async()=>{
 const d=sample();d.tasks[0].title='Форма с длинным предметным заголовком';d.tasks[0].phase='critic_1';
 d.tasks.push({...d.tasks[1],id:'T03',status:'blocked'},{...d.tasks[1],id:'T04',status:'completed',proofCurrent:false});
 const f=await fixture('',d);
 assert.match(f.text('currentActivity'),/Форма с длинным предметным заголовком/);
 assert.match(f.text('overviewCounts'),/1 \/ 4Реализовано задач/);assert.match(f.text('overviewCounts'),/1Блокеры задач/);
 assert.match(f.text('overviewCounts'),/1На проверке/);assert.match(f.text('overviewCounts'),/1В очереди/);
 assert.match(f.text('overviewCounts'),/1Проверку обновить/);assert.doesNotMatch(f.text('overviewCounts'),/%|Принято/);
 f.el('language').change('en');assert.match(f.text('overviewCounts'),/1 \/ 4Implemented tasks/);assert.match(f.text('overviewCounts'),/1Under review/);
});

test('phase colors distinguish implementation review repair blocked and completed with text labels',async()=>{
 const d=sample();const base=d.tasks[0];d.tasks=[
  {...base,id:'T01',phase:'implementation',phaseLabel:'Реализация'},
  {...base,id:'T02',phase:'critic1',phaseLabel:'Первый критик'},
  {...base,id:'T03',phaseTiming:{phase:'repair',elapsedSeconds:1},phaseLabel:'Правка'},
  {...base,id:'T04',status:'blocked'},
  {...base,id:'T05',status:'completed',proofCurrent:false}];
 const f=await fixture('',d);
 for(const [id,kind,label] of [['T01','implementation','Реализация'],['T02','review','Первый критик'],['T03','repair','Правка'],['T04','blocked','Препятствие'],['T05','completed','Реализовано']]){
  assert.ok(f.el('task-'+id).className.split(' ').includes(kind));assert.ok(f.el('task-'+id).children[0].textContent.includes(label));
 }
 assert.match(html,/--review:#0F766E/);assert.match(html,/--review:#99F6E4/);
 assert.match(html,/\.task\.review\{border-left:4px solid var\(--review\)/);
 assert.match(f.el('task-T05').children[0].textContent,/Проверку обновить/);
});

test('overview counters and work headlines qualify offline archive paused and stale phases',async()=>{
 const d=sample();d.tasks[0].phase='critic_2';d.tasks[0].phaseStale=true;
 const f=await fixture('',d);assert.match(f.text('overviewCounts'),/Проверку обновить/);assert.doesNotMatch(f.text('overviewCounts'),/На проверке/);
 await f.fail();assert.match(f.text('currentActivity'),/Последний подтверждённый снимок/);assert.match(f.text('overviewCounts'),/Данные последнего снимка/);
 assert.doesNotMatch(f.text('overviewCounts'),/На проверке|Реализация/);
 const paused=await fixture('',sample('paused'));assert.match(paused.text('currentActivity'),/Пауза/);
 assert.match(paused.text('overviewCounts'),/Сохранённая активность/);assert.doesNotMatch(paused.text('currentActivity'),/Форма|В работе задач/);
 const archive=await fixture('',sample(),{archive:archivedSample('running')});assert.match(archive.text('currentActivity'),/Состояние на дату снимка/);
 assert.match(archive.text('overviewCounts'),/Активность на момент снимка/);
});

test('an owner question suppresses stale active headlines estimates and ready claims',async()=>{
 const d=sample('accepted');d.waiting={kind:'owner_question',note:'Нужно решение'};
 d.estimate={reason:'estimate',lowerSeconds:600,upperSeconds:1200,sampleCount:2};
 const f=await fixture('',d);assert.equal(f.text('label'),'Приостановлено');
 assert.match(f.text('currentActivity'),/Ожидает ответа владельца/);assert.doesNotMatch(f.text('currentActivity'),/Форма/);
 assert.equal(f.text('eta'),'Ожидает ответа владельца');assert.doesNotMatch(f.text('overviewCounts'),/Реализация/);
 f.advance(2000);assert.equal(f.text('runElapsed'),'2 мин 00 с');
});

test('polling preserves native disclosure and focused summary before its asynchronous toggle event',async()=>{
 const d=sample(),f=await fixture('',d),task=f.el('task-T01'),summary=f.el('task-summary-T01');
 task.open=true;summary.focus();await f.update(d);
 assert.equal(f.el('task-T01').open,true);assert.equal(f.activeElement(),f.el('task-summary-T01'));
 assert.equal(f.activeElement().focusOptions.preventScroll,true);
 task.open=false;task.events.toggle(); // A detached node must not overwrite the live disclosure.
 await f.update(d);assert.equal(f.el('task-T01').open,true);
 f.el('task-T01').open=false;await f.update(d);assert.equal(f.el('task-T01').open,false);
});

test('an empty plan or no data never becomes a completion percentage in the overview',async()=>{
 const d=sample();d.tasks=[];const f=await fixture('',d);
 assert.match(f.text('overviewCounts'),/План задач ещё не подготовлен/);assert.doesNotMatch(f.text('overviewCounts'),/100|0 \/ 0|Реализовано/);
 const none=await fixture('',null);assert.equal(none.text('overviewCounts'),'');assert.equal(none.text('eta'),'Нет данных');
});

test('running explanation is disclosed while exception and readiness states stay visible',async()=>{
 const f=await fixture();assert.equal(f.el('heroStatus').hidden,true);assert.equal(f.el('runExplanation').hidden,false);
 assert.match(f.text('runExplanation'),/Промежуточный результат не является приёмкой/);
 for(const status of ['paused','blocked','stale','accepted','delivered_with_remaining']){
  await f.update(sample(status));assert.equal(f.el('heroStatus').hidden,false);assert.equal(f.el('runExplanation').hidden,true);
 }
 await f.fail();assert.equal(f.el('heroStatus').hidden,false);assert.match(f.text('hint'),/Текущая готовность неизвестна/);
});

test('compact ETA qualifier preserves assumptions and unknown waiting without the long explanation',async()=>{
 const d=sample();d.estimate={reason:'estimate',basis:'plan_assumption',sampleCount:0,lowerSeconds:600,upperSeconds:1200};
 const f=await fixture('',d);assert.equal(f.text('etaQualifier'),'Ориентир по плану');assert.match(f.text('etaNote'),/Предварительный ориентир/);
 d.estimate.basis='mixed';await f.update(d);assert.equal(f.text('etaQualifier'),'По замерам и плану');
 d.estimate.waitingForBlockers=true;await f.update(d);assert.equal(f.text('etaQualifier'),'Ожидание блокеров не учтено');assert.match(f.text('eta'),/работы \+ ожидание/);
 await f.fail();assert.equal(f.text('etaQualifier'),'');assert.equal(f.text('eta'),'Не подтверждено');
 const paused=sample('paused');paused.activity={kind:'verification',startedAt:paused.observedAt};
 const p=await fixture('',paused);assert.match(p.text('currentActivity'),/Сохранённая активность · Проверка кода/);assert.doesNotMatch(p.text('currentActivity'),/Выполняется проверка/);
});

test('an unknown task status remains unknown instead of being added to queued work',async()=>{
 const d=sample();d.tasks=[{...d.tasks[0],status:'future-state'}];const f=await fixture('',d);
 assert.match(f.text('overviewCounts'),/1Состояние не определено/);assert.doesNotMatch(f.text('overviewCounts'),/В очереди/);
 assert.match(f.el('task-T01').children[0].textContent,/Состояние не определено/);
});


test('a new run never inherits task disclosure or summary focus from matching task IDs',async()=>{
 const d=sample();d.run='first-run';const f=await fixture('',d);
 f.el('task-T01').open=true;f.el('task-summary-T01').focus();
 const same={...d};await f.update(same);
 assert.equal(f.el('task-T01').open,true);assert.equal(f.activeElement(),f.el('task-summary-T01'));
 const previous=f.el('task-T01'),oldSummary=f.el('task-summary-T01'),next={...d,run:'second-run'};
 await f.update(next);
 assert.equal(f.el('task-T01').open,false);assert.notEqual(f.activeElement(),f.el('task-summary-T01'));
 assert.equal(f.el('task-summary-T01').focusOptions,undefined);
 previous.events.toggle();await f.update(next);assert.equal(f.el('task-T01').open,false);
 assert.notEqual(f.activeElement(),f.el('task-summary-T01'));
 assert.notEqual(f.el('task-summary-T01'),oldSummary);
 // Dropping a run identifier is also a boundary, never evidence of the same run.
 f.el('task-T01').open=true;f.el('task-summary-T01').focus();await f.update(sample());
 assert.equal(f.el('task-T01').open,false);assert.notEqual(f.activeElement(),f.el('task-summary-T01'));
});

test('mobile task titles span the status and time columns before the narrow phone breakpoint',()=>{
 const layout=html.match(/@media\(max-width:650px\)\{\.task-summary[\s\S]*?(?=@media\(max-width:480px\))/)[0];
 assert.match(layout,/\.task-summary \.task-body\{grid-column:2 \/ 4;grid-row:1/);
 assert.match(layout,/\.task-right\{grid-column:2;grid-row:2/);
 assert.match(layout,/\.task-elapsed\{grid-column:3;grid-row:2/);
 assert.match(layout,/\.task-toggle\{grid-column:4;grid-row:1/);
 assert.doesNotMatch(layout,/text-overflow|line-clamp|white-space:nowrap/);
});


test('task disclosures have a visible translated details label and a full accessible label',async()=>{
 const f=await fixture();let toggle=f.el('task-summary-T01').children[0].children.at(-1);
 assert.equal(toggle.children[0].className,'task-toggle-label');assert.equal(toggle.children[0].textContent,'Детали');
 assert.equal(toggle.children[0].attributes['aria-hidden'],'true');assert.equal(toggle.children[1].textContent,'Детали задачи');
 f.el('language').change('en');toggle=f.el('task-summary-T01').children[0].children.at(-1);
 assert.equal(toggle.children[0].textContent,'Details');assert.equal(toggle.children[1].textContent,'Task details');
 assert.match(html,/\.task-time\{font-size:12px\}/);assert.doesNotMatch(html,/\.task-time\{font-size:11px\}/);
 assert.match(html,/\.task-detail \.task-time\{font-size:13px\}/);
 assert.match(html,/\.phase-step\{[^}]*font-size:13px/);
});


test('mixed evidence badges describe every criterion route in Russian and English',async()=>{
 const d=sample(),t=d.tasks[0];t.criteriaDetails=[{text:'Открывает форму',status:'pending',source:'task_state'}];
 t.evidenceSummary={mode:'mixed',counts:{automated:1,browser:1,process:1},routes:['automated','browser','process'].map(kind=>({criterion:'Открывает форму',kind}))};
 const f=await fixture('',d);assert.match(f.text('waves'),/Автотест/);assert.match(f.text('waves'),/Браузер/);assert.match(f.text('waves'),/Процесс/);
 f.el('language').change('en');assert.match(f.text('waves'),/Automated/);assert.match(f.text('waves'),/Browser/);assert.match(f.text('waves'),/Process/);
 delete t.evidenceSummary;await f.update(d);assert.doesNotMatch(f.text('waves'),/Automated|Browser|Process/);
});

test('malformed evidence summaries cannot replace a known dashboard snapshot',async()=>{
 const cases=[null,[],{}, {mode:'mixed',counts:{automated:0,browser:0,process:0},routes:[null]},
  {mode:'mixed',counts:{automated:0,browser:1,process:0},routes:[{criterion:'x',kind:'future'}]},
  {mode:'mixed',counts:{automated:2,browser:0,process:0},routes:[{criterion:'x',kind:'automated'}]}];
 for(const summary of cases){const f=await fixture(),d=sample();d.title='BAD MIXED';d.tasks[0].evidenceSummary=summary;await f.update(d);
  assert.equal(f.text('title'),'Тестовый проект');assert.equal(f.text('label'),'Состояние не подтверждено');}
});

// ---- Ticket 11: honest time, participants, calm polling --------------------------------------
function measured(extra={}){
 return {reason:'estimate',basis:'run',sampleCount:3,lowerSeconds:1260,upperSeconds:2330,overrunning:[],afterOverrun:false,
  unmeasuredTasks:[],waitIncluded:false,waitingForBlockers:false,notes:[],
  work:{workSeconds:3600,idleSeconds:600,ownerWaitSeconds:120},
  rows:[{kind:'tasks',lowerSeconds:1260,upperSeconds:2330,basis:'run',sampleCount:3,status:'running'},
   {kind:'branchReview',status:'pending',lowerSeconds:null,upperSeconds:null,basis:null,sampleCount:0},
   {kind:'finalRepair',status:'conditional',lowerSeconds:null,upperSeconds:null,basis:null,sampleCount:0},
   {kind:'blind',status:'pending',lowerSeconds:null,upperSeconds:null,basis:null,sampleCount:0}],...extra};
}
async function etaFor(estimate,language){
 const d=sample();d.estimate=estimate;const f=await fixture('',d);if(language==='en')f.el('language').change('en');return f;
}
test('remaining time under an hour is words in 5-minute steps, from an hour in half-hour steps',async()=>{
 let f=await etaFor(measured());assert.equal(f.text('eta'),'примерно 20–40 минут');
 f=await etaFor(measured({lowerSeconds:14460,upperSeconds:25140}));assert.equal(f.text('eta'),'примерно 4–7 часов');
 f=await etaFor(measured({lowerSeconds:5400,upperSeconds:9000}));assert.equal(f.text('eta'),'примерно 1,5–2,5 часа');
 f=await etaFor(measured({lowerSeconds:2400,upperSeconds:5400}));assert.equal(f.text('eta'),'примерно 40 минут – 1,5 часа');
 f=await etaFor(measured({lowerSeconds:60,upperSeconds:200}));assert.equal(f.text('eta'),'меньше 5 минут');
 f=await etaFor(measured(),'en');assert.equal(f.text('eta'),'about 20–40 minutes');
 f=await etaFor(measured({lowerSeconds:14460,upperSeconds:25140}),'en');assert.equal(f.text('eta'),'about 4–7 hours');
});
test('one measurement is "usually around" and an unmeasured upper bound is not invented',async()=>{
 let f=await etaFor(measured({sampleCount:1,lowerSeconds:1500,upperSeconds:1500}));
 assert.equal(f.text('eta'),'обычно около 25 минут (по одной задаче)');
 f=await etaFor(measured({sampleCount:1,lowerSeconds:1500,upperSeconds:1500}),'en');assert.equal(f.text('eta'),'usually around 25 minutes (from one task)');
 f=await etaFor(measured({upperSeconds:null}));assert.match(f.text('eta'),/^примерно от 20 минут/);assert.match(f.text('eta'),/верхняя граница пока неизвестна/);
 assert.doesNotMatch(f.text('eta'),/–/);
});
test('without measurements the panel says it does not know yet, in both languages',async()=>{
 const none=measured({reason:'insufficient_data',basis:null,lowerSeconds:null,upperSeconds:null,sampleCount:0});
 let f=await etaFor(none);assert.equal(f.text('eta'),'Сколько осталось — скажу после первой готовой задачи');
 assert.doesNotMatch(f.text('eta'),/\d/);
 f=await etaFor(none,'en');assert.match(f.text('eta'),/first finished task/);
 f=await etaFor(none);assert.match(f.text('etaRows'),/Проверка ветки: сколько займёт — пока не знаю/);
});
test('stages outside tasks get their own lines and a conditional repair says so',async()=>{
 const f=await etaFor(measured());const rows=f.text('etaRows');
 assert.match(rows,/Задачи: примерно 20–40 минут/);assert.match(rows,/Финальный ремонт: если понадобится — пока не знаю/);
 assert.match(rows,/Слепая приёмка: сколько займёт — пока не знаю/);
 f.el('language').change('en');assert.match(f.text('etaRows'),/Branch review: how long it takes is not known yet/);
});
test('a task that outlasts its class is "taking longer than usual" and the rest is counted after it',async()=>{
 const d=sample();d.estimate=measured({overrunning:['T01'],afterOverrun:true,lowerSeconds:1260,upperSeconds:2330});
 const f=await fixture('',d);
 assert.equal(f.text('eta'),'после этой задачи ещё примерно 20–40 минут');
 assert.match(f.text('waves'),/идёт дольше обычного/);assert.doesNotMatch(f.text('eta'),/осталась 1/);
 assert.match(f.text('etaNote'),/T01/);
 f.el('language').change('en');assert.match(f.text('waves'),/taking longer than usual/);
 assert.equal(f.text('eta'),'after this task about 20–40 minutes more');
 const alone=sample();alone.estimate=measured({overrunning:['T01'],afterOverrun:true,lowerSeconds:0,upperSeconds:0});
 const g=await fixture('',alone);assert.equal(g.text('eta'),'Идёт дольше обычного');
});
test('work is shown apart from idle and the owner wait has its own counter, and they tick',async()=>{
 const f=await etaFor(measured({work:{workSeconds:3590,idleSeconds:600,ownerWaitSeconds:120}}));
 assert.equal(f.el('workSplit').hidden,false);
 assert.match(f.text('workSplit'),/Работа/);assert.match(f.text('workSplit'),/Простой и ожидание/);assert.match(f.text('workSplit'),/Ожидание ответа/);
 assert.equal(f.text('workTime'),'59 мин 50 с');assert.equal(f.text('idleTime'),'10 мин 00 с');assert.equal(f.text('ownerWaitTime'),'2 мин 00 с');
 f.advance(5000);assert.equal(f.text('workTime'),'59 мин 55 с');
 f.advance(10000);assert.equal(f.text('workTime'),'1 ч 00 мин');assert.equal(f.text('idleTime'),'10 мин 00 с');assert.equal(f.text('ownerWaitTime'),'2 мин 00 с');
 f.el('language').change('en');assert.match(f.text('workSplit'),/Work/);assert.match(f.text('workSplit'),/Idle and waiting/);assert.match(f.text('workSplit'),/Waiting for an answer/);
});
test('while the owner is asked the work clock stops and the answer-wait counter runs',async()=>{
 const d=sample();d.estimate=measured();d.waiting={kind:'owner_question',note:'Нужно решение'};
 const f=await fixture('',d);f.advance(15000);
 assert.equal(f.text('workTime'),'1 ч 00 мин');assert.equal(f.text('ownerWaitTime'),'2 мин 15 с');
});
test('the work block is absent for older estimates and when the source is not live',async()=>{
 const old=await fixture();assert.equal(old.el('workSplit').hidden,true);
 const f=await etaFor(measured());await f.fail();assert.equal(f.el('workSplit').hidden,true);
});
test('a reused snapshot is as old as the server says, so the clocks catch up',async()=>{
 const d=sample();
 const f=await fixture('',{...d,calculation:{id:'x',state:'ready',startedAt:'2026-10-19T12:00:00Z'},
  snapshot:{capturedAt:'2026-10-19T12:00:00Z',freshness:'current',ageSeconds:120}});
 assert.equal(f.text('runElapsed'),'4 мин 00 с');assert.equal(f.text('time-T01'),'2 мин 30 с');
 assert.match(f.text('sourceLabel'),/Живые данные/,'An old reused snapshot must not read as lost connection');
 f.advance(1000);assert.match(f.text('sourceLabel'),/Живые данные/);
});
function modelRow(extra={}){
 return {id:'w1',role:'worker',roleLabel:'Исполнитель',routeClass:'worker',requestedModel:'claude-sonnet-5-5',requestedReasoning:'medium',
  actualModel:null,actualReasoning:null,actualModelStatus:'unknown',reasoningSupport:'supported',usage:null,selection:{},...extra};
}
test('each participant shows requested model and effort, honest unknowns and only measured usage',async()=>{
 const d=sample();d.models=[modelRow(),
  modelRow({id:'c1',role:'critic1',roleLabel:'Критик · круг 1',requestedModel:'claude-opus-5-5',requestedReasoning:'high',
   actualModel:'claude-opus-5-5',actualReasoning:'high',actualModelStatus:'observed',usage:{inputTokens:2000,outputTokens:300,measuredCalls:2}}),
  modelRow({id:'m1',role:'worker',roleLabel:'Исполнитель',requestedModel:'gpt-6-sol',requestedReasoning:'low',reasoningSupport:'unsupported'})];
 const f=await fixture('',d);const text=f.text('models');
 assert.equal(f.el('modelsPanel').hidden,false);
 assert.match(text,/claude-sonnet-5-5 · medium/);assert.match(text,/фактическая модель неизвестна/);
 assert.match(text,/claude-opus-5-5 · high/);assert.match(text,/фактически: claude-opus-5-5/);
 assert.match(text,/расход: неизвестно/);assert.match(text,/расход: 2000 вх\. \/ 300 исх\. токенов/);
 assert.match(text,/gpt-6-sol · усилие не применено/);assert.doesNotMatch(text,/gpt-6-sol · low/);
 f.el('language').change('en');
 assert.match(f.text('models'),/actual model unknown/);assert.match(f.text('models'),/effort not applied/);assert.match(f.text('models'),/usage: unknown/);
});
test('a model that differs from the request is flagged and an old run has no participants panel',async()=>{
 const d=sample();d.models=[modelRow({actualModel:'claude-haiku-5-5',actualModelStatus:'mismatch'})];
 const f=await fixture('',d);assert.match(f.text('models'),/не совпадает с запрошенной/);
 const old=await fixture();assert.equal(old.el('modelsPanel').hidden,true);
});
test('malformed participant rows cannot replace the last good snapshot',async()=>{
 const f=await fixture();const bad=sample();bad.title='Подмена';bad.models='not-a-list';
 await f.update(bad);assert.notEqual(f.text('title'),'Подмена');assert.match(f.text('sourceLabel'),/Нет связи/);
});
test('the number of collected findings is visible and old runs show none',async()=>{
 const d=sample();d.findings={open:3,confirmed:1,fixed:2,remaining:0};
 const f=await fixture('',d);assert.match(f.text('overviewCounts'),/Замечаний накоплено/);assert.match(f.text('overviewCounts'),/3/);
 f.el('language').change('en');assert.match(f.text('overviewCounts'),/Findings collected/);
 const old=await fixture();assert.doesNotMatch(old.text('overviewCounts'),/Замечаний/);
});
test('decisions tied to requirements are grouped under them and old decisions stay a plain list',async()=>{
 const d=sample();d.decisions=[{at:'2026-10-19T10:00:00Z',text:'Старое решение'},'Строковое решение',
  {at:'2026-10-19T10:01:00Z',text:'Храним в SQLite',requirements:['R01']},
  {at:'2026-10-19T10:02:00Z',text:'Общее для двух',requirements:['R01','A01']}];
 const f=await fixture('',d);const text=f.text('decisions');
 assert.match(text,/Старое решение/);assert.match(text,/Строковое решение/);
 assert.match(text,/R01 · Отправка формы/);assert.match(text,/A01 · Черновик/);
 const grouped=text.slice(text.indexOf('R01 · Отправка формы'));
 assert.match(grouped,/Храним в SQLite/);assert.match(grouped,/Общее для двух/);
 assert.ok(text.indexOf('Старое решение')<text.indexOf('R01 · Отправка формы'));
 const bad=sample();bad.decisions=[{text:'Решение',requirements:'R01'}];
 await f.update(bad);assert.match(f.text('sourceLabel'),/Нет связи/);
});
test('a hidden tab stops polling and the clock tick, and coming back asks at once',async()=>{
 const f=await fixture();assert.equal(f.tickIntervals(),1);assert.equal(f.timerCount(),1);
 const before=f.fetchCount();
 await f.setHidden(true);
 assert.equal(f.tickIntervals(),0,'The one-second tick kept running in a hidden tab');
 assert.equal(f.timerCount(),0,'A refresh timer was left armed in a hidden tab');
 f.advance(60000);assert.equal(f.fetchCount(),before);assert.equal(f.text('time-T01'),'30 с');
 await f.setHidden(false);
 assert.equal(f.fetchCount(),before+1,'Coming back must request at once');
 assert.equal(f.tickIntervals(),1);assert.equal(f.timerCount(),1);
});
test('a request that finishes while the tab is hidden does not rearm polling',async()=>{
 const f=await fixture();f.holdNext();f.el('refresh').click();await f.settle();assert.equal(f.activeRequests(),1);
 await f.setHidden(true);await f.release();
 assert.equal(f.timerCount(),0);
 await f.setHidden(false);assert.equal(f.maxActiveRequests(),1);
});
test('replies of calculating keep saying the snapshot is being calculated and never become "no connection"',async()=>{
 const d=sample(),f=await fixture('',pendingSnapshot(d));
 for(let i=0;i<8;i++){await f.nextTimer();
  assert.match(f.text('sourceLabel'),/Снимок считается/);assert.doesNotMatch(f.text('sourceLabel'),/Нет связи|актуальных данных/);}
 f.advance(60000);assert.match(f.text('sourceLabel'),/Снимок считается/,'A long calculation must not read as lost connection');
});
test('"no connection" needs 20 seconds without any HTTP reply, and a quick failure still shows it',async()=>{
 const f=await fixture();await f.fail();assert.match(f.text('sourceLabel'),/Нет связи/);
 const g=await fixture();g.advance(19999);assert.match(g.text('sourceLabel'),/Живые данные/);
 g.advance(2);assert.match(g.text('sourceLabel'),/Нет актуальных данных/);
});
