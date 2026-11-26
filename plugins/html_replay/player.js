'use strict';
const PACK=JSON.parse(document.getElementById('replay-data').textContent);
document.getElementById('replay-data').textContent='';
let episodeIndex=0,DATA=PACK.episodes[0];
if(DATA.meta.participantOnly){
 perspective=participantSide();document.body.classList.add('faction-view');
 for(const side of ['god','red','blue'])document.getElementById('view-'+side).hidden=side!==participantSide();
}
const $=id=>document.getElementById(id);
const COLOR={blue:0x62dfec,red:0xff8877,neutral:0xe9ce91,gold:0xe9ce91,disabled:0x617580,decoy:0xd7b3f2};
const POLICY_NAMES={idle:'闲置',naive:'朴素',coordinated:'固定协同',greedy:'贪心',adaptive:'自适应',identify:'识别后拦截',identify_reallocate:'识别与余弹分配',attack_all:'见目标就攻击',speed:'按速度筛选',distance:'按距离筛选',probe:'反复试射未知目标'};
let policyFilter='all', finalBuoyLosses=null;
function policyName(id){return displayText((POLICY_NAMES[id]||STRATEGY_NAMES[id]||id)+' / '+id)}
function episodeOrigin(index){return PACK.recordingOrigins?.[index]?.label||''}
function filteredEpisodeIndices(scenario=DATA.meta.scenario){return PACK.episodes.map((e,i)=>e.meta.scenario===scenario&&(policyFilter==='all'||e.meta.policy===policyFilter)?i:-1).filter(i=>i>=0)}
let E=new Map(DATA.entities.map(e=>[e.id,e]));
const fmt=t=>{const seconds=t*tickSeconds();return String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(Math.floor(seconds%60)).padStart(2,'0')};
let END=DATA.meta.end;
function makeChapters(d){
 if(!isCombat(d))return taskChapters(d);
 const scenario=PACK.scenarios.find(s=>s.id===d.meta.scenario),chap=[];
 const add=(t,name,title,copy,ids=[])=>{if(t>=0&&t<=d.meta.end)chap.push({t,name,title,copy,...focusFor(d,ids,t)})};
 add(0,'初始部署',scenario.name+' · 防御展开',scenario.description+' 本局共 '+d.meta.plannedThreats+' 个武装威胁'+(d.meta.plannedDecoys?'，另有 '+d.meta.plannedDecoys+' 个诱饵':'')+'；己方 '+d.meta.combatUnits+' 个作战单元。');
 const first=d.shots[0];if(first)add(first.t,'首次交火','双方开始交战','此后的发射、命中和战损均来自原始记录。',[first.a,first.b]);
 const enemy=d.shots.find(s=>s.side==='blue');if(enemy&&(!first||enemy.t>first.t+12))add(enemy.t,'蓝方开火','蓝方进攻取得射击窗口','目标：'+d.entities.find(e=>e.id===enemy.b).name+'。发射后，已升空导弹继续独立结算。',[enemy.a,enemy.b]);
 const waves=new Map();for(const e of d.entities)if(e.spawn>0&&e.spawn<=d.meta.end){if(!waves.has(e.spawn))waves.set(e.spawn,[]);waves.get(e.spawn).push(e.id)}
 for(const [t,ids] of waves)add(t,'后续波次',fmt(t)+' · 后续威胁入场',ids.length+' 个单元按场景计划加入。己方要同时处理在场威胁并保留后续火力。',ids);
 const loss=d.events.find(ev=>ev.type==='loss'&&d.entities.some(e=>e.id===ev.a&&e.kind!=='facility'));
 if(loss)add(loss.t,'己方战损',loss.title,loss.detail,[loss.a]);
 const targets=new Set(d.entities.filter(e=>e.protected).map(e=>e.id));
 const attack=d.shots.find(s=>s.side==='blue'&&targets.has(s.b));
 if(attack)add(attack.t,'设施遭攻击','敌方向保护设施发射','攻击目标：'+d.entities.find(e=>e.id===attack.b).name+'。可在接下来的命中事件中查看实际损伤。',[attack.a,attack.b]);
 for(const ev of d.events.filter(e=>e.type==='facility-hit'))add(ev.t,'设施受损',ev.title,ev.detail,[ev.a]);
 const neutral=d.events.filter(e=>e.type==='neutralized').at(-1);
 if(neutral)add(neutral.t,'末轮拦截',neutral.title,'武装威胁的最后一条中和记录。剩余威胁与设施状态由最终裁判结果确认。',[neutral.a]);
 if(d.meta.quietAfter<d.meta.end-5)add(d.meta.quietAfter,'等待结算','交战记录结束，等待原始终局','保留原始终局时间；精华导览在这段没有新增发射、毁伤与波次的区间加速。');
 if(d.meta.participantOnly)add(d.meta.end,'观察结束','授权观察记录结束','此文件未包含任务成败、隐藏目标状态和裁判分数。');
 else add(d.meta.end,'裁判结算',d.meta.success?'任务成功 · 裁判结算':outcomeName(d)+' · 裁判记录','已停止威胁（含自爆） '+d.meta.finalNeutral+' / '+d.meta.plannedThreats+'；可用设施 '+d.meta.finalOperational+' / '+d.meta.facilityCount+'。');
 return chap.sort((a,b)=>a.t-b.t);
}

let chapters=makeChapters(DATA);
let time=0,playing=false,speed=1,selected=initialSelection(DATA),following=false,tour=false,showTrails=true,chapterIndex=-1;
let scene,camera,renderer,world,sea,raycaster,labels=[],ships=[],shotMeshes=[],hitMeshes=[],orbit={theta:.12,phi:.75,span:26,center:new THREE.Vector3(5.2,0,0)},targetOrbit={theta:.12,phi:.75,span:26,center:new THREE.Vector3(5.2,0,0)};
const renderScene=$('scene'),viewport=$('viewport'),meshes=new Map(),vec=new THREE.Vector3();
let W=1000,H=580,previous=performance.now(),lastUI=-1,lastEventIndex=-1,drag=null,wasDrag=false,hoverEntity=null;
function point(p){return new THREE.Vector3(p[0]/1000,p[2]/1000*2.2+.05,-p[1]/1000)}
function stateAt(e,t){let s=e.states[0];for(const x of e.states){if(x[0]>t)break;s=x}return {health:s[1],life:s[2]}}
function sampleAt(e,t){const a=e.samples;let lo=0,hi=a.length-1;if(t<=a[0][0])return a[0].slice(1);if(t>=a[hi][0])return a[hi].slice(1);while(hi-lo>1){const m=(hi+lo)>>1;if(a[m][0]<=t)lo=m;else hi=m}if(frameMode)return a[lo].slice(1);const f=(t-a[lo][0])/(a[hi][0]-a[lo][0]);let result=a[lo].slice(1);for(let j=0;j<3;j++)result[j]=a[lo][j+1]+(a[hi][j+1]-a[lo][j+1])*f;
 if(smoothDisplay&&e.source!=='anchors'){const p=[a[Math.max(0,lo-1)],a[lo],a[hi],a[Math.min(a.length-1,hi+1)]].map(s=>new THREE.Vector3(...s.slice(1,4)));const q=new THREE.CatmullRomCurve3(p,false,'centripetal').getPoint((1+f)/3);result.splice(0,3,q.x,q.y,q.z)}return result}
function yawAt(e,t){if(e.source==='native'||e.source==='recorded')return -sampleAt(e,t)[3]*Math.PI/180;const p=sampleAt(e,Math.max(e.spawn,t-.5)),q=sampleAt(e,Math.min(END,t+.5));return -Math.atan2(q[0]-p[0],q[1]-p[1]);}
function mat(color,emission=0){return new THREE.MeshStandardMaterial({color,roughness:.55,metalness:.25,emissive:color,emissiveIntensity:emission})}
function box(group,x,y,z,sx,sy,sz,material){const m=new THREE.Mesh(new THREE.BoxGeometry(sx,sy,sz),material);m.position.set(x,y,z);group.add(m);return m}
function makePlane(side){const g=new THREE.Group(),m=mat(side==='blue'?0xafdbe1:0xd4aaa0,.08),dark=mat(0x263f4e),glow=mat(COLOR[side],1);
 const body=new THREE.Mesh(new THREE.SphereGeometry(1,12,8),m);body.scale.set(.047,.042,.25);g.add(body);
 const wing=new THREE.Mesh(new THREE.BoxGeometry(.56,.022,.09),m);wing.position.z=.04;g.add(wing);
 const tail=new THREE.Mesh(new THREE.BoxGeometry(.23,.018,.065),dark);tail.position.z=.185;g.add(tail);
 box(g,0,.05,.175,.018,.11,.065,m);box(g,0,.03,-.11,.06,.025,.08,dark);
 for(const x of [-.27,.27]){const p=new THREE.Mesh(new THREE.SphereGeometry(.016,7,5),glow);p.position.set(x,0,.04);g.add(p)}
 return g}
function makeBoat(side){const g=new THREE.Group(),hull=mat(side==='blue'?0x5c95a5:0x916d6b),dark=mat(0x1c3544),glow=mat(COLOR[side],.6);
 const shape=new THREE.Shape();shape.moveTo(0,-.26);shape.lineTo(.105,-.08);shape.lineTo(.095,.23);shape.lineTo(-.095,.23);shape.lineTo(-.105,-.08);shape.closePath();const h=new THREE.Mesh(new THREE.ExtrudeGeometry(shape,{depth:.07,bevelEnabled:true,bevelThickness:.015,bevelSize:.018,bevelSegments:1}),hull);h.rotation.x=-Math.PI/2;h.rotation.z=Math.PI;g.add(h);
 box(g,0,.12,.06,.13,.14,.17,dark);box(g,0,.2,.06,.10,.025,.10,hull);box(g,0,.31,.07,.012,.20,.012,hull);box(g,0,.40,.07,.10,.015,.018,glow);box(g,0,.09,-.1,.06,.03,.07,glow);return g}
