"""Shared manoeuvre safety using owned DTOs, delivered contacts and public land.

No classification, faction lookup, private route or engagement permission is
consulted. All five decision policies use the same final navigation filter.
"""
from copy import deepcopy
import math
from .coastal_navigation import CoastalRouter


class SurfaceSafety:
    def __init__(self, config=None, land_regions=(), mobile_entity_ids=None):
        self.config=dict(config or {})
        self.enabled=self.config.get('enabled',False)
        self.mobile_entity_ids=None if mobile_entity_ids is None else frozenset(mobile_entity_ids)
        self.router=CoastalRouter(list(land_regions))
        self.contacts={};self.previous={};self.recovery={};self.diagnostics=[]

    def _traffic(self, obs):
        tick=obs['tick'];p=self.config
        for source in ('organic_contacts','shared_contacts'):
            for c in obs.get(source,()):
                position=c.get('estimated_position_m')
                measured=c.get('observed_tick',tick-c.get('age_ticks',0))
                if not position or position[2]>p['surface_maximum_altitude_m'] or measured>tick:continue
                key=(obs['observer_faction_id'],obs['controller_slot_id'],c['contact_id']);previous=self.contacts.get(key)
                if previous and measured<=previous['observed_tick']:continue
                velocity=[0.,0.]
                if previous:
                    delta=measured-previous['observed_tick']
                    velocity=[(position[a]-previous['position_m'][a])/delta for a in (0,1)]
                self.contacts[key]=dict(position_m=list(position),velocity_mps=velocity,observed_tick=measured)
        self.contacts={k:v for k,v in self.contacts.items()
            if 0<=tick-v['observed_tick']<=p['maximum_contact_age_ticks']}
        traffic=[]
        for own in obs['own_entities']:
            if own['position_m'][2]<=p['surface_maximum_altitude_m']:
                traffic.append(dict(key=own['entity_id'],position_m=own['position_m'],
                    velocity_mps=own.get('velocity_mps',(0.,0.,0.)),uncertainty_m=0.,owned=True))
        for key,c in self.contacts.items():
            if key[:2]!=(obs['observer_faction_id'],obs['controller_slot_id']):continue
            age=tick-c['observed_tick'];speed=math.hypot(*c['velocity_mps'])
            # Old delivered reports remain useful for navigation, with explicit
            # extrapolation uncertainty; the shooting freshness gate is separate.
            traffic.append(dict(key=key,
                position_m=[c['position_m'][a]+age*c['velocity_mps'][a] for a in (0,1)],
                velocity_mps=c['velocity_mps'],uncertainty_m=min(100.,age*(1.+.1*speed)),owned=False))
        return traffic

    @staticmethod
    def _closest(position, velocity, hazard, horizon):
        delta=[hazard['position_m'][a]-position[a] for a in (0,1)]
        relative=[hazard['velocity_mps'][a]-velocity[a] for a in (0,1)]
        square=sum(x*x for x in relative)
        when=max(0.,min(horizon,-sum(delta[a]*relative[a] for a in (0,1))/square)) if square>1e-12 else 0.
        return math.hypot(*(delta[a]+when*relative[a] for a in (0,1))),when

    def apply(self, obs, batch):
        if not self.enabled:return batch
        from .agent import navigation_batch
        p=self.config;tick=obs['tick'];traffic=self._traffic(obs);self.diagnostics=[]
        original={c['entity_id']:deepcopy(c['payload']) for c in (batch or {}).get('persistent_commands',())
                  if c['command_type']=='navigation'}
        payloads=dict(original)
        controlled=set(obs['controlled_entity_ids'])
        for own in obs['own_entities']:
            entity=own['entity_id'];pos=own['position_m']
            if entity not in controlled or pos[2]>p['surface_maximum_altitude_m']:
                continue
            if entity not in (original if self.mobile_entity_ids is None else self.mobile_entity_ids):continue
            if not own.get('capability_flags',{}).get('dynamics',True):continue
            nav=payloads.get(entity,dict(speed_mps=0.,heading_deg=own['heading_deg'],altitude_m=pos[2]))
            actual=own.get('velocity_mps',(0.,0.,0.));speed=math.hypot(*actual[:2])
            stopping=math.log(max(1.,speed/p['settled_speed_mps']))/p['braking_drag_per_m']+p['braking_margin_m']
            desired_angle=math.radians(nav['heading_deg'])
            desired=[nav['speed_mps']*math.sin(desired_angle),nav['speed_mps']*math.cos(desired_angle)]
            hazards=[h for h in traffic if not(h['owned'] and h['key']==entity)
                and math.dist(pos[:2],h['position_m'][:2])<=max(p['lookahead_m'],stopping+2*p['clearance_m'])]
            conflicts=[]
            for hazard in hazards:
                radius=p['clearance_m']+hazard['uncertainty_m']
                closest=min(self._closest(pos,actual,hazard,p['prediction_ticks']),
                            self._closest(pos,desired,hazard,p['prediction_ticks']))
                distance=math.dist(pos[:2],hazard['position_m'][:2])
                if closest[0]<radius and (closest[1]>0. or distance<radius):
                    conflicts.append(hazard)
            memory_key=(obs['observer_faction_id'],obs['controller_slot_id'],entity)
            before=self.previous.get(memory_key)
            if before and tick==before['tick']:
                stuck_since=before['stuck_since']
            elif before and tick>before['tick'] and math.dist(pos[:2],before['position_m'])<.5 and nav['speed_mps']>.5:
                stuck_since=before['stuck_since']
            else:stuck_since=tick
            self.previous[memory_key]=dict(tick=tick,position_m=list(pos[:2]),stuck_since=stuck_since)
            stuck=bool(before and tick-stuck_since>=p['stuck_ticks'])
            recovering=self.recovery.get(memory_key)
            if recovering and (tick>=recovering['until'] or math.dist(pos[:2],recovering['goal'])<30.):
                self.recovery.pop(memory_key);recovering=None
            if (conflicts or stuck) and recovering is None:
                reach=max(140.,2*p['clearance_m'],min(stopping,400.))
                candidates=[]
                for offset in (35,60,90,120,-35,-60,-90,-120,180):
                    heading=(own['heading_deg']+offset)%360.
                    angle=math.radians(heading)
                    goal=[pos[0]+reach*math.sin(angle),pos[1]+reach*math.cos(angle)]
                    if not self.router.clear_segment(pos,goal):continue
                    candidate=[p['steerage_speed_mps']*math.sin(angle),p['steerage_speed_mps']*math.cos(angle)]
                    clearance=min((self._closest(pos,candidate,h,p['prediction_ticks'])[0]-h['uncertainty_m'] for h in hazards),default=1000.)
                    # Prefer the common starboard manoeuvre for symmetric
                    # encounters; bearings, never identity labels, break ties.
                    score=(clearance>=p['clearance_m'],min(clearance,2*p['clearance_m']),offset>0,-abs(offset))
                    candidates.append((score,goal,heading))
                if candidates:
                    _score,goal,heading=max(candidates,key=lambda c:c[0])
                    recovering=dict(goal=goal,heading=heading,until=tick+p['recovery_ticks'])
                    self.recovery[memory_key]=recovering
            if recovering:
                angle=math.degrees(math.atan2(recovering['goal'][0]-pos[0],recovering['goal'][1]-pos[1]))%360.
                nav=dict(nav,heading_deg=angle,speed_mps=0. if speed>1.5 else p['steerage_speed_mps'])
            elif conflicts:
                nav=dict(nav,speed_mps=0.)
            # Brake before reaching a coast, including after a traffic detour.
            angle=math.radians(own['heading_deg'])
            coast=[pos[0]+max(stopping,80.)*math.sin(angle),pos[1]+max(stopping,80.)*math.cos(angle)]
            coast_brake=speed>p['settled_speed_mps'] and not self.router.clear_segment(pos,coast)
            if coast_brake:
                nav=dict(nav,speed_mps=0.)
            if entity in original or conflicts or recovering or stuck or coast_brake:
                payloads[entity]=nav
            self.diagnostics.append(dict(entity_id=entity,conflicts=len(conflicts),actual_speed_mps=speed,
                braking_distance_m=stopping,recovering=bool(recovering),stuck=stuck))
        if not payloads:return batch
        replacement=navigation_batch(obs,payloads)
        if batch:
            # Preserve all independently selected shots and non-navigation
            # commands, including their original action identities.
            replacement=deepcopy(batch)
            replacement['persistent_commands']=[c for c in replacement['persistent_commands'] if c['command_type']!='navigation']+navigation_batch(obs,payloads)['persistent_commands']
        return replacement
