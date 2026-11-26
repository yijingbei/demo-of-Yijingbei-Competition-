"""Opt-in experimental navigation layer. No engine imports or privileged state.

Inputs are controller-scoped DTOs and published dynamics/geography. Opaque contact
handles are dictionary keys only. Weapons, firing, scoring and RNG are untouched.
"""
import math

VARIANTS=('baseline','sprint25','sprint35','sprint50','drag25','drag35','drag50',
          'beam50','climb50','dive50','zigzag3','zigzag6','vertical6','late3',
          'drag_climb','adaptive','predictive','energy_drag')

def delta(a,b):return (b-a+180.)%360.-180.
def bearing(a,b):return math.degrees(math.atan2(b[0]-a[0],b[1]-a[1]))%360.
def clip(x,lo,hi):return max(lo,min(hi,x))

class EvasionLayer:
    def __init__(self,variant='baseline',config=None,router=None):
        if variant not in VARIANTS:raise ValueError(variant)
        self.variant=variant
        self.cfg=dict(enabled=False,max_speed_mps=50.,turn_rate_deg_s=30.,
            acceleration_mps2=8.,deceleration_mps2=10.,climb_mps=20.,
            min_altitude_m=80.,max_altitude_m=2900.,tick_seconds=1.,
            minimum_planar_track_speed=52.,minimum_quality=.6,samples=2,
            max_sample_gap=6,max_contact_age=3,trigger_seconds=120.,
            trigger_miss_m=300.,hold_after_contact_ticks=6,replan_ticks=3,
            missile_speed_mps=60.,missile_turn_rate=60.,missile_climb_mps=60.,
            energy_idle=0.,energy_motion=1./36000.,energy_reference=18.,
            energy_exponent=2.,energy_reserve=.12)
        self.cfg.update(config or {})
        self.router=router;self.history={};self.states={};self.decisions=[]
        self.ticks={}

    def observe(self,obs):
        if not self.cfg['enabled'] or self.variant=='baseline':return []
        cfg=self.cfg;tick=obs['tick'];dt=cfg['tick_seconds']
        scope=(obs.get('session_id'),obs['observer_faction_id'],obs['controller_slot_id'])
        selected={}
        for c in [*obs.get('organic_contacts',[]),*obs.get('shared_contacts',[])]:
            age=c.get('age_ticks',999)
            if not(c.get('detected') and c.get('confirmed') and 0<=age<=cfg['max_contact_age'] and c.get('track_quality',c.get('confidence',0.))>=cfg['minimum_quality']):continue
            when=c.get('observed_tick',tick-age)
            if not 0<=when<=tick:continue
            key=c['contact_id']
            if key not in selected or when>selected[key][0]:selected[key]=(when,c)
        tracks=[]
        for handle,(when,c) in selected.items():
            key=(*scope,handle);pos=tuple(c['estimated_position_m']);samples=self.history.get(key,[])
            if samples and when<samples[-1][0]:continue
            if samples and when-samples[-1][0]>cfg['max_sample_gap']:samples=[]
            if not samples or when>samples[-1][0]:samples=[*samples,(when,pos)][-cfg['samples']:]
            self.history[key]=samples
            if len(samples)<cfg['samples']:continue
            mean=sum(t for t,p in samples)/len(samples)
            norm=sum((t-mean)**2 for t,p in samples)
            if norm<=0:continue
            v=tuple(sum((t-mean)*p[a] for t,p in samples)/norm/dt for a in range(3))
            if not cfg['minimum_planar_track_speed']<=math.hypot(*v[:2])<=400.:continue
            tracks.append(dict(contact_id=handle,position_m=tuple(pos[a]+v[a]*(tick-when)*dt for a in range(3)),velocity_mps=v,observed_tick=when))
        if tick%20==0:
            self.history={k:v for k,v in self.history.items() if tick-v[-1][0]<=cfg['max_sample_gap']+cfg['max_contact_age']}
        return tracks

    def _advance(self,pos,heading,speed,nav):
        c=self.cfg;dt=c['tick_seconds']
        heading=(heading+clip(delta(heading,nav['heading_deg']),-c['turn_rate_deg_s']*dt,c['turn_rate_deg_s']*dt))%360.
        speed=clip(speed+clip(nav['speed_mps']-speed,-c['deceleration_mps2']*dt,c['acceleration_mps2']*dt),0.,c['max_speed_mps'])
        r=math.radians(heading)
        return ((pos[0]+speed*math.sin(r)*dt,pos[1]+speed*math.cos(r)*dt,pos[2]+clip(nav['altitude_m']-pos[2],-c['climb_mps']*dt,c['climb_mps']*dt)),heading,speed)

    def _safe(self,own,nav,peers=()):
        pos=tuple(own['position_m']);h=own['heading_deg'];s=math.hypot(*own.get('velocity_mps',(0.,0.,0.))[:2]);dt=self.cfg['tick_seconds']
        for step in range(1,7):
            end,h,s=self._advance(pos,h,s,nav)
            if self.router is not None and not self.router.clear_segment(pos,end):return False
            for p in peers:
                if p['entity_id']==own['entity_id'] or p.get('lifecycle_state','active') not in ('active','degraded'):continue
                v=p.get('velocity_mps',(0.,0.,0.));future=[p['position_m'][a]+v[a]*step*dt for a in range(3)]
                if math.dist(end[:2],future[:2])<45. and abs(end[2]-future[2])<25.:return False
            pos=end
        return True

    def _forecast(self,own,nav,hazards):
        c=self.cfg;pos=tuple(own['position_m']);h=own['heading_deg'];s=math.hypot(*own.get('velocity_mps',(0.,0.,0.))[:2])
        pursuers=[[tuple(t['position_m']),bearing((0.,0.),t['velocity_mps']),math.hypot(*t['velocity_mps'][:2])] for _,_,t in hazards[:2]]
        closest=math.inf;cost=0.;dt=c['tick_seconds']
        horizon=min(45,max(8,int(hazards[0][0])+5))
        for step in range(horizon):
            end,h,s=self._advance(pos,h,s,nav)
            cost+=c['energy_idle']*dt+c['energy_motion']*math.dist(pos,end)*(math.dist(pos,end)/dt/c['energy_reference'])**(c['energy_exponent']-1.)
            for m in pursuers:
                mp,mh,ms=m;want=bearing(mp,end)
                mh=(mh+clip(delta(mh,want),-c['missile_turn_rate']*dt,c['missile_turn_rate']*dt))%360.
                mr=math.radians(mh);ms=c['missile_speed_mps']
                me=(mp[0]+ms*math.sin(mr)*dt,mp[1]+ms*math.cos(mr)*dt,mp[2]+clip(end[2]-mp[2],-c['missile_climb_mps']*dt,c['missile_climb_mps']*dt))
                rel=[mp[a]-pos[a] for a in range(3)];vel=[me[a]-mp[a]-end[a]+pos[a] for a in range(3)]
                norm=sum(v*v for v in vel);f=clip(-sum(rel[a]*vel[a] for a in range(3))/norm,0.,1.) if norm else 0.
                closest=min(closest,math.sqrt(sum((rel[a]+f*vel[a])**2 for a in range(3))))
                m[:]=[me,mh,ms]
            pos=end
        # First avoid the 15 m fuse, then retain separation, with a small energy tie-break.
        return min(closest,500.)-20.*cost

    def apply(self,obs,own,navigation,tracks,tags=(),peers=()):
        c=self.cfg;tick=obs['tick'];entity=own['entity_id'];name=self.variant
        if not c['enabled'] or name=='baseline' or 'uav' not in tags or own.get('lifecycle_state','active') not in ('active','degraded'):return navigation
        key=(obs.get('session_id'),obs['observer_faction_id'],obs['controller_slot_id'],entity)
        pos=own['position_m'];velocity=own.get('velocity_mps',(0.,0.,0.));hazards=[]
        for t in tracks:
            r=[t['position_m'][a]-pos[a] for a in range(3)];v=[t['velocity_mps'][a]-velocity[a] for a in range(3)]
            dot=sum(r[a]*v[a] for a in range(3));norm=sum(x*x for x in v)
            if dot>=0 or norm<1.:continue
            when=-dot/norm;miss=math.sqrt(sum((r[a]+when*v[a])**2 for a in range(3)))
            if when<=c['trigger_seconds'] and miss<=c['trigger_miss_m']:hazards.append((when,math.dist(pos,t['position_m']),t))
        hazards.sort(key=lambda row:(row[0],row[1],row[2]['contact_id']))
        state=self.states.get(key)
        if name=='late3' and state is None and hazards and hazards[0][0]>3.:return navigation
        if not hazards:
            if state is None:return navigation
            if tick-state['seen']>c['hold_after_contact_ticks']:
                self.states.pop(key,None);return navigation
            nav=dict(state['nav'])
        else:
            if state is None:
                state=dict(start=tick,altitude=pos[2],heading=own['heading_deg'],seen=tick,planned=-999)
                self.states[key]=state
            state['seen']=tick
            if tick-state['planned']<c['replan_ticks'] and name not in ('zigzag3','zigzag6','vertical6'):
                nav=dict(state['nav'])
            else:
                toward=bearing(pos,hazards[0][2]['position_m']);away=(toward+180.)%360.
                beam=min(((toward-90.)%360.,(toward+90.)%360.),key=lambda h:abs(delta(own['heading_deg'],h)))
                elapsed=(tick-state['start'])*c['tick_seconds']
                speed=25. if name.endswith('25') else 35. if name.endswith('35') else 50.
                h=own['heading_deg'] if name.startswith('sprint') else away
                z=state['altitude']
                if name in ('beam50','climb50','dive50','late3'):h=beam
                if name in ('climb50','drag_climb'):z+=350.
                if name=='dive50':z-=350.
                if name.startswith('zigzag'):
                    period=3 if name=='zigzag3' else 6
                    h=(away+(65. if int(elapsed)//period%2==0 else -65.))%360.
                if name=='vertical6':z+=(150. if int(elapsed)//6%2==0 else -150.)
                if name=='adaptive' and hazards[0][1]<350.:
                    h=beam;z+=(150. if hazards[0][2]['position_m'][2]<=pos[2] else -150.)
                if name=='energy_drag':
                    energy=own.get('energy_state',{}).get('value',own.get('energy',1.))
                    speed=50. if energy is None or energy>.25 else 35.
                nav=dict(navigation,speed_mps=min(speed,c['max_speed_mps']),heading_deg=h,altitude_m=clip(z,c['min_altitude_m'],c['max_altitude_m']))
                if name=='predictive':
                    candidates=[]
                    for heading in (away,beam,(beam+180.)%360.,own['heading_deg']):
                        for sp in (35.,50.):
                            for dz in (-120.,0.,120.):
                                candidate=dict(nav,heading_deg=heading,speed_mps=min(sp,c['max_speed_mps']),altitude_m=clip(pos[2]+dz,c['min_altitude_m'],c['max_altitude_m']))
                                if self._safe(own,candidate,peers):candidates.append(candidate)
                    if candidates:nav=max(candidates,key=lambda v:self._forecast(own,v,hazards))
                state.update(planned=tick,nav=dict(nav),contact=hazards[0][2]['contact_id'])
        guarded=False
        if (self.router is not None or peers) and not self._safe(own,nav,peers):
            guarded=True;alternatives=[]
            for offset in (30.,-30.,60.,-60.,90.,-90.,120.,-120.,180.):
                candidate=dict(nav,heading_deg=(nav['heading_deg']+offset)%360.)
                if self._safe(own,candidate,peers):alternatives.append(candidate)
            if alternatives:nav=alternatives[0]
            else:nav=dict(navigation)
        self.decisions.append(dict(tick=tick,controller=obs['controller_slot_id'],entity_id=entity,variant=name,navigation=dict(nav),contact_id=state.get('contact'),fresh_threat=bool(hazards),guarded=guarded,ttc_s=hazards[0][0] if hazards else None))
        return nav