function makeBuoy(){const g=new THREE.Group();
 const float=new THREE.Mesh(new THREE.CylinderGeometry(.10,.12,.06,16),mat(COLOR.neutral));g.add(float);
 box(g,0,.16,0,.012,.28,.012,mat(COLOR.neutral));box(g,0,.29,0,.07,.025,.05,mat(0x263f4e));return g}
function makeFacility(e){const g=new THREE.Group(),m=mat(0x7b8e89),dark=mat(0x3b5356),glow=mat(COLOR[e.side],.3);
 if(e.id==='site.shore-radar'){box(g,0,.45,0,.05,.9,.05,dark);const dish=new THREE.Mesh(new THREE.ConeGeometry(.2,.12,14,1,true),glow);dish.rotation.x=Math.PI/2;dish.position.y=.9;g.add(dish);const base=new THREE.Mesh(new THREE.CylinderGeometry(.18,.22,.05,12),dark);g.add(base)}
 else if(e.id==='facility.berth'||e.id==='facility.pier'){box(g,0,.035,0,.7,.07,.24,dark);for(const x of [-.25,0,.25])box(g,x,.015,.22,.06,.10,.48,m);box(g,.13,.22,0,.025,.4,.025,glow);box(g,.23,.4,0,.3,.025,.025,glow);box(g,-.2,.13,-.03,.18,.20,.12,m)}
 else{box(g,0,.14,0,.35,.28,.25,m);box(g,-.22,.08,.04,.20,.16,.21,dark);box(g,.2,.08,.03,.16,.16,.16,dark);box(g,0,.3,0,.26,.04,.19,glow);box(g,.09,.48,.03,.015,.35,.015,glow)}return g}
function createEntity(e){const g=new THREE.Group();g.userData.entityId=e.id;const model=e.kind==='air'?makePlane(e.decoy?'decoy':e.side):e.kind==='surface'?(e.side==='neutral'?makeBuoy():makeBoat(e.side)):makeFacility(e);if(!isCombat())model.scale.setScalar(.4);g.add(model);
 const ring=new THREE.Mesh(new THREE.RingGeometry(.17,.185,40),new THREE.MeshBasicMaterial({color:e.decoy?COLOR.decoy:COLOR[e.side],transparent:true,opacity:.55,side:THREE.DoubleSide}));ring.rotation.x=-Math.PI/2;ring.position.y=-.01;g.add(ring);
 const selection=new THREE.Mesh(new THREE.RingGeometry(.25,.268,48),new THREE.MeshBasicMaterial({color:COLOR.gold,transparent:true,opacity:.9,side:THREE.DoubleSide}));selection.rotation.x=-Math.PI/2;g.add(selection);selection.visible=false;
 const stem=new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(),new THREE.Vector3(0,-1,0)]),new THREE.LineDashedMaterial({color:COLOR[e.side],transparent:true,opacity:.25,dashSize:.04,gapSize:.04}));g.add(stem);
 const shadow=new THREE.Mesh(new THREE.RingGeometry(.08,.115,30),new THREE.MeshBasicMaterial({color:COLOR[e.side],transparent:true,opacity:.25,side:THREE.DoubleSide}));shadow.rotation.x=-Math.PI/2;world.add(shadow);
 const lineMat=e.source==='anchors'?new THREE.LineDashedMaterial({color:e.decoy?COLOR.decoy:COLOR[e.side],transparent:true,opacity:.4,dashSize:.14,gapSize:.11}):new THREE.LineBasicMaterial({color:COLOR[e.side],transparent:true,opacity:.5});
 const trail=new THREE.Line(new THREE.BufferGeometry(),lineMat);world.add(trail);
 const trailDots=new THREE.Points(new THREE.BufferGeometry(),new THREE.PointsMaterial({color:COLOR[e.side],size:.045,transparent:true,opacity:.8,sizeAttenuation:true}));world.add(trailDots);
 const label=document.createElement('button');label.type='button';label.dataset.entityId=e.id;label.className='entity-label '+e.side+(e.kind==='facility'?' facility':'')+(e.decoy?' decoy':'');label.style.color=e.side==='neutral'?'#e9ce91':e.side==='blue'?'#bceef3':'#ffc0b6';label.style.borderColor=e.side==='neutral'?'#665f3f':e.side==='blue'?'#294b59':'#644440';const dot=document.createElement('span');dot.className='small-dot';label.append(dot,document.createTextNode(e.short+(e.decoy?' · 诱':'')));label.setAttribute('aria-label','观察'+e.name);label.addEventListener('click',()=>selectEntity(e.id));$('labels').appendChild(label);
 world.add(g);meshes.set(e.id,{g,model,ring,selection,stem,shadow,trail,trailDots,label,lastLife:null});
}
function init(){scene=new THREE.Scene();scene.fog=new THREE.FogExp2(0x081724,.003);renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,powerPreference:'high-performance',preserveDrawingBuffer:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0x07131d,0);renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.3;renderScene.appendChild(renderer.domElement);
 scene.add(new THREE.HemisphereLight(0xbce9f4,0x133144,2.5));const key=new THREE.DirectionalLight(0xd6eaf5,3);key.position.set(-8,20,5);scene.add(key);const rim=new THREE.DirectionalLight(0x4cb9d3,1.2);rim.position.set(10,5,-10);scene.add(rim);
 camera=new THREE.OrthographicCamera(-14,14,10,-10,.1,2000);raycaster=new THREE.Raycaster();
 buildWorld();const ro=new ResizeObserver(resize);ro.observe(viewport);resize();setupPointer();
 const view=sceneOverview();targetOrbit.center.set(...view.focus);targetOrbit.span=view.span;orbit.center.copy(targetOrbit.center);orbit.span=targetOrbit.span;
 updateUI(true);requestAnimationFrame(frame);
}
let terrainLayers={coast:null,shelf:null};
function geographyFor(d=DATA){return d.geography||PACK.geographies?.[d.geographyRef]||PACK.geographies?.[d.visualization?.geographyContextRef]||null}
function sceneOverview(d=DATA){return d.visualization?.overview||d.meta.overview}
function showOverview(view){stopCameraAutomation();targetOrbit.center.set(...view.focus);targetOrbit.span=view.span}
function geographicOverview(){const g=geographyFor();if(!g)return DATA.meta.overview;const points=[...g.coastlineLines,...g.harborLines].flatMap(l=>l.coordinates_m),xs=points.map(p=>p[0]/1000),zs=points.map(p=>-p[1]/1000);return {focus:[(Math.min(...xs)+Math.max(...xs))/2,0,(Math.min(...zs)+Math.max(...zs))/2],span:Math.max(22,(Math.max(...xs)-Math.min(...xs)+5)*1.25,(Math.max(...zs)-Math.min(...zs)+5)*1.6)}}
function addCoastRibbon(coords){
 const positions=[],uvs=[],width=(geographyFor()?.presentation?.ribbonWidthM??320)/1000;
 for(let i=1;i<coords.length;i++){const a=coords[i-1],b=coords[i],dx=(b[0]-a[0])/1000,dz=-(b[1]-a[1])/1000,len=Math.hypot(dx,dz);if(len<1e-8)continue;const nx=-dz/len*width,nz=dx/len*width;const points=[[a[0]/1000+nx,.004,-a[1]/1000+nz],[a[0]/1000-nx,.004,-a[1]/1000-nz],[b[0]/1000+nx,.004,-b[1]/1000+nz],[b[0]/1000-nx,.004,-b[1]/1000-nz]];for(const j of [0,1,2,2,1,3]){positions.push(...points[j]);uvs.push(j%2,0)}}
 const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));geometry.setAttribute('uv',new THREE.Float32BufferAttribute(uvs,2));
 const material=new THREE.ShaderMaterial({transparent:true,depthWrite:false,side:THREE.DoubleSide,vertexShader:'varying float vAcross;void main(){vAcross=uv.x;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}',fragmentShader:'varying float vAcross;void main(){float q=abs(vAcross*2.-1.);gl_FragColor=vec4(.09,.36,.40,pow(1.-q,1.6)*.48);}'});
 const mesh=new THREE.Mesh(geometry,material);mesh.name='coast-adjacent-visual-band';terrainLayers.shelf.add(mesh);
}
function buildTerrain(){
 terrainLayers={coast:new THREE.Group(),shelf:new THREE.Group()};
 terrainLayers.coast.name='scenario-coastlines';terrainLayers.shelf.name='continental-shelf-schematic';
 world.add(terrainLayers.coast,terrainLayers.shelf);
 // Scenario collision polygons are data, not geographic coastlines. Do not draw
 // their outline, fill or distance contours in any scenario or perspective.
 const geography=geographyFor();
 if(geography){
  // Only explicitly closed source rings become filled land. Open mainland paths stay open.
  for(const region of geography.backgroundLand){const pts=region.coordinates_m.map(p=>new THREE.Vector2(p[0]/1000,p[1]/1000));const shape=new THREE.Shape(pts);const land=new THREE.Mesh(new THREE.ShapeGeometry(shape),mat(0x314c48));land.rotation.x=-Math.PI/2;land.position.y=.10;world.add(land)}
  for(const [lines,harbor] of [[geography.coastlineLines,false],[geography.harborLines,true]])for(const source of lines){
   const points=source.coordinates_m.map(p=>new THREE.Vector3(p[0]/1000,.14,-p[1]/1000));
   const coast=new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color:harbor?0x709aa4:0xd0e1b0,transparent:true,opacity:harbor?.65:.95}));coast.name=source.id;coast.userData.originalOpenPath=!source.closed;terrainLayers.coast.add(coast);
   if(!harbor)addCoastRibbon(source.coordinates_m);
  }
 }
 if($('coastlines')){terrainLayers.coast.visible=$('coastlines').checked;terrainLayers.shelf.visible=$('shelf').checked;$('coastlines').disabled=$('shelf').disabled=!geography}
 const context=DATA.visualization?.role==='background-only';
 viewport.classList.toggle('has-map-context',context);
 if($('map-context')){$('map-context').disabled=!geography;$('map-context').setAttribute('aria-label',context?'查看作为背景引用的 IE 海岸全图':'查看完整原始大陆海岸线')}
 if($('task-context'))$('task-context').hidden=!context;
 if($('overview'))$('overview').textContent=context?'海岸场景':'战区';
 if($('terrain-context')){$('terrain-context').hidden=!context;$('terrain-context').textContent=context?'IE 海图背景 · 原记录未启用地形判定':''}
 if($('terrain-note'))$('terrain-note').textContent=context?DATA.visualization.note:geography?'大陆海岸线 '+geography.coastlineLines.length+' 段 · 港区边界 '+geography.harborLines.length+' 段 · 无真实水深数据，近岸色带仅示意':'本场景未提供海岸地图；场景碰撞多边形不作为海岸线绘制';
}
function buildWorld(){world=new THREE.Group();scene.add(world);
 const waterMat=new THREE.ShaderMaterial({uniforms:{uTime:{value:0}},vertexShader:'varying vec3 vP;void main(){vP=(modelMatrix*vec4(position,1.)).xyz;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}',fragmentShader:'varying vec3 vP;uniform float uTime;void main(){float x=vP.x,z=vP.z;float wave=sin(x*11.+z*8.+uTime*.35)*sin(z*18.-x*3.+uTime*.2);float stripe=pow(max(0.,sin(x*3.8+z*5.4+wave*.2)),28.);float glow=exp(-length(vec2(x-5.,z+1.))*.045);vec3 c=mix(vec3(.018,.052,.077),vec3(.032,.102,.135),glow);c+=stripe*.005+wave*.0018;gl_FragColor=vec4(c,1.);}' });
 sea=new THREE.Mesh(new THREE.PlaneGeometry(250,250),waterMat);sea.rotation.x=-Math.PI/2;sea.position.y=-.05;world.add(sea);
 const grid=new THREE.GridHelper(60,30,0x2c5666,0x173542);grid.position.set(6,-.038,0);grid.material.transparent=true;grid.material.opacity=.42;world.add(grid);
 buildTerrain();
 buildTaskZones();
 for(const zone of DATA.zones){const pts=zone.coordinates_m.map(p=>new THREE.Vector3(p[0]/1000,.15,-p[1]/1000));const l=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),new THREE.LineDashedMaterial({color:0x9cc3a9,transparent:true,opacity:.4,dashSize:.06,gapSize:.06}));l.computeLineDistances();world.add(l)}
 for(const e of DATA.entities)createEntity(e);
 buildRangeOverlays();
 for(const shot of DATA.shots){const glow=new THREE.Mesh(new THREE.SphereGeometry(.065,8,6),new THREE.MeshBasicMaterial({color:COLOR[shot.side]}));const track=new THREE.Line(new THREE.BufferGeometry(),new THREE.LineBasicMaterial({color:COLOR[shot.side],transparent:true,opacity:.75}));const route=new THREE.Line(new THREE.BufferGeometry(),new THREE.LineDashedMaterial({color:COLOR[shot.side],transparent:true,opacity:.14,dashSize:.08,gapSize:.09}));const launch=new THREE.Mesh(new THREE.RingGeometry(.09,.13,32),new THREE.MeshBasicMaterial({color:COLOR[shot.side],transparent:true,opacity:.9,side:THREE.DoubleSide,depthWrite:false}));launch.rotation.x=-Math.PI/2;world.add(glow,track,route,launch);shotMeshes.push({glow,track,route,launch})}
 for(const hit of DATA.hits){const ring=new THREE.Mesh(new THREE.RingGeometry(.2,.235,48),new THREE.MeshBasicMaterial({color:COLOR.gold,transparent:true,opacity:.7,side:THREE.DoubleSide,depthWrite:false}));ring.rotation.x=-Math.PI/2;const glow=new THREE.Mesh(new THREE.SphereGeometry(.08,10,8),new THREE.MeshBasicMaterial({color:0xffd09a,transparent:true,opacity:.85,depthWrite:false}));world.add(ring,glow);hitMeshes.push({ring,glow})}
}

