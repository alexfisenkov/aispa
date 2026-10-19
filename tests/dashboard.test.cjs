// Black-box tests of the delivered inline script, without an extra DOM dependency.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../skills/aispa/assets/dashboard.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];

class Element {
 constructor(tag='div',text=''){this.tag=tag;this._text=text;this.children=[];this.style={};this.events={};this.attributes={};this.className='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this._text='';this.children=[...items];}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
 set innerHTML(value){throw new Error('Unsafe HTML assignment');}
 addEventListener(name,fn){this.events[name]=fn;}
 setAttribute(name,value){this.attributes[name]=value;}
 click(){return this.events.click?.();}
}
function descend(el,id){if(el.id===id)return el;for(const child of el.children){if(typeof child==='object'){const found=descend(child,id);if(found)return found;}}return null;}
async function fixture(search='',data=sample()){
 const elements={};for(const match of html.matchAll(/\bid="([^"]+)"/g)){elements[match[1]]=new Element();elements[match[1]].id=match[1];}
 const intervals=[];let response=data,error=null,clock=0,calls=0;
 const document={getElementById(id){return elements[id]||Object.values(elements).map(e=>descend(e,id)).find(Boolean)||null;},
  createElement(tag){return new Element(tag);},createTextNode(text){return new Element('#text',String(text));}};
 const context={document,location:{search},URLSearchParams,Date,Set,Array,Number,AbortController,
  performance:{now:()=>clock},setInterval:fn=>intervals.push(fn),setTimeout:()=>1,clearTimeout(){},
  fetch:async()=>{calls++;if(error)throw Error('network');return {ok:true,json:async()=>response};}};
 vm.runInNewContext(script,context);
 async function settle(){for(let i=0;i<8;i++)await Promise.resolve();}
 await settle();
 return {el:id=>document.getElementById(id),text:id=>document.getElementById(id).textContent,
  async update(data){response=data;error=null;await elements.refresh.click();await settle();},
  async fail(){error=true;await elements.refresh.click();await settle();},
  advance(ms){clock+=ms;intervals.forEach(fn=>{if(fn.name==='tick')fn();});},
  fetchCount:()=>calls};
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
test('stale task proof is visibly awaiting a new check',async()=>{
 const d=sample();d.requirements[0].implementation='needs_check';
 const f=await fixture('',d);assert.match(f.text('requirements'),/Нужна повторная проверка/);
});
test('inherited unfinished work is visible independently of current tasks',async()=>{
 const d=sample();d.remaining.inherited=[{id:'C-example',status:'outstanding',text:'Экспорт из прошлого прогона'}];
 const f=await fixture('',d);assert.match(f.text('remaining'),/Остатки прошлых прогонов/);assert.match(f.text('remaining'),/Экспорт из прошлого прогона/);
});
