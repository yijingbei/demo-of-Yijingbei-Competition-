"""Five autonomous public-observation baselines for the buoy identification scenario."""
import math
from .trajectory_judgment import MotionLedger

BASELINE_MODES=('idle','naive','coordinated','greedy','adaptive')


def contact_policy(brief,mode):
    family=brief['contact_judgment_contract']['reference_policy'].get('strategy_family')
    if family is not None:raise ValueError('unsupported public contact strategy family')
    return BuoyBaselinePolicy(brief,mode)


class BuoyBaselinePolicy:
    def __init__(self,brief,mode):
        if mode not in BASELINE_MODES:raise ValueError('unsupported standard mode')
        self.brief=brief;self.mode=mode;self.config=dict(brief['contact_judgment_contract']['reference_policy'])
        self.motion=MotionLedger(self.config);self.pending={};self.attempts={};self.wait_until={}
        from .action_feedback import FireFeedback
        self.feedback=FireFeedback(brief['contact_judgment_contract'].get('fire_feedback_policy'))
        self.search_leg=0;self.inspected={};self.deliberate_waste_done=False
        self.objectives=[a['initial_position_m'][:2] for a in brief['own_assets'] if a['entity_id'] in brief['facility_weights']]
        assets={a['entity_id']:a for a in brief['own_assets']}
        self.search_points=[[assets[e]['initial_position_m'][0]+3000.,assets[e]['initial_position_m'][1]]
            for e in sorted(brief['own_weapon_policies'])]
        self.search_points.append([brief['objective_m'][0]+6500.,brief['objective_m'][1]])
        self.waste_one_round=False
        from .surface_safety import SurfaceSafety
        self.safety=SurfaceSafety(brief.get('surface_safety'),brief.get('navigation_map',{}).get('land_regions',[]),brief['own_weapon_policies'])

    def _confirm(self,obs,units):
        for entity,p in list(self.pending.items()):
            own=units.get(entity)
            if own is None:continue
            spent=p['rounds_before']-own['ammunition'].get(p['ammunition_ref'],0)
            if spent>0:
                key=self.motion.aliases.key(p['key']);self.attempts[key]=self.attempts.get(key,0)+spent
                self.wait_until[key]=p['wait_until'];self.pending.pop(entity)
                if p['deliberate_waste']:self.deliberate_waste_done=True
            elif obs['tick']>p['tick']+2:self.pending.pop(entity)

    def observe_receipt(self,receipt):
        self.feedback.observe_receipt(receipt)

    def act(self,obs):
        from .agent import navigation_batch
        contacts=self.motion.observe(obs);tick=obs['tick'];p=self.config
        waits={}
        for key,value in self.wait_until.items():
            root=self.motion.aliases.key(key);waits[root]=max(value,waits.get(root,-1))
        self.wait_until=waits
        units={u['entity_id']:u for u in obs['own_entities']};self._confirm(obs,units)
        if self.mode=='idle':return self.safety.apply(obs,None)
        risks={key:self.motion.approaching_risk(key,self.objectives) for key in contacts}
        risked=[(key,c) for key,c in contacts.items() if risks[key][0]>0]
        distance=lambda c:min(math.dist(c['estimated_position_m'][:2],q) for q in self.objectives)
        if self.mode=='naive':targets=list(contacts.items())
        elif self.mode=='greedy':targets=[(k,c) for k,c in contacts.items() if risks[k][1]['span']>=p['minimum_track_span_ticks']]
        else:targets=risked
        targets.sort(key=lambda v:(self.attempts.get(v[0],0) if self.mode=='coordinated' else 0,
            distance(v[1])/max(1.,risks[v[0]][1]['closing_mps']) if self.mode=='adaptive' else distance(v[1]),v[0]))
        # Optional acceptance submission intentionally spends one uncertain shot,
        # still choosing solely from public low-progress motion histories.
        waste=None
        if self.waste_one_round and not self.deliberate_waste_done and not self.pending:
            uncertain=[(k,c) for k,c in contacts.items() if risks[k][1]['span']>=p['minimum_track_span_ticks'] and risks[k][0]==0]
            if uncertain:waste=min(uncertain,key=lambda v:(abs(risks[v[0]][1]['progress_m']),distance(v[1]),v[0]))
        if waste:targets=[waste]
        payloads={};fires=[];reserved=set()
        scout=p['scout_entity_id'];own=units.get(scout)
        if own and scout in obs['controlled_entity_ids']:
            # Older delivered reports remain useful for navigation. The fire
            # age gate below never discards a track or its accumulated history.
            inspect=[(k,c) for k,c in contacts.items() if tick-self.inspected.get(k,-10000)>p['inspection_revisit_ticks']]
            choices=risked or inspect
            target=min(choices,key=lambda v:(distance(v[1]),v[0])) if choices else None
            if target:
                key,c=target;goal=c['estimated_position_m']
                if math.dist(own['position_m'][:2],goal[:2])<=p['inspection_radius_m']:self.inspected[key]=tick
            else:
                goal=self.search_points[self.search_leg]
                if math.dist(own['position_m'][:2],goal[:2])<=900.:
                    self.search_leg=(self.search_leg+1)%len(self.search_points);goal=self.search_points[self.search_leg]
            distance_to=math.dist(own['position_m'][:2],goal[:2])
            heading=math.degrees(math.atan2(goal[0]-own['position_m'][0],goal[1]-own['position_m'][1]))%360.
            payloads[scout]=dict(speed_mps=min(p['scout_speed_mps'],max(0.,distance_to-p['scout_standoff_m'])/4.),
                heading_deg=heading,altitude_m=p['scout_altitude_m'])
        for entity,weapon in sorted(self.brief['own_weapon_policies'].items()):
            if entity not in units or entity not in obs['controlled_entity_ids']:continue
            own=units[entity];payloads[entity]=dict(speed_mps=0.,heading_deg=own['heading_deg'],altitude_m=own['position_m'][2])
            if (entity in self.pending or not own['capability_flags'].get('weapon')
                    or own['ammunition'].get(weapon['ammunition_ref'],0)<=0
                    or own['cooldown_ticks'].get(weapon['weapon_ref'],0)>0):continue
            for key,c in targets:
                if key in reserved or tick-c['observed_tick']>p['fire_max_age_ticks']:continue
                if tick<self.wait_until.get(key,-1):continue
                d=math.dist(own['position_m'],c['estimated_position_m'])
                if not weapon['minimum_range_m']<=d<=weapon['maximum_range_m']:continue
                fires.append((entity,dict(weapon_ref=weapon['weapon_ref'],contact_id=c['contact_id'])))
                reserved.add(key)
                flight=d/max(1.,weapon.get('missile_cruise_speed_mps',100.))
                delay=math.ceil(flight)+p['flight_margin_ticks'] if self.mode=='adaptive' else (20 if self.mode in ('naive','greedy') else p['coordinated_retry_ticks'])
                self.pending[entity]=dict(key=key,tick=tick,rounds_before=own['ammunition'][weapon['ammunition_ref']],
                    ammunition_ref=weapon['ammunition_ref'],wait_until=tick+delay,deliberate_waste=bool(waste))
                break
        return self.safety.apply(obs,self.feedback.apply(obs,navigation_batch(obs,payloads,fires)))