function resize(){W=viewport.clientWidth;H=viewport.clientHeight;renderer.setSize(W,H,false);drawTimeline();}
function updateCamera(dt){const f=1-Math.exp(-dt*3);orbit.theta+=(targetOrbit.theta-orbit.theta)*f;orbit.phi+=(targetOrbit.phi-orbit.phi)*f;orbit.span+=(targetOrbit.span-orbit.span)*f;orbit.center.lerp(targetOrbit.center,f);
 let span=orbit.span,aspect=W/H;let vertical=span/Math.max(aspect,.8);if(aspect<1)vertical=Math.max(isCombat()?22:3.4,vertical*.78);let horizontal=vertical*aspect;camera.left=-horizontal/2;camera.right=horizontal/2;camera.top=vertical/2;camera.bottom=-vertical/2;camera.updateProjectionMatrix();const dist=Math.max(40,orbit.span*2);camera.position.set(orbit.center.x+Math.sin(orbit.theta)*Math.sin(orbit.phi)*dist,orbit.center.y+Math.cos(orbit.phi)*dist,orbit.center.z+Math.cos(orbit.theta)*Math.sin(orbit.phi)*dist);camera.lookAt(orbit.center);camera.updateMatrixWorld();$('north-arrow').style.transform='rotate('+(-orbit.theta*180/Math.PI)+'deg)';const scaleM=DATA.visualization?.role==='background-only'?(horizontal<4?200:horizontal<12?500:2000):2000;$('scale-line').style.width=(scaleM/1000/horizontal*W)+'px';$('scale-label').textContent=scaleM<1000?scaleM+' m':scaleM/1000+' km';}
function trajectorySamples(e){if(!trajectoryCache.has(e)){const ticks=e.nativeSampleTicks?new Set(e.nativeSampleTicks):null;trajectoryCache.set(e,ticks?e.samples.filter(s=>ticks.has(s[0])):e.source==='native'?e.samples.filter(s=>s[0]<=(e.lastNativeTick??Infinity)):e.samples)}return trajectoryCache.get(e)}
function smoothPoints(pts){return smoothDisplay&&!frameMode&&pts.length>2?new THREE.CatmullRomCurve3(pts,false,'centripetal').getPoints((pts.length-1)*4):pts}
function updateTrail(e,m,t){if(!showTrails||e.kind==='facility'||t<e.spawn||!visibleInPerspective(e)){m.trail.visible=m.trailDots.visible=false;return}
 const samples=trajectorySamples(e).filter(s=>s[0]<=displayTime()),pts=samples.map(s=>point(s.slice(1,4)));
 m.trail.visible=pts.length>1;m.trailDots.visible=e.source!=='anchors'&&pts.length>0;
 const key=samples.length+'|'+smoothDisplay+'|'+frameMode;if(key===m.trailKey)return;m.trailKey=key;
 setLine(m.trail,e.source==='anchors'?pts:smoothPoints(pts),e.source==='anchors');setLine(m.trailDots,e.source==='anchors'?[]:pts);
 m.trail.userData={recordedPoints:pts.length,source:e.source,smoothed:smoothDisplay&&!frameMode&&e.source!=='anchors'};}

function updateEntities(){refreshPerspective();for(const e of DATA.entities){const m=meshes.get(e.id);const visible=visibleInPerspective(e);m.g.visible=visible;m.shadow.visible=visible&&e.kind==='air';m.label.style.display=visible?'block':'none';if(!visible){m.trail.visible=m.trailDots.visible=false;continue}
 const sample=displayedSample(e),st=displayedState(e);const disabled=st.life==='disabled'||st.life==='destroyed';m.g.position.copy(point(sample));m.model.rotation.y=yawAt(e,displayTime());m.selection.visible=e.id===selected;m.shadow.position.set(m.g.position.x,.016,m.g.position.z);m.stem.visible=e.kind==='air';if(e.kind==='air'){m.stem.geometry.dispose();m.stem.geometry=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0,-m.g.position.y+.01,0),new THREE.Vector3(0,-.05,0)]);m.stem.computeLineDistances()}
 if(m.lastLife!==st.life){m.lastLife=st.life;const c=disabled?(e.kind==='facility'?COLOR.red:COLOR.disabled):(e.decoy?COLOR.decoy:COLOR[e.side]);m.ring.material.color.setHex(c);m.model.traverse(o=>{if(o.isMesh){o.material.transparent=disabled;o.material.opacity=disabled?.42:1}});m.label.classList.toggle('disabled',disabled)}m.label.classList.toggle('selected',e.id===selected);m.label.style.opacity=disabled&&e.id!==selected?.60:1;
 if(following&&e.id===selected)targetOrbit.center.copy(m.g.position).setY(0);
 }updateRangeOverlays();}
