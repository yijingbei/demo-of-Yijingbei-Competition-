"""Permit an observed hostile hull blocking a ship's station to be engaged.

The caller retains ordinary sensing freshness, weapon/ammunition/range checks.
Only the ammunition reservation for an unseen facility is relaxed; this helper
does not manufacture contacts, damage, expenditure or terminal credit.
"""
import math


def can_clear_blocked_station(attack, tags, own, contact, domain, facility, station):
    config=attack.get('surface_blockade_response',{})
    avoidance=attack.get('surface_avoidance_m',0.)
    if (not config.get('enabled') or 'boat' not in tags or domain!='surface'
            or facility is not None or avoidance<=0):
        return False
    # These are the same confirmed, organic estimates already accepted by the
    # firing policy. A public task coordinate is never itself fire authority.
    return (math.dist(own['position_m'][:2],station[:2])
            <=config.get('station_approach_radius_m',1200.)
            and math.dist(contact['estimated_position_m'][:2],station[:2])<=2*avoidance)
