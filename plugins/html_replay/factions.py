"""Map faction identities to fixed colors without changing recorded evidence."""


def convention(scene, brief):
    participant = brief.get('defence', {}).get('faction_id') or brief.get('evaluated_side') or 'coalition.defender'
    declared = brief.get('faction_convention', {})
    mapping = {'blue':'blue', 'red':'red', 'coalition.intruder':'blue', 'coalition.defender':'red'}
    if declared.get('roles', {}).get('neutral') == 'noncombatant':
        mapping['neutral'] = 'neutral'
    factions = {e['faction_id'] for e in scene['entities']}
    if factions - set(mapping):
        raise ValueError('Replay needs an explicit supported faction-to-color mapping')
    if participant not in mapping:
        raise ValueError('Replay participant faction is undeclared')
    track = brief.get('track', 'interception')
    roles = declared.get('roles') or {'interception':{'blue':'attacker','red':'defender'},
        'reconnaissance':{'blue':'opposing_targets','red':'reconnaissance'},
        'emergency':{'blue':'environment','red':'responder'}}.get(track, {'blue':'opponent','red':'participant'})
    return dict(schema_version='competition-factions@1', colors={f:mapping[f] for f in sorted(factions)},
                roles=roles, participant_faction_id=participant, participant_side=mapping[participant])