function setLine(line,pts,dashed=false){line.geometry.dispose();line.geometry=new THREE.BufferGeometry().setFromPoints(pts);if(dashed)line.computeLineDistances()}
function missilePoint(s,f){const a=point(s.from),b=point(s.end!==null?s.impact:s.toward);const p=a.lerp(b,f);p.y+=Math.sin(Math.PI*f)*(s.weapon.includes('surface')?.32:.17);return p}
function initialMissilePoint(s,f){const origin=point(s.from),direction=point(s.toward).sub(origin);direction.multiplyScalar(Math.min(1,.9/Math.max(.001,direction.length())));return origin.addScaledVector(direction,f)}
function updateShots(){const launches=[];DATA.shots.forEach((s,i)=>{const m=shotMeshes[i],age=displayTime()-s.t,allowed=perspective==='god'||s.side===perspective;
 const dur=s.end!==null?Math.max(.001,s.end-s.t):Math.min(10,Math.max(3,point(s.from).distanceTo(point(s.toward))*1.7)),f=age/dur;
 m.launch.visible=allowed&&age>=0&&age<1.5;m.launch.position.copy(point(s.from));m.launch.scale.setScalar(1+Math.max(0,age)*2);m.launch.material.opacity=Math.max(0,.95*(1-age/1.5));
 if(allowed&&age>=0&&age<2)launches.push(E.get(s.a)?.short||s.a);
 const hasNative=recordedMissileIds().has(s.id),native=allowed&&hasNative?recordedMissilePoint(s):null;
 const active=allowed&&s.kind!=='delayed_effect'&&(hasNative?!!native:f>=0&&f<=1&&(perspective==='god'||age<=3));m.glow.visible=m.track.visible=m.route.visible=active;if(!active)return;
 if(native){m.glow.position.copy(native);const ps=DATA.recording.frames.filter(fr=>fr.tick>=displayTime()-3&&fr.tick<=displayTime()).flatMap(fr=>(fr.projectiles||[]).filter(p=>p.missile_id===s.id).map(p=>point(p.position_m)));if(smoothDisplay&&!frameMode)ps.push(native.clone());setLine(m.track,ps);m.route.visible=false;return}
 if(perspective!=='god'){const a=Math.min(1,age/3),p=initialMissilePoint(s,a);m.glow.position.copy(p);setLine(m.track,[initialMissilePoint(s,Math.max(0,a-.35)),p]);m.route.visible=false;return}
 const limited=s.end!==null?f:f*.28;m.glow.position.copy(missilePoint(s,limited));const pts=[];for(let j=0;j<=16;j++){const k=Math.max(0,limited-(s.end!==null?.13:.10))+((limited-Math.max(0,limited-(s.end!==null?.13:.10)))*j/16);pts.push(missilePoint(s,k))}setLine(m.track,pts);m.track.material.opacity=s.end!==null?.85:.45;m.glow.scale.setScalar(s.end!==null?1.15:.7);
 if(s.end!==null){const ps=[];for(let j=0;j<=30;j++)ps.push(missilePoint(s,j/30));setLine(m.route,ps,true)}else m.route.visible=false;
 });const status=$('launch-status');status.hidden=!launches.length;status.textContent=launches.length?'武器启动 · '+[...new Set(launches)].join(' / ')+(DATA.recording?'':' · 初段示意'):'';
 DATA.hits.forEach((h,i)=>{const m=hitMeshes[i],age=displayTime()-h.t,visible=(perspective==='god'||E.get(h.target)?.side===perspective)&&!!h.position&&age>=0&&age<8;m.ring.visible=visible;m.glow.visible=visible;if(!visible)return;const p=point(h.position);m.ring.position.copy(p);m.ring.scale.setScalar(1+age*.60);m.ring.material.opacity=.8*(1-age/8);m.glow.position.copy(p);m.glow.scale.setScalar(.8+Math.min(age,1)*2);m.glow.material.opacity=.9*Math.max(0,1-age/2.4)})}
function updateLabels(){const occupied=[];const sorted=DATA.entities.slice().sort((a,b)=>(b.id===selected?100:0)+(b.kind==='facility'?20:0)-(a.id===selected?100:0)-(a.kind==='facility'?20:0));for(const e of sorted){const m=meshes.get(e.id);if(!m.g.visible)continue;vec.copy(m.g.position);vec.y+=e.kind==='facility'?.7:.35;vec.project(camera);const x=(vec.x*.5+.5)*W,y=(-vec.y*.5+.5)*H;const width=m.label.offsetWidth||55,height=21;let yy=y;const pinned=e.id===selected||e.kind==='facility';if(x<width/2+3||x>W-width/2-3||y<145||y>H-98||Math.abs(vec.z)>1){m.label.style.visibility='hidden';continue}
 let bounds={l:x-width/2,r:x+width/2,t:yy-height,b:yy};for(let attempt=0;attempt<4&&occupied.some(q=>bounds.l<q.r+3&&bounds.r>q.l-3&&bounds.t<q.b+3&&bounds.b>q.t-3);attempt++){yy-=22;bounds.t=yy-height;bounds.b=yy}const overlaps=occupied.some(q=>bounds.l<q.r+3&&bounds.r>q.l-3&&bounds.t<q.b+3&&bounds.b>q.t-3);if((overlaps&&!pinned)||yy<132){m.label.style.visibility='hidden';continue}occupied.push(bounds);m.label.style.visibility='visible';m.label.style.left=x+'px';m.label.style.top=yy+'px';}updateContactLabels();}
function currentChapter(){let n=0;chapters.forEach((c,i)=>{if(time>=c.t)n=i});return n}

const UNIT_LIFE_LABELS={active:'可行动',degraded:'受损，可行动',disabled:'失能',destroyed:'摧毁',wreck:'残骸',despawned:'已离场'};
const energyRows=new Map();
let energyRosterEpisode=null,energyRosterSide=null,energyRosterIds='';

function renderEnergy(bar,label,state){
 const value=state?.energy_state?.value;
 const available=Number.isFinite(value)&&value>=0&&value<=1;
 bar.hidden=!available;
 bar.toggleAttribute('data-low',available&&value<=.2);
 if(!available){bar.removeAttribute('value');label.textContent='未记录';return}
 bar.value=value;
 const percent=value>0&&value<.001?'<0.1%':(value*100).toFixed(1).replace(/\.0$/,'')+'%';
 label.textContent=percent+(value===0?' · 已耗尽':value<=.2?' · 低能量':'');
}

function rebuildEnergyRows(list,units){
 energyRows.clear();
 list.replaceChildren();
 if(!units.length){
  const note=document.createElement('p');note.className='energy-empty';note.textContent='当前帧没有可查看的单位';
  list.append(note);return;
 }
 for(const side of ['red','blue']){
  const group=units.filter(unit=>unit.side===side);
  if(!group.length)continue;
  const heading=document.createElement('div');heading.className='energy-group';heading.textContent=sideName(side);
  list.append(heading);
  for(const unit of group){
   const button=document.createElement('button');button.type='button';button.className='energy-row';
   button.dataset.energyEntity=unit.id;
   const name=document.createElement('span');name.className='energy-unit-name';name.textContent=unit.name;name.title=unit.name;
   const label=document.createElement('span');label.className='energy-value';
   const bar=document.createElement('progress');bar.max=1;bar.setAttribute('aria-label',unit.name+'剩余能量');
   const status=document.createElement('span');status.className='energy-unit-state';
   button.append(name,label,bar,status);
   button.addEventListener('click',()=>{
    if(!visibleInPerspective(unit))return;
    selectEntity(unit.id);
    targetOrbit.center.copy(point(displayedSample(unit))).setY(0);
   });
   energyRows.set(unit.id,{button,bar,label,status});list.append(button);
  }
 }
}

function updateEnergyInspector(entity){
 const side=perspective==='god'&&DATA.meta.observationScope==='participant-trace'?participantSide():perspective;
 const tracked=DATA.entities.filter(unit=>unit.energyTracked&&(side==='god'||unit.side===side));
 const inspector=$('inspect-energy');
 const roster=$('energy-roster'),list=$('energy-roster-list');
 if(energyRosterEpisode!==DATA||energyRosterSide!==side){
  energyRosterEpisode=DATA;energyRosterSide=side;energyRosterIds='';
  energyRows.clear();list.replaceChildren();
 }
 const show=entity?.energyTracked&&(side==='god'||entity.side===side)&&visibleInPerspective(entity);
 inspector.hidden=!show;
 roster.hidden=!tracked.length;
 if(!show&&(roster.hidden||!roster.open))return;
 const readings=side==='god'
  ?new Map((nativeFrame()?.entities||[]).map(state=>[state.entity_id,state]))
  :perspectiveTick===Math.floor(displayTime())?perspectiveOwn:null;
 if(show)renderEnergy($('inspect-energy-bar'),$('inspect-energy-value'),readings?.get(entity.id));
 if(roster.hidden||!roster.open)return;
 const units=tracked.filter(visibleInPerspective);
 const ids=JSON.stringify(units.map(unit=>unit.id));
 if(ids!==energyRosterIds){energyRosterIds=ids;rebuildEnergyRows(list,units)}
 for(const unit of units){
  const {button,bar,label,status}=energyRows.get(unit.id),state=readings?.get(unit.id);
  button.setAttribute('aria-current',String(unit.id===selected));
  renderEnergy(bar,label,state);
  status.textContent=UNIT_LIFE_LABELS[state?.lifecycle_state]||'状态未记录';
 }
}

