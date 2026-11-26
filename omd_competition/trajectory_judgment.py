"""Motion estimates from delivered anonymous observations; no referee labels."""
import math
from .contact_aliases import PublicContactAliases


class MotionLedger:
    def __init__(self,config):
        self.config=dict(config);self.aliases=PublicContactAliases();self.samples={};self.latest_reports={}

    def observe(self,obs):
        tick=obs['tick'];contacts=obs['organic_contacts']+obs['shared_contacts']
        self.aliases.observe(contacts,tick)
        merged={}
        for key,history in self.samples.items():merged.setdefault(self.aliases.key(key),{}).update(history)
        self.samples=merged;latest={}
        for key,c in self.latest_reports.items():
            if 0<=tick-c['observed_tick']<=self.config['navigation_max_age_ticks']:
                root=self.aliases.key(key)
                if root not in latest or c['observed_tick']>latest[root]['observed_tick']:latest[root]=c
        for c in contacts:
            if not c.get('confirmed') or c.get('estimated_domain') not in ('surface','land',None):continue
            age=tick-c['observed_tick']
            if not 0<=age<=self.config['navigation_max_age_ticks']:continue
            key=self.aliases.key(c['contact_id']);point=tuple(c['estimated_position_m'][:2])
            self.samples.setdefault(key,{})[c['observed_tick']]=point
            prior=latest.get(key)
            if prior is None or (c['observed_tick'],c.get('confidence',0),c['contact_id'])>(prior['observed_tick'],prior.get('confidence',0),prior['contact_id']):latest[key]=c
        self.samples={k:{t:p for t,p in v.items() if tick-t<=self.config['history_ticks']} for k,v in self.samples.items()}
        self.samples={k:v for k,v in self.samples.items() if v}
        self.latest_reports={k:dict(c,age_ticks=tick-c['observed_tick']) for k,c in latest.items()}
        return self.latest_reports

    def features(self,key,objectives):
        history=sorted(self.samples.get(key,{}).items())
        if len(history)<2:return dict(span=0,progress_m=0.,closing_mps=0.,speed_mps=0.,directionality=0.,path_m=0.)
        first,last=history[0],history[-1];span=last[0]-first[0]
        path=sum(math.dist(a[1],b[1]) for a,b in zip(history,history[1:]))
        distance=lambda p:min(math.dist(p,q[:2]) for q in objectives)
        progress=distance(first[1])-distance(last[1])
        return dict(span=span,progress_m=progress,closing_mps=progress/max(1,span),
            speed_mps=path/max(1,span),directionality=progress/max(1.,path),path_m=path)

    def approaching_risk(self,key,objectives):
        f=self.features(key,objectives);p=self.config
        eligible=(f['span']>=p['minimum_track_span_ticks'] and f['progress_m']>=p['minimum_progress_m']
            and f['closing_mps']>=p['minimum_closing_mps'] and f['directionality']>=p['minimum_directionality'])
        return (f['closing_mps']+f['progress_m']/1000.) if eligible else 0.,f


class PublicObjectiveSelector:
    """Blue weighs public mission coordinates and observed approach trajectories.

    No entity IDs, faction tables, buoy routes or true target bindings enter
    this object. Identical public measurements always receive the same score.
    """
    def __init__(self,config):
        self.config=dict(config);self.motion=MotionLedger(config);self.self_defence_contacts=set()

    def _surface_approach(self,key,contact,obs):
        p=self.config.get('surface_self_defence')
        if not p or contact.get('estimated_domain') not in ('surface',None):return None
        history=sorted((t,q) for t,q in self.motion.samples.get(key,{}).items() if obs['tick']-t<=p['history_ticks'])
        if len(history)<2 or history[-1][0]-history[0][0]<p['minimum_track_span_ticks']:return None
        first,last=history[0][1],history[-1][1];span=history[-1][0]-history[0][0]
        path=sum(math.dist(a[1],b[1]) for a,b in zip(history,history[1:]))
        distances=[]
        for own in obs['own_entities']:
            if own['entity_id'] not in obs['controlled_entity_ids'] or own['position_m'][2]>50:continue
            position=own['position_m'][:2];distance=math.dist(last,position)
            # Measure the external trajectory toward the current owned position;
            # our own transit toward a stationary buoy is not hostile motion.
            progress=math.dist(first,position)-distance
            if (distance<=p['maximum_range_m'] and progress>=p['minimum_progress_m']
                    and progress/span>=p['minimum_closing_mps']
                    and progress/max(1.,path)>=p['minimum_directionality']):distances.append(distance)
        return min(distances) if distances else None

    def candidates(self,obs):
        contacts=self.motion.observe(obs);p=self.config;selected=[];self.self_defence_contacts=set()
        for key,c in contacts.items():
            if obs['tick']-c['observed_tick']>p['fire_max_age_ticks']:continue
            f=self.motion.features(key,p['public_objectives_m'])
            distance=min(math.dist(c['estimated_position_m'][:2],q) for q in p['public_objectives_m'])
            if p['mode']=='naive' or (distance<=p['objective_match_radius_m']
                    and f['span']>=p['minimum_track_span_ticks'] and f['speed_mps']<=p['stationary_speed_mps']):
                selected.append((1,distance,c))
            approach=self._surface_approach(key,c,obs)
            if approach is not None:
                self.self_defence_contacts.add(c['contact_id'])
                urgent=approach<=p['surface_self_defence']['urgent_range_m']
                if not any(item[2]['contact_id']==c['contact_id'] for item in selected):
                    selected.append((0 if urgent else 2,approach,c))
        if p.get('air_contact_policy')=='confirmed':
            seen={c['contact_id'] for _,_,c in selected}
            for c in obs['organic_contacts']+obs['shared_contacts']:
                if (c['contact_id'] not in seen and c.get('confirmed') and c.get('detected')
                        and c.get('estimated_domain')=='air'
                        and 0<=obs['tick']-c['observed_tick']<=p['fire_max_age_ticks']):
                    selected.append((1,min(math.dist(c['estimated_position_m'][:2],q) for q in p['public_objectives_m']),c))
                    seen.add(c['contact_id'])
        return [c for _,_,c in sorted(selected,key=lambda row:(row[0],row[1],row[2]['contact_id']))]
