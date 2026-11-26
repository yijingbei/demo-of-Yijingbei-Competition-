"""Select an explicitly registered controller, preserving the original controller."""
SUPPORTED_S_IDS = frozenset({'IE-S2-LURE', 'IE-S10-TWO-AXIS'})
SURFACE_JUDGMENT_VARIANTS=frozenset({'buoy_identification_v1','buoy_autonomous_v2'})

def select_policy(row, original_policy):
    variant = row.get('opponent_variant')
    if variant is None:
        return original_policy
    if variant in SURFACE_JUDGMENT_VARIANTS:
        from .buoy_navigation import attack_policy_class
        return attack_policy_class()
    if variant != 's_v16' or row.get('id') not in SUPPORTED_S_IDS:
        raise ValueError('Unregistered opponent variant: '+str(variant))
    from .opponent_s_v16 import AttackPolicy
    return AttackPolicy