function updateUI(force=false){
 if(force)updateBuoyLossReport();
 updateWeatherPanel();
 updateEngagementPanel();
 updateFrameTools();
 updateRangeInspector();mountRangeControls();
 $('clock').textContent=fmt(time);$('scrub').value=time;$('scrub').style.setProperty('--progress',(time/END*100)+'%');
 const n=currentChapter();if(n!==chapterIndex||force){chapterIndex=n;const c=chapters[n];$('chapter-count').textContent=String(n+1).padStart(2,'0')+' / '+String(chapters.length).padStart(2,'0')+'   '+fmt(c.t);$('chapter-title').textContent=c.title;$('chapter-copy').textContent=c.copy;
  document.querySelectorAll('[data-chapter]').forEach(b=>b.classList.toggle('active',+b.dataset.chapter===n));if(tour&&!following){targetOrbit.center.set(...c.focus);targetOrbit.span=c.span}}
 $('quiet-skip').style.display=tour&&playing&&time>DATA.meta.quietAfter&&time<END?'block':'none';$('finish').style.display=time>=END?'block':'none';$('narrative').style.opacity=time>=END?'0':'1';
 let neutral=0,active=0;for(const e of DATA.entities){if(time<e.spawn)continue;const st=lifeAt(e,time);if(e.armedThreat&&!isOperationalLife(st.life))neutral++;if(e.side===participantSide()&&e.kind!=='facility'&&isOperationalLife(st.life))active++}
 $('neutral-count').textContent=DATA.meta.participantOnly?'—':neutral;$('own-active').textContent=active;$('shot-count').textContent=DATA.meta.participantOnly?'—':DATA.shots.filter(s=>s.t<=time).length;$('shot-max').textContent=DATA.meta.participantOnly?' / 执行状态见记录':' / '+DATA.shots.length;$('final-score').textContent=DATA.meta.participantOnly?'未包含':time>=END?scoreText(DATA.meta.score):'—';
 updateTaskPanel();
 let lost=0;for(const e of protectedEntities()){
  const st=lifeAt(e,time),row=Array.from(document.querySelectorAll('[data-facility]')).find(r=>r.dataset.facility===e.id);if(!row)continue;
  const bad=!isOperationalLife(st.life);if(bad)lost++;
  const value=row.querySelector('.value'),bar=row.querySelector('.facility-health i');
  value.textContent=Math.round(st.health*100)+'%'+(bad?' '+(st.life==='destroyed'?'摧毁':'失能'):st.life==='degraded'?' 受损':'');value.style.color=bar.style.background=bad?'var(--red)':st.health<1?'var(--gold)':'var(--green)';bar.style.width=st.health*100+'%';row.classList.toggle('facility-lost',bad);
 }
 const banner=$('damage-alert');banner.hidden=lost===0;banner.textContent=lost?'保护设施 '+lost+' / '+DATA.meta.facilityCount+' 已失能或摧毁':'';
 const e=E.get(selected);if(e&&visibleInPerspective(e)){const p=displayedSample(e),st=displayedState(e);
  $('inspect-name').textContent=e.name;$('inspect-kind').textContent=sideName(e.side)+(e.decoy?' / 无武装诱饵':'')+' / '+({air:'空中单元',surface:'海面单元',facility:'固定设施'}[e.kind])+' / '+e.short;
  $('inspect-color').style.background=e.decoy?'#d7b3f2':e.side==='neutral'?'#e9ce91':e.side==='blue'?'var(--blue)':'var(--red)';$('inspect-state').textContent=time<e.spawn?'待入场':({active:'可行动',degraded:'受损，可行动',disabled:'失能',destroyed:'摧毁'}[st.life]||st.life);
  $('inspect-health').textContent=time<e.spawn?'—':Math.round(st.health*100)+'%';$('inspect-alt').textContent=time<e.spawn?'—':Math.round(p[2])+' m';$('inspect-ammo-label').textContent=e.source!=='anchors'?'剩余弹药':'已证实发射';$('inspect-ammo').textContent=time<e.spawn?'—':e.source!=='anchors'?p[5]+' 发':DATA.shots.filter(s=>s.a===e.id&&s.t<=time).length+' 发';
  $('inspect-source').textContent=(perspective!=='god'?'本方全队 · ':'')+(e.source==='recorded'?'位置：逐 tick 记录':e.source==='native'?'位置：原生观测记录':'位置：稀疏锚点示意，缺少完整航迹')+(frameMode?' · 原始帧':smoothDisplay&&e.source!=='anchors'?' · 平滑显示':' · 记录点折线');
 }
 if(!e||!visibleInPerspective(e)){for(const id of ['inspect-health','inspect-alt','inspect-ammo','inspect-state'])$(id).textContent='—';$('inspect-name').textContent='暂无可见己方单元';$('inspect-kind').textContent='本方单位尚未入场或记录缺失';$('inspect-source').textContent='';}$('follow').disabled=!e||!visibleInPerspective(e);
 updateEnergyInspector(e);
 const happened=perspective==='god'?DATA.events.filter(e=>e.t<=time):[];$('event-total').textContent=happened.length+' 条已发生';if(happened.length!==lastEventIndex||force){lastEventIndex=happened.length;const panel=$('event-list');panel.replaceChildren();if(!happened.length){const empty=document.createElement('div');empty.className='event-empty';empty.textContent='尚未交战。可点击下方关键阶段快速查看。';panel.appendChild(empty)}
  else for(const ev of happened.slice().reverse()){const btn=document.createElement('button');btn.className='event-entry '+ev.type;const stamp=document.createElement('time');stamp.textContent=fmt(ev.t);const copy=document.createElement('span');copy.className='event-copy';const title=document.createElement('b');title.textContent=ev.title;const detail=document.createElement('small');detail.textContent=ev.detail;copy.append(title,detail);btn.append(stamp,copy);btn.addEventListener('click',()=>{seek(ev.t);if(ev.a)selectEntity(ev.a)});panel.appendChild(btn)}panel.scrollTop=0;
 }
}
function updateWeatherPanel(){
 const panel=document.getElementById('weather-panel');if(!panel)return;
 panel.hidden=!DATA.weather;if(!DATA.weather)return;
 const tick=Math.floor(displayTime()),phase=(DATA.weather.timeline||[]).filter(p=>p.tick<=tick).at(-1);
 const stamp=DATA.meta.key+':'+(phase?.active_from_tick??'none');if(panel.dataset.stamp===stamp)return;
 panel.dataset.stamp=stamp;panel.replaceChildren();
 const title=document.createElement('div');title.className='section-heading';title.textContent='公开天气阶段 · 原生记录';panel.appendChild(title);
 const text=document.createElement('p');text.className='source-label';text.textContent=phase?`${phase.label} · t${phase.active_from_tick} 生效 · 传感器距离 ×${phase.sensor_range_multiplier} · 探测概率 ×${phase.sensor_detection_probability_multiplier} · 海况 ${phase.sea_state}`:'暂无已记录的原生天气状态';panel.appendChild(text);
 for(const p of DATA.weather.schedule||[]){const button=document.createElement('button');button.className='source-label';button.textContent=`t${p.tick} ${p.label}`;button.addEventListener('click',()=>seek(Math.min(END,p.tick)));panel.appendChild(button)}
 const note=document.createElement('p');note.className='source-label';note.textContent=DATA.engagement?.contract?.schema_version==='autonomous-contact-judgment@2'?'天气对双方生效；双方根据实际观测及轨迹自行判断目标。匿名接触不代表无效接触，发射由原生通用规则检查。':'天气对双方生效；轨迹不能授予攻击权限。证据按实际光电测量、通信与有效期更新，不随起雾强制清空。';panel.appendChild(note);
}
function engagementEvidenceText(event){
 if(event.type==='action_feedback_delivered')return `原生 t${event.execution_tick} 的执行回执于 t${event.tick} 送达本方 · ${event.action_id} · ${event.status}${event.error_code?' · '+event.error_code:''}`;
 const target=event.target_entity_id||event.contact_id||'';
 if(event.diagnostic_only){
  if(event.type==='attack_request')return `${event.attacker_id} → ${target} · 自主射击请求 · 已提交原生检查${event.third_party?' · 裁判真值：第三方':''} · 不额外扣分或改判`;
  if(event.type==='attack_executed')return `${event.attacker_id} → ${target} · 实际发射并耗弹 ${event.shots} 发${event.third_party?' · 裁判真值：第三方':''}`;
  if(event.type==='third_party_damage')return `${target} · 原生健康值 ${event.health_before} → ${event.health_after} · 武器归属 ${(event.attributed_loss_factions||[]).join('、')||'见原始损伤凭据'} · 仅诊断，不额外扣分`;
  return `${event.contact_id||event.action_id||''} · ${event.status||''} · ${event.error_code||'原生执行结果'}`;
 }
 const identity={unknown:'尚未识别',armed_surface_vessel:'已识别武装水面艇',observation_buoy:'已识别观测浮标'};
 const reasons={unidentified_or_unauthorized:'尚未识别或未获交战授权',third_party_target:'针对第三方',unresolved_contact:'无法关联有效接触'};
 if(event.type==='identification'||event.type==='evidence_refresh')return `${target} · ${event.reidentified?'再次取得有效证据 · ':''}${identity[event.status]||event.status} · 设备 ${event.observed_features?.equipment||'未记录'} · 证据 t${event.evidence_tick}，有效至 t${event.valid_until_tick}${event.optical_sample_ticks?' · 原生测量 ticks '+event.optical_sample_ticks.join('、'):''}`;
 if(event.type==='identification_expired')return `${target} · 证据 t${event.evidence_tick} 已于 t${event.valid_until_tick} 到期 · 当前未授权；接触消失不代表威胁已处置`;
 if(event.type==='attack_request')return `${event.attacker_id} → ${target} · ${event.violation?'违规请求':'合法请求'} · ${(event.reasons||[]).map(reason=>reasons[reason]||reason).join('；')||'公开识别授权有效'} · ${event.native_submitted?'已提交原生执行检查':'发射前已拒绝'} · 接触 ${event.contact_id||'无'}`;
 if(event.type==='attack_executed')return `${event.attacker_id} → ${target} · 原生实际发射 ${event.shots} 发${event.third_party?' · 目标为第三方':''}`;
 if(event.type==='third_party_damage'){
  const projectiles=(event.red_weapon_intents||[]).map(intent=>intent.parameters?.missile_id||intent.parameters?.pending_impact_id).filter(Boolean);
  return `${target} · 健康值 ${event.health_before} → ${event.health_after} · ${event.loss?'发生损失':'尚未损失'} · ${event.loss_attributed_to_red?'损失可归责于红方武器':'未将损失归责于红方'}${event.mixed_causes?' · 存在混合来源':''} · 红方发射关联 ${projectiles.join('，')||'见原始凭据'}`;
 }
 return `${target} · ${event.status||'见原始凭据'}`;
}
function updateBuoyLossReport(){
 const report=$('buoy-loss-summary');
 const counts=perspective==='god'?finalBuoyLosses:null;
 report.textContent=counts?'中立浮标损失（按浮标去重） · 本方 '+counts.own+' · 对方 '+counts.opponent+' · 未归责 '+counts.unattributed:'';
 report.hidden=!counts;
}

