/* Read-only coordinate readout. Uses the replay's own projection and display samples. */
(() => {
 'use strict';
 const style=document.createElement('style');
 style.textContent='#map-coordinate-readout{position:absolute;left:12px;bottom:12px;z-index:12;pointer-events:none;max-width:calc(100% - 24px);padding:6px 9px;border:1px solid #45617088;border-radius:5px;background:#07131de0;color:#d5e7ef;font:11px/1.65 "Consolas","Microsoft YaHei",monospace;white-space:pre-line;font-variant-numeric:tabular-nums;text-shadow:0 1px 2px #000}#map-coordinate-readout:empty{display:none}@media(max-width:650px){#map-coordinate-readout{font-size:10px;left:6px;bottom:6px}}';
 document.head.appendChild(style);
 const output=document.createElement('output');output.id='map-coordinate-readout';
 output.setAttribute('aria-label','鼠标地图坐标与单位坐标');
 output.title='鼠标坐标是视线与地图零高度平面的交点；单位高度来自回放位置记录，未使用画面高度放大值。';
 viewport.appendChild(output);
 const picker=new THREE.Raycaster(),ndc=new THREE.Vector2(),intersection=new THREE.Vector3();
 // point([x,y,z]) = [x/1000, z/1000*2.2+.05, -y/1000].
 const plane=new THREE.Plane(new THREE.Vector3(0,1,0),-.05);
 let pointer=null,lastUpdate=-Infinity,lastText='',lastSnapshot=null;
 const finite=n=>typeof n==='number'&&Number.isFinite(n);
 function geographic(position){
  const origin=geographyFor()?.originWgs84;
  if(!Array.isArray(origin)||origin.length<2||!origin.every(finite)||!position.slice(0,3).every(finite))return null;
  const cos=Math.cos(origin[1]*Math.PI/180);if(Math.abs(cos)<1e-10)return null;
  // Exact inverse of geography.py's local map projection, not a global geodesic.
  const lon=((origin[0]+position[0]/(6378137*cos)*180/Math.PI+180)%360+360)%360-180;
  const lat=origin[1]+position[1]/6378137*180/Math.PI;
  if(lat < -90 || lat > 90)return null;
  return {longitude:lon,latitude:lat,height:position[2]};
 }
 const pair=g=>'经度 '+g.longitude.toFixed(6)+'°  纬度 '+g.latitude.toFixed(6)+'°';
 function update(){
  const lines=[],snapshot={episode:episodeIndex,time,mouse:null,unit:null};
  let hoverId=null;
  const bounds=renderer.domElement.getBoundingClientRect();
  const inside=pointer&&pointer.x>=bounds.left&&pointer.x<=bounds.right&&pointer.y>=bounds.top&&pointer.y<=bounds.bottom;
  if(inside&&bounds.width>0&&bounds.height>0){
   const target=document.elementFromPoint(pointer.x,pointer.y);
   // Toolbar/modal overlays do not designate a position on the underlying map.
   const onMap=target===renderer.domElement||target===renderScene||!!target?.closest('.entity-label');
   if(onMap){
    ndc.set((pointer.x-bounds.left)/bounds.width*2-1,-(pointer.y-bounds.top)/bounds.height*2+1);
    camera.updateMatrixWorld();picker.setFromCamera(ndc,camera);
    if(picker.ray.intersectPlane(plane,intersection)){
     const p=[intersection.x*1000,-intersection.z*1000,0],g=geographic(p);
     snapshot.mouse={position:p,geographic:g};lines.push(g?'鼠标 · '+pair(g):'鼠标 · 地图缺少有效经纬度基准');
    }else lines.push('鼠标 · 当前视线未与地图平面相交');
    hoverId=target?.closest('.entity-label')?.dataset.entityId||null;
    if(!hoverId){
     const hit=picker.intersectObjects([...meshes.values()].filter(m=>m.g.visible).map(m=>m.g),true)[0];
     let object=hit?.object;while(object&&!object.userData.entityId)object=object.parent;
     hoverId=object?.userData.entityId||null;
    }
   }
  }
  if(!lines.length)lines.push('鼠标 · 移入地图查看经纬度');
  const id=hoverId||selected,entity=E.get(id),mesh=meshes.get(id);
  if(entity&&mesh?.g.visible&&visibleInPerspective(entity)){
   const p=displayedSample(entity);
   if(p&&p.slice(0,3).every(finite)){
    const g=geographic(p),mode=hoverId?'悬停':'选中';
    snapshot.unit={id,mode,position:p.slice(0,3),geographic:g};
    lines.push(mode+' · '+id+'\n'+(g?pair(g):'经纬度基准不可用')+'  高度 '+p[2].toFixed(1)+' m');
    if(entity.source==='anchors')lines.push('位置来源：稀疏锚点示意');
   }
  }
  const text=lines.join('\n');if(text!==lastText){output.textContent=text;lastText=text}
  lastSnapshot=snapshot;
 }
 viewport.addEventListener('pointermove',event=>{pointer={x:event.clientX,y:event.clientY}},{passive:true});
 viewport.addEventListener('pointerleave',()=>{pointer=null;lastUpdate=-Infinity},{passive:true});
 window.addEventListener('blur',()=>{pointer=null;lastUpdate=-Infinity});
 // Throttled: no geometry mutation, no extra renderer, at most ten raycasts/sec.
 function refresh(now){if(now-lastUpdate>=100){lastUpdate=now;update()}requestAnimationFrame(refresh)}
 requestAnimationFrame(refresh);
 window.replayCoordinates={version:'1.0',state:()=>lastSnapshot,toGeographic:geographic};
})();
