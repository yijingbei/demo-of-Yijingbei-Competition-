"""Anonymous native contact transport and diagnostic-only bilateral fire ledger.

No observation is classified, no identity-based action is denied, and no score
or terminal is changed. Native authority/contact/ROE/envelope/resource checks
remain authoritative. Truth below is solely for the separate judge ledger.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib,hmac,math,secrets
from .surface_identification import public_receipt

VERSION='autonomous-contact-judgment@2'
LOST={'disabled','destroyed','wreck','despawned'}
CONTACT_FIELDS=frozenset({'contact_id','observer_entity_id','estimated_position_m','observed_tick',
    'age_ticks','detected','confirmed','confidence','track_quality','position_uncertainty_m',
    'classification_confidence','estimated_domain','raw_contact','contact_evidence_version','delivered_tick'})


def declared_factions(resolved):
    """Judge-only ownership includes every declared future reinforcement."""
    result={e.id:e.faction_id for e in resolved.entities}
    for event in resolved.events:
        if event.event_type=='spawn':
            entity=event.payload['entity']
            identifier=entity.id if hasattr(entity,'id') else entity['id']
            result[identifier]=entity.faction_id if hasattr(entity,'faction_id') else entity['faction_id']
    return result


def validate_contract(brief,resolved):
    contract=brief['contact_judgment_contract']
    if 'surface_coast' in brief:
        physical=resolved.world.physical_motion
        if (physical is None or physical.surface_coast is None
                or brief['surface_coast']!=physical.surface_coast.model_dump(mode='json')):
            raise ValueError('public surface coast policy differs from World physics')
    if 'surface_arrival' in brief:
        from .surface_arrival import SurfaceArrival
        SurfaceArrival(brief['surface_arrival'])
        coast=brief.get('surface_coast',{})
        arrival=brief['surface_arrival']
        if (arrival['linear_drag_per_s']!=coast.get('linear_drag_per_s')
                or arrival['low_speed_damping_mps']!=coast.get('healthy_zero_thrust_below_mps')):
            raise ValueError('arrival braking profile must match public coast physics')
    if contract.get('schema_version')!=VERSION:
        raise ValueError('unsupported autonomous contact contract')
    if 'fire_feedback_policy' in contract:
        from .action_feedback import validate_retry_policy
        validate_retry_policy(contract['fire_feedback_policy'])
        if contract['blue_reference_policy'].get('fire_feedback_policy')!=contract['fire_feedback_policy']:
            raise ValueError('both sides must publish the same fire feedback policy')
    for profile in ('reference_policy','blue_reference_policy'):
        p=contract[profile]
        for key in ('history_ticks','navigation_max_age_ticks','fire_max_age_ticks','minimum_track_span_ticks'):
            if type(p.get(key)) is not int or p[key]<=0:raise ValueError('invalid motion window: '+key)
        if not p['fire_max_age_ticks']<=p['navigation_max_age_ticks']<=p['history_ticks']:
            raise ValueError('navigation history must not inherit a stricter firing age')
    for key in ('minimum_progress_m','minimum_closing_mps','minimum_directionality'):
        value=contract['reference_policy'].get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('invalid trajectory preference: '+key)
    goals=contract['blue_reference_policy']['public_objectives_m']
    if goals!=[f['position_m'][:2] for f in brief['protected_facilities']]:
        raise ValueError('blue selection goals must be published mission coordinates')
    defence=contract['blue_reference_policy'].get('surface_self_defence')
    if defence:
        if (any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in defence.values())
                or defence['minimum_track_span_ticks']>defence['history_ticks']
                or defence['urgent_range_m']>defence['maximum_range_m'] or defence['minimum_directionality']>1):
            raise ValueError('invalid public surface self-defence preference')
    factions=declared_factions(resolved)
    for side in ('red','blue'):
        if not any(r.source_faction_id==side and r.target_faction_id=='neutral'
                   and r.relationship=='neutral' and r.engagement_permitted
                   for r in resolved.world.roe_rules):
            raise ValueError('both combatants require explicit neutral-target native ROE')
    for metric in resolved.scoring.metrics:
        if any(factions.get(e)=='neutral' for e in metric.plugin_parameters.get('attacker_ids',())):
            raise ValueError('neutral entities must never enter the threat scoring cohort')
    return deepcopy(contract)


class AnonymousContacts:
    def __init__(self,brief,resolved,catalog,resolve_contact):
        del catalog
        self.contract=validate_contract(brief,resolved)
        self.resolve_contact=resolve_contact
        self.factions=declared_factions(resolved)
        self.neutral_ids={e for e,f in self.factions.items() if f=='neutral'}
        self.secret=secrets.token_bytes(32)
        self.aliases={};self.events=[];self.requests={};self.projectiles={}
        self.counts={s:dict(requests=0,executed_attacks=0,shots=0,third_party_requests=0,
            third_party_shots=0,third_party_damage_events=0) for s in ('red','blue')}
        self.losses=defaultdict(set)

    def observation(self,native):
        obs=deepcopy(native)
        for key in ('resolved_hash','catalog_hash','model_registry_hash'):
            obs.get('metadata',{}).pop(key,None)
        for field in ('organic_contacts','shared_contacts'):
            for c in obs[field]:
                original=c['contact_id'];slot=obs['controller_slot_id']
                alias='contact.'+hmac.new(self.secret,(slot+':'+original).encode(),hashlib.sha256).hexdigest()[:24]
                record=self.resolve_contact(controller_slot_id=slot,
                    firing_entity_id=obs['controlled_entity_ids'][0] if obs['controlled_entity_ids'] else '',
                    contact_id=original,current_tick=obs['tick'])
                previous=self.aliases.get((slot,alias),(None,None))
                self.aliases[slot,alias]=(original,record.target_entity_id if record else previous[1])
                for key in set(c)-CONTACT_FIELDS:c.pop(key)
                c['contact_id']=alias
                # Native position/time/confidence/source remain unchanged. No
                # EO oracle, equipment labels, classifier answer or authority bit.
            obs[field].sort(key=lambda c:c['contact_id'])
        return obs

    def filter_batch(self,batch,obs):
        if batch is None:return None,[]
        result=deepcopy(batch);side=obs['observer_faction_id']
        for action in result.get('discrete_actions',[]):
            if action['action_type']!='fire_weapon':continue
            handle=action['payload'].get('contact_id')
            original,target=self.aliases.get((obs['controller_slot_id'],handle),(None,None))
            item=dict(type='attack_request',tick=obs['tick'],controller_slot=obs['controller_slot_id'],
                action_id=action['action_id'],attacker_id=action['entity_id'],attacker_faction=side,
                contact_id=handle,target_entity_id=target,third_party=target in self.neutral_ids,
                native_submitted=True,diagnostic_only=True)
            self.events.append(item);self.requests[action['action_id']]=item
            if side in self.counts:
                self.counts[side]['requests']+=1
                self.counts[side]['third_party_requests']+=int(target in self.neutral_ids)
            # Invalid/expired handles also reach the native contact validator;
            # no relationship-dependent application response is introduced.
            action['payload']['contact_id']=original or 'unresolved.anonymous-contact'
        return result,[]

    def combat(self,receipts):
        for row in receipts:
            ex=row.get('execution')
            request=self.requests.get(row['request_id'],{})
            side=self.factions.get(ex['attacker_id']) if ex else request.get('attacker_faction')
            if row.get('status')!='executed' or not ex:
                self.events.append(dict(type='native_fire_result',tick=row.get('tick',request.get('tick')),
                    action_id=row['request_id'],attacker_faction=side,contact_id=request.get('contact_id'),
                    status=row.get('status'),error_code=row.get('error_code'),diagnostic_only=True))
                continue
            for projectile in [*ex.get('launched_missile_ids',()),*ex.get('pending_impact_ids',())]:
                self.projectiles[projectile]=dict(attacker_id=ex['attacker_id'],faction_id=side,
                    request_id=row['request_id'])
            third_party=ex['target_id'] in self.neutral_ids
            if side in self.counts:
                self.counts[side]['executed_attacks']+=1;self.counts[side]['shots']+=ex['requested_shots']
                self.counts[side]['third_party_shots']+=ex['requested_shots'] if third_party else 0
            self.events.append(dict(type='attack_executed',tick=ex['tick'],action_id=row['request_id'],
                attacker_id=ex['attacker_id'],attacker_faction=side,target_entity_id=ex['target_id'],
                contact_id=request.get('contact_id'),shots=ex['requested_shots'],
                third_party=third_party,diagnostic_only=True))

    def feedback(self,controller_slot,receipt,tick):
        for child in public_receipt(receipt or {}).get('execution_receipts',()):
            request=self.requests.get(child.get('child_id'))
            if request is None or request['controller_slot']!=controller_slot:continue
            self.events.append(dict(type='action_feedback_delivered',tick=tick,
                action_id=child['child_id'],attacker_id=request['attacker_id'],
                attacker_faction=request['attacker_faction'],contact_id=request['contact_id'],
                status=child.get('status'),error_code=child.get('error_code'),
                execution_tick=child.get('tick'),diagnostic_only=True))

    def damage(self,receipts):
        for receipt in receipts:
            intents=defaultdict(list)
            for intent in receipt.get('applied_intents',()):
                intents[intent['target_entity_id']].append(intent)
            for change in receipt.get('results',()):
                target=change['target_entity_id']
                if target not in self.neutral_ids or change['health_after']>=change['health_before']:continue
                sources=[];magnitudes=defaultdict(float);other=False
                for intent in intents[target]:
                    p=intent.get('parameters',{});pid=p.get('missile_id') or p.get('pending_impact_id')
                    owner=self.projectiles.get(pid)
                    if owner:
                        sources.append(dict(projectile_id=pid,**owner))
                        magnitudes[owner['faction_id']]+=intent.get('magnitude') or 0.
                    else:other=True
                lost=change['lifecycle_before'] not in LOST and change['lifecycle_after'] in LOST
                attributed=[]
                for side,magnitude in magnitudes.items():
                    if side in self.counts:self.counts[side]['third_party_damage_events']+=1
                    if lost and len(magnitudes)==1 and not other:
                        self.losses[side].add(target);attributed.append(side)
                self.events.append(dict(type='third_party_damage',tick=change['tick'],target_entity_id=target,
                    health_before=change['health_before'],health_after=change['health_after'],loss=lost,
                    weapon_sources=sources,attributed_loss_factions=sorted(attributed),
                    mixed_causes=other or len(magnitudes)>1,diagnostic_only=True))

    def audit(self):
        return dict(schema_version='autonomous-engagement-audit@2',diagnostic_only=True,
            request_count=sum(c['requests'] for c in self.counts.values()),
            executed_attack_count=sum(c['executed_attacks'] for c in self.counts.values()),
            by_faction={s:dict(c,attributable_third_party_loss_ids=sorted(self.losses[s])) for s,c in self.counts.items()})

    def drain(self):
        events,self.events=self.events,[]
        return events