function updateEngagementPanel(){
 const panel=document.getElementById('engagement-panel');
 if(!panel)return;
 panel.hidden=!DATA.engagement;
 if(!DATA.engagement)return;
 const stamp=(perspective==='god'?'referee':'participant')+':'+Math.floor(time);
 if(panel.dataset.stamp===stamp&&panel.dataset.episode===DATA.meta.key)return;
 panel.dataset.stamp=stamp;panel.dataset.episode=DATA.meta.key;panel.replaceChildren();
 const title=document.createElement('div');title.className='section-heading';title.textContent=perspective==='god'?'识别与交战 · 裁判证据':'识别与交战 · 授权观察';panel.appendChild(title);
 const text=document.createElement('p');text.className='source-label';
 if(DATA.engagement.contract.schema_version==='autonomous-contact-judgment@2'){
  title.textContent=perspective==='god'?'自主判断与真实交战 · 裁判诊断':'匿名接触与观测历史';
  if(perspective==='god'){
   const latest=DATA.engagement.timeline.filter(r=>r.t<=time).at(-1),by=latest?.audit?.by_faction;
   text.textContent=by?`原IE得分 ${latest.points.toFixed(2)} · 红方实际耗弹 ${by.red.shots}（第三方 ${by.red.third_party_shots}） · 蓝方实际耗弹 ${by.blue.shots}（第三方 ${by.blue.third_party_shots}） · 误判记录不额外改变评分或胜负`:'尚无交战诊断';panel.appendChild(text);
   if(time>=END&&DATA.meta.verdictReason){const reason=document.createElement('p');reason.className='source-label';reason.textContent='原生最终判定：'+DATA.meta.verdictReason;panel.appendChild(reason)}
   for(const event of DATA.engagement.events.filter(e=>e.tick<=time).slice(-12)){
    const row=document.createElement('p');row.className='source-label';row.textContent=`t${event.tick} · ${engagementEvidenceText(event)}`;panel.appendChild(row);
    const details=document.createElement('details'),summary=document.createElement('summary'),raw=document.createElement('pre');summary.textContent='原始裁判诊断（不参与改分）';raw.style.whiteSpace='pre-wrap';raw.style.overflowWrap='anywhere';raw.textContent=JSON.stringify(event,null,2);details.append(summary,raw);panel.appendChild(details);
   }
  }else{
   text.textContent='仅本方实际收到的匿名观测。灰色符号与轨迹不表示阵营或类别；没有裁判识别答案。';panel.appendChild(text);
   for(const c of perspectiveContacts){const row=document.createElement('p');row.className='source-label';row.textContent=`${c.contact_id} · 测量 t${c.observed_tick} · 报告年龄 ${c.age_ticks} · 置信度 ${c.track_quality??c.confidence} · 来源 ${c.observer_entity_id}`;panel.appendChild(row)}
   if(DATA.meta.participantOnly)for(const event of DATA.engagement.events.filter(e=>e.tick<=time).slice(-6)){const row=document.createElement('p');row.className='source-label';row.textContent=`t${event.tick} ${event.type} · ${event.contact_id||event.action_id||''} · ${event.response||event.error_code||event.status||'本方执行状态'}`;panel.appendChild(row)}
  }
  return;
 }
 if(perspective==='god'){
  const latest=DATA.engagement.timeline.filter(r=>r.t<=time).at(-1);
  const audit=latest?.audit;
  text.textContent=audit?`当前 ${latest.points.toFixed(2)} 分 · 攻击请求 ${audit.request_count} 次 · 违规请求 ${audit.violation_request_count} 次 · 去重目标 ${audit.violated_entity_ids.length} 个 · 归责第三方损失 ${audit.attributable_third_party_loss_ids.length} 个`:'尚无裁判交战记录。';
  panel.appendChild(text);
  if(time>=END&&DATA.meta.verdictReason){const reason=document.createElement('p');reason.className='source-label';reason.textContent='最终判定依据：'+DATA.meta.verdictReason;panel.appendChild(reason)}
  for(const event of DATA.engagement.events.filter(e=>e.tick<=time).slice(-12)){
   const row=document.createElement('p');row.className='source-label';
   const kinds={identification:'特征识别',evidence_refresh:'证据更新',identification_expired:'证据到期',attack_request:'攻击请求',attack_executed:'实际发射',third_party_damage:'第三方损伤归因'};
   row.textContent=`t${event.tick} ${kinds[event.type]||event.type} · ${engagementEvidenceText(event)}`;panel.appendChild(row);
   const details=document.createElement('details');details.className='source-label';
   const summary=document.createElement('summary');summary.textContent='查看原始裁判凭据';
   const raw=document.createElement('pre');raw.style.whiteSpace='pre-wrap';raw.style.overflowWrap='anywhere';raw.textContent=JSON.stringify(event,null,2);
   details.append(summary,raw);panel.appendChild(details);
  }
 }else{
  text.textContent='仅展示本方授权范围内的接触和设备特征，含结算期末状态；未知目标的拒绝反馈不含身份。';panel.appendChild(text);
  for(const contact of perspectiveContacts){
   const row=document.createElement('p');row.className='source-label';
   const identity=contact.identification||{};
   const now=Math.floor(displayTime()),limit=DATA.engagement.contract.maximum_fire_contact_age_ticks;
   const usable=identity.fire_authorized&&now<=identity.valid_until_tick&&contact.age_ticks<=limit;
   const evidence=identity.evidence_tick==null?'尚无有效光电证据':`证据 t${identity.evidence_tick} · 有效至 t${identity.valid_until_tick} · 剩余 ${Math.max(0,identity.valid_until_tick-now)} tick · 接触年龄 ${contact.age_ticks}/${limit}`;
   row.textContent=`${contact.contact_id} · ${identity.status||'unknown'} · ${usable?'当前可请求交战':'当前不可请求交战'} · ${identity.observed_features?.equipment||'尚未获得设备特征'} · ${evidence}`;panel.appendChild(row);
  }
  if(DATA.meta.participantOnly)for(const event of DATA.engagement.events.filter(e=>e.tick<=time).slice(-6)){
   const row=document.createElement('p');row.className='source-label';
   row.textContent=`t${event.tick} ${event.type} · ${event.contact_id||event.action_id||''} · ${event.response||event.status||'执行状态'}`;panel.appendChild(row);
  }
 }
}

function frame(now){const dt=Math.min(.1,(now-previous)/1000);previous=now;if(playing){time=Math.min(END,time+dt*(tour&&time>DATA.meta.quietAfter?120:speed)/tickSeconds());if(time>=END)setPlaying(false)}updateEntities();updateShots();if(now-lastUI>90){updateUI();for(const e of DATA.entities)updateTrail(e,meshes.get(e.id),time);lastUI=now}updateCamera(dt);updateLabels();sea.material.uniforms.uTime.value=now/1000;renderer.render(scene,camera);requestAnimationFrame(frame)}
function selectEntity(id){if(!E.has(id)||!visibleInPerspective(E.get(id)))return;selected=id;updateUI(true);if(following)targetOrbit.span=10}
function seek(t){time=Math.max(0,Math.min(END,t));refreshPerspective();updateEntities();updateShots();updateUI(true);for(const e of DATA.entities)updateTrail(e,meshes.get(e.id),time);if(time>=END)setPlaying(false)}
function setPlaying(value){playing=value;$('play').textContent=value?'Ⅱ 暂停':'▶ 播放';$('play').setAttribute('aria-label',value?'暂停回放':'播放回放')}
function startDemo(){setPerspective('god');following=false;$('follow').setAttribute('aria-pressed','false');tour=false;$('tour').checked=false;speed=1;$('speed').value='1';seek(0);setPlaying(true)}
function drawTimeline(){
 const canvas=$('timeline-events'),ctx=canvas.getContext('2d'),width=canvas.clientWidth||1000;
 canvas.width=width*devicePixelRatio;canvas.height=40*devicePixelRatio;ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);ctx.clearRect(0,0,width,40);ctx.font='10px Segoe UI';
 ctx.strokeStyle='#223a47';for(const y of [10,29]){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke()}
 for(const s of DATA.shots){const x=s.t/END*width,y=E.get(s.a).kind==='air'?10:29;ctx.strokeStyle=s.side==='blue'?'#62dfec':'#ff8877';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x,y-4);ctx.lineTo(x,y+4);ctx.stroke()}
 for(const h of DATA.hits){const x=h.t/END*width,y=E.get(h.target).kind==='air'?10:29;ctx.fillStyle='#e9ce91';ctx.beginPath();ctx.arc(x,y,2.2,0,Math.PI*2);ctx.fill()}
 for(const ev of DATA.events.filter(e=>e.type==='facility-hit')){const x=ev.t/END*width;ctx.fillStyle='#ff8877';ctx.beginPath();ctx.moveTo(x,15);ctx.lineTo(x+4,20);ctx.lineTo(x,25);ctx.lineTo(x-4,20);ctx.closePath();ctx.fill()}
 if(DATA.meta.quietAfter<END-40){ctx.fillStyle='#718796';ctx.fillText('等待原始结算',Math.max(0,Math.min(width-90,(DATA.meta.quietAfter+12)/END*width)),35)}
}

