// Nominal reference envelopes. Enemy contacts never acquire truth-backed profiles.
const rangeSettings={enabled:false,uav:true,usv:true,shore:true,sensor:true,weapon:false,selected:true};
const rangeLayers=new Map();
let rangeNoteCache='';
function rangeKey(r){return r.type==='radar'?r.category:r.type==='weapon'?'weapon':'sensor'}
function rangeDistance(m){return m>=1000?Number((m/1000).toFixed(2))+' km':Number(m.toFixed(1))+' m'}
function rangeName(r){
 if(r.type!=='weapon')return r.type==='radar'?'雷达':r.sensorKind==='eo_ir'?'光电':'其他传感器';
 const id=String(r.ref||'').split('@')[0];
 if(id==='weapon.loitering-munition'||id.startsWith('weapon.loitering-munition.'))return '巡飞弹';
 if(id==='weapon.interceptor-missile'||id==='weapon.air-interceptor-munition')return '空空拦截弹';
 if(id==='weapon.surface-missile')return '对海导弹';
 if(id==='weapon.suicide-warhead'||id.startsWith('weapon.suicide-warhead.'))return '自杀艇战斗部';
 return r.deliveryModel==='guided_missile'?'制导导弹':'武器';
}
function rangeEligible(e){
 const st=displayedState(e);
 return rangeSettings.enabled&&visibleInPerspective(e)&&st.health>0&&['active','degraded'].includes(st.life)&&(!rangeSettings.selected||e.id===selected);
}
function circleLine(radius,color,dashed=false){
 const pts=Array.from({length:193},(_,i)=>{const a=i/192*Math.PI*2;return new THREE.Vector3(Math.cos(a)*radius,.003,Math.sin(a)*radius)});
 const options={color,transparent:true,opacity:.72,depthWrite:false};
 const material=dashed?new THREE.LineDashedMaterial({...options,dashSize:Math.max(.008,radius*.035),gapSize:Math.max(.005,radius*.022)}):new THREE.LineBasicMaterial(options);
 const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),material);if(dashed)line.computeLineDistances();return line;
}
function buildRangeOverlays(){
 rangeLayers.clear();rangeNoteCache='';
 for(const e of DATA.entities){const layers=[];
  for(const r of e.ranges||[]){
   if(!Number.isFinite(r.maxM)||r.maxM<=0||!Number.isFinite(r.minM)||r.minM<0||r.minM>r.maxM)continue;
   const group=new THREE.Group(),color=COLOR[e.side],radius=r.maxM/1000,minimum=r.minM/1000;
   group.name='nominal-range';group.visible=false;group.userData={entityId:e.id,side:e.side,category:rangeKey(r),ref:r.ref,maxM:r.maxM,minM:r.minM};
   const outline=circleLine(radius,color,r.type!=='radar');group.add(outline);
   const fill=new THREE.Mesh(new THREE.RingGeometry(minimum,radius,192),new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide,transparent:true,opacity:r.type==='weapon'?.018:.022,depthWrite:false}));
   fill.rotation.x=-Math.PI/2;group.add(fill);
   if(minimum>0){const blind=circleLine(minimum,color,true);blind.name='minimum-range';group.add(blind)}
   if(r.altitudeProfile&&r.altitudeProfile.low_altitude_range_m!==r.maxM){const low=circleLine(r.altitudeProfile.low_altitude_range_m/1000,color,true);low.name='low-altitude-range';group.add(low)}
   const label=document.createElement('div');label.className='range-label '+e.side;label.hidden=true;
   label.textContent=rangeName(r)+' 上限 '+(minimum>0?rangeDistance(r.minM)+'–':'')+rangeDistance(r.maxM);
   $('labels').appendChild(label);world.add(group);layers.push({group,label,profile:r,outline,fill});
  }rangeLayers.set(e.id,layers);
 }mountRangeControls();
}
function mountRangeControls(){
 for(const key of Object.keys(rangeSettings)){
  const input=$('range-'+key);input.checked=rangeSettings[key];
  const exists=key==='enabled'||key==='selected'||DATA.entities.some(e=>(perspective==='god'||e.side===perspective)&&(e.ranges||[]).some(r=>rangeKey(r)===key));
  input.disabled=!exists;input.parentElement.classList.toggle('unavailable',!exists);
  input.parentElement.title=exists?'标称资源范围参考圈':'当前视角没有此类范围数据';
 }
 const mode=!rangeSettings.enabled?'hidden':rangeSettings.selected?'selected':'all';
 for(const value of ['hidden','selected','all'])$('range-mode-'+value).setAttribute('aria-pressed',String(value===mode));
}
function setRangeMode(mode){
 rangeSettings.enabled=mode!=='hidden';
 if(rangeSettings.enabled)rangeSettings.selected=mode==='selected';
 mountRangeControls();updateRangeOverlays();
}
function updateRangeOverlays(){
 let visible=0;
 for(const e of DATA.entities){const allowed=rangeEligible(e);let p=null;
  for(const layer of rangeLayers.get(e.id)||[]){
   const show=allowed&&!!rangeSettings[rangeKey(layer.profile)];layer.group.visible=show;layer.label.hidden=true;
   if(!show)continue;if(!p)p=displayedSample(e);if(!p){layer.group.visible=false;continue}visible++;
   // A sea-level reference footprint uses true horizontal metres, never exaggerated altitude.
   layer.group.position.set(p[0]/1000,.17,-p[1]/1000);
   layer.outline.material.opacity=e.id===selected?.85:.48;
   layer.fill.visible=e.id===selected;
   if(e.id===selected){const radius=layer.profile.maxM/1000,angle=layer.profile.type==='weapon'?.6:layer.profile.type==='radar'?0:-.6;
    const a=layer.group.position.clone().add(new THREE.Vector3(Math.cos(angle)*radius,.02,Math.sin(angle)*radius));a.project(camera);
    if(a.z>=-1&&a.z<=1&&Math.abs(a.x)<.98&&Math.abs(a.y)<.92){layer.label.hidden=false;layer.label.style.left=((a.x+1)/2*W)+'px';layer.label.style.top=((-a.y+1)/2*H)+'px'}
   }
  }
 }
 const info=DATA.rangeInfo;
 const available=DATA.entities.filter(e=>perspective==='god'||e.side===perspective).some(e=>(e.ranges||[]).length);
 const note=!available?(info?.status==='unavailable'?info.reason:'本局当前视角没有范围资源；旧记录需使用对应版本源码重新导出。'):
  (!rangeSettings.enabled?'范围圈已隐藏':rangeSettings.selected?'仅所选单元：显示 '+visible+' 个标称上限圈':'全部可见单元：显示 '+visible+' 个标称上限圈')+'；探测还受目标高度、视距、地形、天气、概率与刷新周期限制。内侧雷达虚线为低空上限，武器内圈为最小射距 · '+(perspective==='god'?sideName('blue')+' / '+sideName('red'):'仅显示本方范围，敌方触点不展示真实能力')+'。';
 if(note!==rangeNoteCache){$('range-status').textContent=note;rangeNoteCache=note}
 $('range-fit').disabled=!selectedRangeLayers().length;
}
function selectedRangeLayers(){return (rangeLayers.get(selected)||[]).filter(l=>l.group.visible)}
function fitSelectedRanges(){
 const e=E.get(selected),layers=selectedRangeLayers();if(!e||!layers.length)return;
 stopCameraAutomation();const p=displayedSample(e),radius=Math.max(...layers.map(l=>l.profile.maxM))/1000;
 targetOrbit.center.copy(point(p)).setY(0);targetOrbit.span=Math.max(1,2.5*radius*Math.max(1,W/H));
}
function updateRangeInspector(){
 const e=E.get(selected),target=$('inspect-ranges');target.replaceChildren();
 if(!e||!visibleInPerspective(e))return;
 const profiles=e.ranges||[];
 if(!profiles.length){target.textContent=DATA.rangeInfo?.status==='unavailable'?'本局范围参数未保存或版本未核实':'未配置可显示的探测器或武器';return}
 for(const r of profiles){const row=document.createElement('div');row.className='range-detail';
  const name=document.createElement('span');name.textContent=rangeName(r);
  const value=document.createElement('b');value.textContent=(r.minM>0?rangeDistance(r.minM)+'–':'')+rangeDistance(r.maxM);
  const detail=document.createElement('small');detail.textContent=r.ref+(r.type==='weapon'?' · 目标域 '+(r.targetDomains.join(' / ')||'不限'):' · 周期 '+r.updateTicks+' tick');
  if(Number.isFinite(r.cruiseMps))detail.textContent+=' · 巡航水平速度 '+rangeDistance(r.cruiseMps)+'/s'+(Number.isFinite(r.launchMps)?' · 初速 '+r.launchMps+' m/s':'');
  if(r.altitudeProfile)detail.textContent+=' · 低空 ≤'+r.altitudeProfile.low_altitude_ceiling_m+' m AGL：'+rangeDistance(r.altitudeProfile.low_altitude_range_m);
  if(r.visibilityProfile)detail.textContent+=' · 天线高 '+r.visibilityProfile.mount_height_m+' m；视距依目标高度变化'+(r.visibilityProfile.terrain_occlusion?'；地形可遮挡':'');
  row.append(name,value,detail);target.appendChild(row);
 }
}
function bindRangeControls(){
 for(const key of Object.keys(rangeSettings))$('range-'+key).addEventListener('change',event=>{
  rangeSettings[key]=event.target.checked;
  if(!['enabled','selected'].includes(key)&&event.target.checked)rangeSettings.enabled=true;
  mountRangeControls();updateRangeOverlays();
 });
 for(const mode of ['hidden','selected','all'])$('range-mode-'+mode).addEventListener('click',()=>setRangeMode(mode));
 $('range-fit').addEventListener('click',fitSelectedRanges);
}
