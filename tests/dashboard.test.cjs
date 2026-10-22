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
 click(){return this.events.click?.();}
 change(value){this.value=value;return this.events.change?.call(this);}
}
function descend(el,id){if(el.id===id)return el;for(const child of el.children){if(typeof child==='object'){const found=descend(child,id);if(found)return found;}}return null;}
async function fixture(search='',data=sample(),options={}){
 const elements={};for(const match of html.matchAll(/\bid="([^"]+)"/g)){elements[match[1]]=new Element();elements[match[1]].id=match[1];}
 const staticEls=[];for(const match of html.matchAll(/<[^>]*\bdata-i18n="([^"]+)"[^>]*>/g)){
  const el=new Element();el.setAttribute('data-i18n',match[1]);staticEls.push(el);
 }
 const intervals=[];let response=data,error=null,clock=0,calls=0;
 const document={getElementById(id){return elements[id]||Object.values(elements).map(e=>descend(e,id)).find(Boolean)||null;},
  createElement(tag){return new Element(tag);},createTextNode(text){return new Element('#text',String(text));},
  documentElement:new Element('html'),querySelectorAll(selector){return selector==='[data-i18n]'?staticEls:[];}};
 const context={document,location:{search},URLSearchParams,Date,Set,Array,Number,AbortController,
  performance:{now:()=>clock},setInterval:fn=>intervals.push(fn),setTimeout:()=>1,clearTimeout(){},
  window:{AISPA_ARCHIVE:options.archive,matchMedia:()=>({matches:!!options.systemDark,addEventListener(){}})},
  fetch:async()=>{calls++;if(error)throw Error('network');return {ok:true,json:async()=>response};}};
 if(options.storage)context.localStorage=options.storage;
 vm.runInNewContext(script,context);
 async function settle(){for(let i=0;i<8;i++)await Promise.resolve();}
 await settle();
 return {el:id=>document.getElementById(id),text:id=>document.getElementById(id).textContent,
  staticText:key=>staticEls.find(el=>el.getAttribute('data-i18n')===key)?.textContent,
  htmlElement:document.documentElement,
  async update(data){response=data;error=null;await elements.refresh.click();await settle();},
  async fail(){error=true;await elements.refresh.click();await settle();},
  advance(ms){clock+=ms;intervals.forEach(fn=>{if(fn.name==='tick')fn();});},
  fetchCount:()=>calls,intervalCount:()=>intervals.length};
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
 const f=await fixture();assert.match(f.text('currentActivity'),/Форма/);assert.equal(f.text('active-time-T01'),'30 с');
 f.advance(2000);assert.equal(f.text('active-time-T01'),'32 с');assert.equal(f.text('runElapsed'),'2 мин 02 с');
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
 const f=await fixture();f.advance(16000);assert.equal(f.text('label'),'Состояние не подтверждено');assert.equal(f.text('time-T01'),'30 с');
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
 assert.match(f.text('currentActivity'),/Сейчас: Правка/);assert.match(f.text('currentActivity'),/Вся задача/);
 f.advance(2000);assert.equal(f.text('phase-time-T01'),'10 с');assert.equal(f.text('active-phase-time-T01'),'10 с');
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
 const f=await fixture('',d);const row=f.el('waves').children[0].children[1].children[0].children[1].children[2];
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
 const f=await fixture('',d);assert.match(f.text('waves'),/Нужна повторная проверка/);
 assert.match(f.text('waves'),/0 из 1 задач принято/);
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
 const t02=f.el('waves').children[1].children[1].children[0].children[1].children[2];
 assert.equal(t02.children[2].attributes['aria-label'],'Правка — не требуется');
 assert.equal(t02.children[3].attributes['aria-label'],'Второй критик — не требуется');
 const accepted=await fixture('?demo&status=accepted');
 let skipped=0,reviewed=0;
 for(const wave of accepted.el('waves').children)for(const task of wave.children[1].children){
  const steps=task.children[1].children[2];
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
 assert.match(f.text('waves'),/1 из 1 задач принято · на дату снимка/);
 assert.match(f.text('waves'),/Задача принята · на дату снимка/);
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
 assert.match(f.text('waves'),/1 из 1 задач принято · последний снимок/);
 assert.match(f.text('waves'),/Задача принята · последний снимок/);
 const demo=await fixture('?demo&status=accepted');
 assert.match(demo.text('label'),/пример/);assert.match(demo.text('waves'),/Задача принята · пример/);
 const stale=sample('stale');stale.tasks[0].status='completed';stale.tasks[0].proofCurrent=true;
 const s=await fixture('',stale);
 assert.match(s.text('label'),/устарела/);assert.match(s.text('waves'),/Задача принята/);
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
 assert.match(f.text('waves'),/Лёгкая задача/);assert.match(f.text('waves'),/1 из 1 выполнено и проверено/);
 assert.doesNotMatch(f.text('waves'),/Задача принята|задач принято/);assert.equal(f.text('label'),'В работе');
 d.tasks[0].proofCurrent=false;await f.update(d);assert.match(f.text('waves'),/Нужна повторная проверка/);
 assert.match(f.text('waves'),/0 из 1 выполнено и проверено/);
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
 assert.match(text,/Тяжёлая задача · PASS критика закрывает задачу/);assert.match(text,/Задача принята/);
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