function setupPointer(){setupMapPointer()}
$('play').addEventListener('click',()=>{if(time>=END)seek(0);setPlaying(!playing)});$('restart').addEventListener('click',()=>seek(0));$('replay-again').addEventListener('click',startDemo);$('speed').addEventListener('change',e=>speed=+e.target.value);$('scrub').addEventListener('input',e=>seek(+e.target.value));$('tour').addEventListener('change',e=>{tour=e.target.checked;updateUI(true)});$('trails').addEventListener('change',e=>{showTrails=e.target.checked;for(const en of DATA.entities)updateTrail(en,meshes.get(en.id),time)});$('follow').addEventListener('click',()=>{following=!following;$('follow').setAttribute('aria-pressed',following);if(following)targetOrbit.span=10});$('oblique').addEventListener('click',()=>{targetOrbit.phi=.75;$('oblique').setAttribute('aria-pressed','true');$('topdown').setAttribute('aria-pressed','false')});$('topdown').addEventListener('click',()=>{targetOrbit.phi=.001;targetOrbit.theta=0;$('topdown').setAttribute('aria-pressed','true');$('oblique').setAttribute('aria-pressed','false')});$('overview').addEventListener('click',()=>showOverview(sceneOverview()));
function fullscreen(){if(window.toggleMapFullscreen){window.toggleMapFullscreen();return}if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen().catch(()=>{})}$('fullscreen').addEventListener('click',fullscreen);$('about-button').addEventListener('click',()=>$('about').showModal());$('metrics-button').addEventListener('click',()=>$('metrics').showModal());$('finish-metrics').addEventListener('click',()=>$('metrics').showModal());document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close()}}));
document.addEventListener('keydown',e=>{if(document.querySelector('dialog[open]')||['INPUT','SELECT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName))return;if(e.code==='Space'){e.preventDefault();if(time>=END)seek(0);setPlaying(!playing)}if(e.code==='ArrowRight'){e.preventDefault();seek(time+5/tickSeconds())}if(e.code==='ArrowLeft'){e.preventDefault();seek(time-5/tickSeconds())}if(e.key.toLowerCase()==='f')fullscreen()});
function mountEpisode(){
 const s=currentScenario(),m=DATA.meta;
 finalBuoyLosses=neutralBuoyLosses(DATA);
 updateBuoyLossReport();
 const diagnostics=$('diagnostic-summary');diagnostics.textContent=(m.warnings||[]).length?'诊断提示：'+m.warnings.join(' '):'轨迹、来源版本、发射数与评分核对结果见“回放说明”。';
 const pill=document.querySelector('.status-pill');pill.textContent=outcomeName(DATA);pill.classList.toggle('failed',m.success!==true);
 $('scrub').max=END;$('duration').textContent=' / '+fmt(END);$('seed').textContent=m.seed;$('case-version').textContent=m.sourceVersion;
 $('current-scenario').textContent=s.name;$('current-description').textContent=s.description;$('map-title').textContent=s.name+' · 空海态势';
 $('neutral-max').textContent=' / '+m.plannedThreats;$('own-max').textContent=' / '+m.combatUnits;
 $('match-meta').textContent=m.sourceVersion+' · 策略 '+m.policy+' · 种子 '+m.seed+' · '+m.plannedThreats+' 个武装威胁'+(m.plannedDecoys?' + '+m.plannedDecoys+' 个诱饵':'')+' · '+m.facilityCount+' 处设施';
 const group=PACK.episodes.filter(e=>e.meta.scenario===m.scenario);
 const method=$('method-filter');method.replaceChildren();
 for(const id of ['all',...new Set(group.map(e=>e.meta.policy))]){const option=document.createElement('option');option.value=id;option.textContent=id==='all'?'全部方法':policyName(id);method.appendChild(option)}
 if(![...method.options].some(o=>o.value===policyFilter))policyFilter='all';method.value=policyFilter;
 const episodeSelect=$('episode');episodeSelect.replaceChildren();
 for(const i of filteredEpisodeIndices()){
  const e=PACK.episodes[i],option=document.createElement('option');option.value=i;
  option.textContent=s.name+' · '+policyName(e.meta.policy)+' · 种子 '+e.meta.seed;
  episodeSelect.appendChild(option);
 }
 episodeSelect.value=episodeIndex;episodeSelect.title=episodeSelect.selectedOptions[0]?.textContent||'';

 $('method-note').textContent='按实际记录列出策略，不限定为五种规则；大小模型信息需由记录声明。对照应核对同场景、同种子、同蓝方条件和同运行时；单局画面不能证明统计区分度。';
 const coverage=PACK.comparison?.coverage?.find(row=>row.scenario===m.scenario);
 $('baseline-coverage').textContent=coverage?(PACK.comparison?.originLabel||'本地')+'对照进度 '+coverage.recorded+' / 5；正常结束 '+coverage.completed+' 局'+(coverage.failures?'，程序异常 '+coverage.failures+' 局':''):'本题已有 '+new Set(group.map(e=>e.meta.policy)).size+' 种方法 · '+group.length+' 份记录';
 $('next-episode').disabled=filteredEpisodeIndices().length<2;
 const critical=criticalEvent(DATA);$('failure-jump').hidden=!critical;$('failure-jump').textContent=critical?(m.success?'查看设施受损':'观看突破过程')+' ↗ '+fmt(critical.t):'';
 $('finish-score').textContent=scoreText(m.score);$('finish-title').textContent=outcomeName(DATA);$('finish-eyebrow').textContent=(m.status||'completed')!=='completed'?'INCOMPLETE RUN':m.success?'MISSION COMPLETE':'OBJECTIVE INCOMPLETE';$('finish-eyebrow').style.color=m.success?'var(--green)':'var(--red)';
 $('finish-copy').textContent='已停止威胁（含自爆） '+m.finalNeutral+' / '+m.plannedThreats+' · 可用设施 '+m.finalOperational+' / '+m.facilityCount+'\n己方仍可行动 '+m.finalOwnActive+' / '+m.combatUnits+' 个作战单元'+(m.success&&m.finalOperational<m.facilityCount?'\n任务判定成功，仍有部分设施损失。':'');
 if(m.participantOnly){$('finish-score').textContent='未包含';$('finish-eyebrow').textContent='AUTHORIZED OBSERVATIONS';$('finish-eyebrow').style.color='var(--muted)';$('finish-copy').textContent='此文件只包含参赛者实际收到的观察、公开反馈和己方状态。任务成败、威胁处置与分数请查看独立裁判回放。'}
 $('diagnostics').textContent=JSON.stringify({status:m.status,terminal:m.terminal,mapVisualization:geographyFor()?{mapRef:geographyFor().mapRef,sourceSha256:geographyFor().sourceSha256,role:DATA.visualization?.role||'scenario-bound',originalScenarioMapRef:DATA.visualization?.originalScenarioMapRef??DATA.geographyRef??null,coordinateTransform:DATA.visualization?.coordinateTransform||'native',coastlines:geographyFor().coastlineLines.length,harborLines:geographyFor().harborLines.length}:null,checks:DATA.checks,warnings:m.warnings,combatRejections:m.combatRejections,decisionTimeouts:m.decisionTimeouts,error:m.error},null,2);$('source-path').textContent=m.campaign+'/'+m.casePath;$('source-sha').textContent=m.traceSha256;$('source-version').textContent=m.sourceVersion;
 $('metric-total').textContent=scoreText(m.score);$('metrics-episode').textContent=s.name+' · '+outcomeName(DATA);
 $('metric-note').textContent=((m.status||'completed')==='completed'?'任务判定与连续得分分别来自原始裁判结果。':'这是未完成 / 异常测试；若有数值，仅为原结果记录的阶段性得分，不代表完整成绩。')+' 分项按本局实际权重显示；诱饵不计入武装威胁中和。'+(m.success&&m.facilityDamaged?' 本局成功条件已满足，仍有设施受损。':'');
 if(m.participantOnly){$('metric-total').textContent='未包含';$('metric-note').textContent='此文件未包含裁判结算。识别特征和攻击请求的公开反馈见识别与交战记录。';$('source-path').textContent='授权观察导出';$('source-sha').textContent='裁判摘要未包含'}
 $('metric-rows').replaceChildren();for(const metric of DATA.metrics){const row=document.createElement('div');row.className='metric-row';const label=document.createElement('span');label.textContent=metric.name+' '+Math.round(metric.weight*100)+'%';const bar=document.createElement('div');bar.className='metric-bar';const fill=document.createElement('i');fill.style.width=Math.max(0,Math.min(100,metric.value*100))+'%';bar.appendChild(fill);const points=document.createElement('span');points.className='points';points.textContent=metric.points.toFixed(2)+' 分';row.append(label,bar,points);$('metric-rows').appendChild(row)}
 $('facility-list').replaceChildren();for(const e of protectedEntities()){const row=document.createElement('button');row.className='facility-row';row.dataset.facility=e.id;row.type='button';const icon=document.createElement('span');icon.className='facility-icon';icon.textContent=e.id.includes('pier')||e.id.includes('berth')?'▱':'⌂';const info=document.createElement('div');info.className='facility-info';info.textContent=e.name;const health=document.createElement('div');health.className='facility-health';health.appendChild(document.createElement('i'));info.appendChild(health);const value=document.createElement('span');value.className='value';row.append(icon,info,value);row.addEventListener('click',()=>{selectEntity(e.id);const f=focusFor(DATA,[e.id],time);targetOrbit.center.set(...f.focus);targetOrbit.span=f.span});$('facility-list').appendChild(row)}
 $('chapter-nav').replaceChildren();chapters.forEach((c,i)=>{const b=document.createElement('button');b.dataset.chapter=i;b.textContent=fmt(c.t)+' '+c.name;b.addEventListener('click',()=>seek(c.t));$('chapter-nav').appendChild(b)});
 $('time-ticks').replaceChildren();for(const tick of [0,END*.25,END*.5,END*.75,END]){const label=document.createElement('span');label.textContent=fmt(tick);label.style.cssText='position:absolute;left:'+(tick/END*100)+'%;transform:translateX('+(tick===0?'0':tick===END?'-100%':'-50%')+')';$('time-ticks').appendChild(label)}
 document.querySelectorAll('[data-scenario]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scenario===m.scenario)));
 mountTaskPanel();
 mountStrategyPanel();
}

let switchGeneration=0;
window.__openmdReplayChunks=Object.create(null);
async function loadEpisodeFile(episode){
 if(!episode.lazyFile)return episode;
 const blob=await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=new URL(episode.lazyFile,location.href).href;
  script.onload=()=>{const value=window.__openmdReplayChunks[episode.lazyKey];delete window.__openmdReplayChunks[episode.lazyKey];script.remove();typeof value==='string'?resolve(value):reject(new Error('该局数据文件未提供完整记录'))};
  script.onerror=()=>{script.remove();reject(new Error('找不到该局数据文件，请保留 HTML 旁的“IE回放数据”文件夹'))};document.body.appendChild(script)});
 return {...episode,lazyData:blob};
}
async function switchEpisode(n){
 if(!Number.isInteger(n)||n<0||n>=PACK.episodes.length)return;
 const previousTime=time,previousScenario=DATA.meta.scenario;
 const generation=++switchGeneration;setPlaying(false);let next=PACK.episodes[n];
 if((next.lazyData||next.lazyFile)&&!next.entities){try{$('diagnostic-summary').textContent='正在加载该局完整记录…';next=await loadEpisodeFile(next);next={...await inflateEpisode(next.lazyData),lazyData:next.lazyData,lazyFile:next.lazyFile,lazyKey:next.lazyKey}}catch(error){$('diagnostic-summary').textContent='该局记录加载失败：'+error.message;return}}
 if(generation!==switchGeneration)return;
 if(policyFilter!=='all'&&next.meta.policy!==policyFilter)policyFilter='all';
 if(PACK.lazyEpisodes&&episodeIndex!==n){const previous=PACK.episodes[episodeIndex];PACK.episodes[episodeIndex]={meta:previous.meta,public:previous.public,...(previous.lazyFile?{lazyFile:previous.lazyFile,lazyKey:previous.lazyKey}:{lazyData:previous.lazyData})}}
 PACK.episodes[n]=next;episodeIndex=n;DATA=next;E=new Map(DATA.entities.map(e=>[e.id,e]));END=DATA.meta.end;
 perspectiveCache='';viewHistoryCache='';clearContactMarkers();chapters=makeChapters(DATA);time=0;following=false;chapterIndex=-1;lastEventIndex=-1;lastUI=-1;
 selected=initialSelection(DATA);
 $('follow').setAttribute('aria-pressed','false');clearWorld();buildWorld();mountEpisode();
 const view=sceneOverview();targetOrbit.center.set(...view.focus);targetOrbit.span=view.span;orbit.center.copy(targetOrbit.center);orbit.span=targetOrbit.span;
 setPlaying(false);seek($('strategy-keep-tick')?.checked&&DATA.meta.scenario===previousScenario?Math.min(END,previousTime):0);drawTimeline();
}

function currentScenario(){return PACK.scenarios.find(s=>s.id===DATA.meta.scenario)}
function isOperationalLife(life){return life==='active'||life==='degraded'}
function lifeAt(e,t){return stateAt(e,t)}
function protectedEntities(){return DATA.entities.filter(e=>e.protected&&visibleInPerspective(e))}
function scoreText(v){return Number.isFinite(v)?v.toFixed(2):'未结算'}
function initialSelection(d){return (d.entities.find(e=>e.side===participantSide(d)&&e.kind==='air')||d.entities.find(e=>e.side===participantSide(d))||d.entities[0]).id}
function outcomeName(d){const m=d.meta;if(m.participantOnly)return '参赛者授权观察回放';if((m.status||'completed')!=='completed')return {'bounded_probe':'截断探针','algorithm_failure':'算法异常','infrastructure_failure':'基础设施异常'}[m.status]||'未完成';return m.success===true?(m.facilityDamaged?'任务成功 · 设施受损':'任务成功'):m.success===false?'任务未成功':'成败未记录'}

function criticalEvent(d){
 const hits=d.events.filter(e=>e.type==='facility-hit');
 return (!d.meta.success&&hits.find(e=>e.a==='facility.command'))||hits[0]||null;
}
function focusFor(d,ids,t){
 const list=ids.map(id=>d.entities.find(e=>e.id===id)).filter(Boolean);
 if(!list.length)return {focus:d.meta.overview.focus,span:d.meta.overview.span};
 const p=list.map(e=>sampleAt(e,t)),xs=p.map(x=>x[0]/1000),zs=p.map(x=>-x[1]/1000);
 const padding=isCombat(d)?6:.5;
 return {focus:[(Math.min(...xs)+Math.max(...xs))/2,0,(Math.min(...zs)+Math.max(...zs))/2],span:Math.max(isCombat(d)?12:2.5,(Math.max(...xs)-Math.min(...xs)+padding)*1.5,(Math.max(...zs)-Math.min(...zs)+padding)*1.7)};
}
function clearWorld(){
 if(!world)return;
 const geometries=new Set(),materials=new Set();
 world.traverse(o=>{if(o.geometry)geometries.add(o.geometry);if(o.material)(Array.isArray(o.material)?o.material:[o.material]).forEach(m=>materials.add(m))});
 scene.remove(world);geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());
 meshes.clear();shotMeshes.length=0;hitMeshes.length=0;$('labels').replaceChildren();
 rangeLayers.clear();
}
function jumpToCritical(){
 const event=criticalEvent(DATA);if(!event)return;
 const shot=DATA.shots.filter(s=>s.b===event.a&&s.side==='blue'&&s.t<=event.t).sort((a,b)=>b.t-a.t)[0];
 const start=Math.max(0,(shot?shot.t:event.t)-8);
 following=false;$('follow').setAttribute('aria-pressed','false');seek(start);selectEntity(event.a);
 const f=focusFor(DATA,[event.a,shot&&shot.a].filter(Boolean),start);
 targetOrbit.center.set(...f.focus);targetOrbit.span=f.span;setPlaying(true);
}
function mountGallery(){
 const gallery=$('scenario-gallery');gallery.replaceChildren();
 const filter=$('track-filter');filter.replaceChildren();
 const tracks=[['all','全部类别'],['interception','拦截交战'],['reconnaissance','侦察搜索'],['emergency','应急响应']];
 const scenarioTracks=new Map(PACK.scenarios.map(s=>[s.id,PACK.episodes.find(e=>e.meta.scenario===s.id)?.meta.track||'interception']));
 for(const [value,label] of tracks){const count=PACK.scenarios.filter(s=>value==='all'||scenarioTracks.get(s.id)===value).length;if(!count)continue;const option=document.createElement('option');option.value=value;option.textContent=label+' · '+count+' 题';filter.appendChild(option)}
 filter.onchange=()=>{for(const card of gallery.children)card.hidden=filter.value!=='all'&&card.dataset.track!==filter.value};
 for(const s of PACK.scenarios){
  const b=document.createElement('button');b.type='button';b.className='scenario-card';b.dataset.scenario=s.id;
  b.dataset.track=scenarioTracks.get(s.id);
  b.setAttribute('aria-pressed',String(s.id===DATA.meta.scenario));
  const code=document.createElement('small');code.textContent=(s.id.match(/^IE-(?:S\d+|\d+)/)?.[0]||s.id)+' · '+s.tag;
  const name=document.createElement('strong');name.textContent=s.name;
  const desc=document.createElement('span');desc.textContent=s.short;
  b.append(code,name,desc);b.addEventListener('click',()=>switchEpisode(filteredEpisodeIndices(s.id)[0]??PACK.episodes.findIndex(e=>e.meta.scenario===s.id)));
  gallery.appendChild(b);
 }
}

mountGallery();mountEpisode();
$('episode').addEventListener('change',e=>switchEpisode(+e.target.value));
$('method-filter').addEventListener('change',()=>{policyFilter=$('method-filter').value;if(policyFilter!=='all'&&DATA.meta.policy!==policyFilter){const n=PACK.episodes.findIndex(e=>e.meta.scenario===DATA.meta.scenario&&e.meta.policy===policyFilter&&e.meta.seed===DATA.meta.seed),fallback=PACK.episodes.findIndex(e=>e.meta.scenario===DATA.meta.scenario&&e.meta.policy===policyFilter);switchEpisode(n>=0?n:fallback)}else mountEpisode()});
$('next-episode').addEventListener('click',()=>{const indices=filteredEpisodeIndices();switchEpisode(indices[(indices.indexOf(episodeIndex)+1)%indices.length])});
$('failure-jump').addEventListener('click',jumpToCritical);
$('coastlines').addEventListener('change',()=>{if(terrainLayers.coast)terrainLayers.coast.visible=$('coastlines').checked});
$('shelf').addEventListener('change',()=>{if(terrainLayers.shelf)terrainLayers.shelf.visible=$('shelf').checked});
$('task-context').addEventListener('click',()=>showOverview(DATA.meta.overview));
$('map-context').addEventListener('click',()=>{showOverview(geographicOverview());targetOrbit.phi=.001;targetOrbit.theta=0;$('topdown').setAttribute('aria-pressed','true');$('oblique').setAttribute('aria-pressed','false')});
$('jump-end').addEventListener('click',()=>{setPlaying(false);seek(END)});
window.replay={seek,startDemo,select:selectEntity,switchEpisode,jumpToCritical,setPerspective,setDragMode,setSmoothing,stepFrame,exportFrames:downloadFrames,pause:()=>setPlaying(false),play:()=>setPlaying(true),
 state:()=>({time,playing,selected,chapter:chapterIndex,episode:episodeIndex,scenario:DATA.meta.scenario,end:END,score:DATA.meta.score,width:W,height:H,perspective,dragMode,frameMode,smoothDisplay,perspectiveTick,visibleContacts:perspectiveContacts.length,visibleMissiles:shotMeshes.filter(m=>m.glow.visible).length,visibleLaunches:shotMeshes.filter(m=>m.launch.visible).length,recordedFrames:DATA.recording?.frames.length||0,center:targetOrbit.center.toArray(),theta:targetOrbit.theta,phi:targetOrbit.phi,pointerCount:pointerPositions.size,visibleEntities:[...meshes.values()].filter(m=>m.g.visible).length,meshCount:meshes.size,geometries:renderer?renderer.info.memory.geometries:0,coastlines:terrainLayers.coast?.children.length||0,shelves:terrainLayers.shelf?.children.length||0,coastlinesVisible:terrainLayers.coast?.visible||false,shelfVisible:terrainLayers.shelf?.visible||false}),data:()=>DATA,episodes:()=>PACK.episodes.map(e=>e.meta)};
mountReplayControls();
$('energy-roster').addEventListener('toggle',()=>updateEnergyInspector(E.get(selected)));
bindRangeControls();
try{init()}catch(error){const box=document.createElement('div');box.className='fallback';box.innerHTML='<div><h2>三维画面未能启动</h2><p>请使用新版 Edge 或 Chrome 并开启图形加速。本页面包含全部数据，打开时不需要联网。</p></div>';viewport.appendChild(box);console.error(error)}
const query=new URLSearchParams(location.search);if(query.has('episode')){const n=Number(query.get('episode'));if(Number.isInteger(n)&&n>=0&&n<PACK.episodes.length)switchEpisode(n)}
if(query.has('demo'))setTimeout(startDemo,900);

if(query.has('context'))setTimeout(()=>$('map-context').click(),250);
