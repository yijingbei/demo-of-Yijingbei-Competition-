'use strict';
const template=JSON.parse(document.getElementById('player-template').textContent);
document.getElementById('player-template').textContent='';
const byId=id=>document.getElementById(id),records=[],seen=new Set();
let generation=0,current=-1,frame=null;
const htmlEscape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function unload(){if(frame){frame.remove();frame=null}byId('empty').hidden=false}
function errorMessage(text){byId('error').textContent=text;byId('error').hidden=!text}
function renderList(){
 byId('matches').replaceChildren();
 records.forEach((entry,i)=>{const b=document.createElement('button');b.type='button';b.dataset.index=i;b.setAttribute('aria-pressed',String(i===current));b.textContent=entry.label;
  const small=document.createElement('small');small.textContent=(entry.file.size/1024/1024).toFixed(2)+' MiB · '+entry.path;b.appendChild(small);b.addEventListener('click',()=>loadRecord(i));byId('matches').appendChild(b)});
 byId('status').textContent=records.length+' 个回放文件 · 一次仅解压一局';
}
async function inflateBytes(bytes){return await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text()}
async function readPack(file){
 if(/^(?:trace|replay\.frames)\.jsonl(?:\.gz)?$/i.test(file.name))throw new Error('原始日志需先用 replay 命令转换为回放数据包');
 const bytes=new Uint8Array(await file.arrayBuffer());let text;
 if(bytes[0]===31&&bytes[1]===139)text=await inflateBytes(bytes);else text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
 if(file.name.toLowerCase().endsWith('.html')){const doc=new DOMParser().parseFromString(text,'text/html'),node=doc.getElementById('replay-data');if(!node)throw new Error('此 HTML 不包含回放记录');text=node.type==='application/gzip-base64'?await inflateBytes(Uint8Array.from(atob(node.textContent.trim()),c=>c.charCodeAt(0))):node.textContent}
 const pack=JSON.parse(text);
 if(pack.schema!=='competition-multiscene-replay@1'||!pack.episodes?.length||!Array.isArray(pack.scenarios))throw new Error('请选择回放数据包，原始日志需先用 replay 命令转换');
 if(pack.lazyEpisodes){const first=pack.episodes[0];pack.episodes[0]={...JSON.parse(await inflateBytes(Uint8Array.from(atob(first.lazyData),c=>c.charCodeAt(0)))),lazyData:first.lazyData}}
 if(!pack.episodes[0].entities?.length)throw new Error('回放缺少实体数据');return pack;
}
async function loadRecord(index){
 const token=++generation;current=index;errorMessage('');renderList();unload();
 byId('status').textContent='正在解压所选对局…';
 try{const entry=records[index],pack=await readPack(entry.file);if(token!==generation)return;
  entry.label=pack.episodes[0].meta.label||entry.path;
  const values={TITLE:htmlEscape('自己的实验 · 回放'),SUMMARY:pack.scenarios.length+' 个场景 · '+pack.episodes.length+' 局记录',
   OPTIONS:pack.episodes.map((e,i)=>'<option value="'+i+'">'+htmlEscape(e.meta.label||e.meta.scenario)+'</option>').join(''),
   REPLAY_DATA:JSON.stringify(pack).replace(/</g,'\\u003c')};
  frame=document.createElement('iframe');frame.id='match-player';frame.title='选中对局的三维回放';frame.setAttribute('sandbox','allow-scripts allow-downloads');frame.allowFullscreen=true;
  frame.srcdoc=template.replace(/\{\{(TITLE|SUMMARY|OPTIONS|REPLAY_DATA)\}\}/g,(_,key)=>values[key]);
  byId('empty').hidden=true;byId('main').appendChild(frame);renderList();
 }catch(error){if(token!==generation)return;errorMessage('无法打开：'+error.message);byId('status').textContent='记录加载失败；请保留原始结果检查'}
}
function addFiles(files,folder=false){
 errorMessage('');const added=[];
 for(const file of files){const path=file.webkitRelativePath||file.name;if(folder&&!/^replay\.pack\.json(?:\.gz)?$/i.test(file.name))continue;
  if(!folder&&!/\.(json|gz|html)$/i.test(file.name))continue;
  const key=path+'|'+file.size+'|'+file.lastModified;if(seen.has(key))continue;seen.add(key);
  added.push(records.length);records.push({file,path,label:path.replace(/\/replay\.pack\.json\.gz$/,'')});
 }
 if(!added.length){errorMessage('没有找到新的 replay.pack.json.gz 回放数据；请先完成实验或使用 replay 命令导出');return}
 renderList();const wanted=new URLSearchParams(location.search).get('case');
 const choice=wanted?added.find(i=>records[i].path.split('/').includes(wanted)):undefined;
 loadRecord(choice??added[0]);
}
byId('pick-files').addEventListener('change',event=>{addFiles(event.target.files);event.target.value=''});
byId('pick-folder').addEventListener('change',event=>{addFiles(event.target.files,true);event.target.value=''});
byId('clear').addEventListener('click',()=>{++generation;current=-1;unload();errorMessage('');renderList()});
for(const type of ['dragenter','dragover'])document.addEventListener(type,event=>{event.preventDefault();document.body.classList.add('dragging')});
document.addEventListener('dragleave',event=>{if(!event.relatedTarget)document.body.classList.remove('dragging')});
document.addEventListener('drop',event=>{event.preventDefault();document.body.classList.remove('dragging');addFiles(event.dataTransfer.files)});
