"""Application faction contract: colors are identities, participation is a role."""
BLUE = 'blue'
RED = 'red'


def contract(brief):
    value = brief.get('faction_convention')
    if not isinstance(value, dict) or value.get('schema_version') != 'competition-factions@1':
        raise ValueError('scenario requires the competition faction convention')
    roles = value.get('roles', {})
    if set(roles) not in ({BLUE, RED}, {BLUE, RED, 'neutral'}):
        raise ValueError('faction convention must declare blue and red roles')
    if 'neutral' in roles and (roles['neutral'] != 'noncombatant'
            or not ({'identification_contract','contact_judgment_contract'} & brief.keys())):
        raise ValueError('third party requires an explicit identification scenario')
    if value.get('participant_faction_id') not in {BLUE, RED}:
        raise ValueError('participant faction must be explicit')
    return value


def validate_scenario(resolved, brief):
    value = contract(brief)
    if {f.id for f in resolved.factions} != set(value['roles']):
        raise ValueError('resolved scenario factions differ from the public convention')
    participant = brief.get('defence', {}).get('faction_id', brief.get('evaluated_side'))
    if participant != value['participant_faction_id']:
        raise ValueError('participant controller faction differs from the public convention')
    if brief.get('track') == 'interception' and {
            key: value['roles'][key] for key in (BLUE, RED)} != {BLUE:'attacker', RED:'defender'}:
        raise ValueError('interception requires blue attackers and red defenders')
    if brief.get('track') == 'interception' and (
            participant != RED or brief.get('opponent_contract', {}).get('faction_id') != BLUE):
        raise ValueError('interception participant must defend as red against blue attackers')
    return value
