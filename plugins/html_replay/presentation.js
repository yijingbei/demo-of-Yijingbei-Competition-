/* Presentation only: preserve raw pack, IDs, coordinates, scores and downloads. */
const DISPLAY_TERMS=__DISPLAY_TERMS__;
const DISPLAY_PATTERNS=[["command[ _-]?post", "coordination-center"], ["combined[ _-]?arms", "multi-domain"], ["surface[ _-]?raid", "surface-control"], ["deep[ _-]?strike", "dual-area-control"], ["island[ _-]?strike", "island-control"], ["dual[ _-]?axis[ _-]?pincer", "two-axis-control"], ["(?<![a-z])(?:radar|sam|aam|asm|ciws)(?![a-z])", "sensor-device"], ["(?<![a-z])(?:headquarters|hq|arsenal)(?![a-z])", "coordination-center"], ["(?<![a-z])(?:missiles?|warheads?|weapons?|munitions?|ammo|ammunition|torpedoes?|bombs?|cannons?|artillery)(?![a-z])", "response-item"], ["(?<![a-z])(?:suicide|kamikaze|military|combat|warfare|battle|strike|raid|attack|attacker|threat|saturation|destroyer|frigate|fighter|bomber)(?![a-z])", "patrol-event"]];
function displayText(value){let text=String(value??'');for(const [old,replacement] of DISPLAY_TERMS)text=text.split(old).join(replacement);for(const [pattern,replacement] of DISPLAY_PATTERNS)text=text.replace(new RegExp(pattern,"gi"),replacement);return text}
function sanitizeDisplay(root){
 if(!root)return;
 if(root.nodeType===Node.TEXT_NODE){if(!root.parentElement?.closest('script,style,textarea,#public-task')){const text=displayText(root.nodeValue);if(text!==root.nodeValue)root.nodeValue=text}return}
 if(root.nodeType!==Node.ELEMENT_NODE&&root.nodeType!==Node.DOCUMENT_NODE)return;
 if(root.nodeType===Node.ELEMENT_NODE){if(root.matches('script,style,textarea'))return;for(const attr of ['title','aria-label','placeholder','alt','label'])if(root.hasAttribute(attr)){const value=root.getAttribute(attr),text=displayText(value);if(text!==value)root.setAttribute(attr,text)}}
 for(const child of root.childNodes)sanitizeDisplay(child);
}
sanitizeDisplay(document.documentElement);
new MutationObserver(records=>{for(const r of records){if(r.type==='characterData'||r.type==='attributes')sanitizeDisplay(r.target);else for(const n of r.addedNodes)sanitizeDisplay(n)}}).observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['title','aria-label','placeholder','alt','label']});
const STRATEGY_NAMES={rule:'规则脚本',script:'场景脚本',llm:'大模型',hybrid:'混合架构',large_small:'大小模型协同',unknown:'未记录'};
const MODE_NAMES={idle:'闲置',naive:'朴素',coordinated:'固定协同',greedy:'就近贪心',adaptive:'自适应'};
function strategyInfo(d,side){
 const declared=d.meta.strategies?.[side];
 if(declared)return declared;
 const mode=side===(d.meta.participantSide||'red')?d.meta.policy:'unknown';
 return {kind:MODE_NAMES[mode]?'rule':'unknown',mode:mode||'unknown',difficulty:'unknown',driver:'unknown',source:mode?'旧记录 policy':'not_recorded'};
}
function valueLabel(v){return !v||v==='unknown'?'未记录':v==='ie_attack'?'IE场景脚本':displayText(v)}
function strategyLabel(d,side){const s=strategyInfo(d,side);return (STRATEGY_NAMES[s.kind]||valueLabel(s.kind))+' · '+(MODE_NAMES[s.mode]||valueLabel(s.mode))}
function blueSignature(d){const s=strategyInfo(d,'blue');return JSON.stringify([s.kind,s.mode,s.difficulty,s.driver,s.large_model,s.small_model])}
function mountStrategyPanel(){
 for(const side of ['red','blue']){
  const s=strategyInfo(DATA,side),target=$('strategy-'+side);target.replaceChildren();
  const title=document.createElement('strong');title.textContent=sideName(side)+'｜'+strategyLabel(DATA,side);target.appendChild(title);
  const details=document.createElement('p');details.textContent='脚本 / 驱动：'+valueLabel(s.driver)+'；该方难度：'+valueLabel(s.difficulty)+(s.large_model?'；大模型：'+valueLabel(s.large_model):'')+(s.small_model?'；小模型：'+valueLabel(s.small_model):'');target.appendChild(details);
  const provenance=document.createElement('small');provenance.textContent='信息来源：'+valueLabel(s.source)+(s.declaration_only?'；仅为展示声明，未核验实际模型调用':'');target.appendChild(provenance);
 }
 const rows=$('strategy-comparison-body');rows.replaceChildren();
 const current=DATA.meta;
 PACK.episodes.forEach((episode,i)=>{
  const m=episode.meta;if(m.scenario!==current.scenario)return;
  const tr=document.createElement('tr');tr.classList.toggle('selected',i===episodeIndex);
  const action=document.createElement('button');action.type='button';action.textContent=i===episodeIndex?'当前局':'查看';action.addEventListener('click',()=>switchEpisode(i));
  const cell=document.createElement('td');cell.appendChild(action);tr.appendChild(cell);
  const same=Number.isFinite(m.seed)&&m.seed===current.seed&&m.runtimeSha256&&m.runtimeSha256===current.runtimeSha256&&JSON.stringify(m.sourceFiles)===JSON.stringify(current.sourceFiles)&&blueSignature(episode)===blueSignature(DATA);
  for(const value of [strategyLabel(episode,'red'),strategyLabel(episode,'blue'),valueLabel(strategyInfo(episode,'blue').difficulty),m.seed??'未记录',scoreText(m.score),outcomeName(episode),
      Number.isFinite(m.finalOperational)?m.finalOperational+'/'+m.facilityCount:'未记录',
      Number.isFinite(m.redShots)&&Number.isFinite(m.blueShots)?m.redShots+' / '+m.blueShots:'未记录',
      m.end??'未记录',same?'已知条件一致，仍需核对完整配置':'条件不同或信息不足']){
   const td=document.createElement('td');td.textContent=value;tr.appendChild(td);
  }rows.appendChild(tr);
 });
}

for(const name of ["fillText","strokeText"]){const original=CanvasRenderingContext2D.prototype[name];CanvasRenderingContext2D.prototype[name]=function(text,...args){return original.call(this,displayText(text),...args)}}
