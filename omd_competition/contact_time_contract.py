"""Independent contact DTO validation; no policy or engine dependencies."""
import math


def validated_contact(contact, tick, max_age):
    """Return a copy with measurement time, or None for unusable evidence.

    Missing observed_tick can be reconstructed only from an explicit age.
    No clock reset, category inference, or hidden identity lookup is performed.
    """
    if type(tick) is not int or type(max_age) is not int or max_age < 0:
        raise ValueError('invalid contact clock')
    if not contact.get('detected', True) or not contact.get('confirmed', False):
        return None
    observed = contact.get('observed_tick')
    age = contact.get('age_ticks')
    if age is not None and (type(age) is not int or age < 0):
        return None
    if observed is None:
        if age is None:
            return None
        observed = tick - age
    if type(observed) is not int or not 0 <= observed <= tick:
        return None
    effective_age = max(tick - observed, age or 0)
    quality = contact.get('track_quality', contact.get('confidence'))
    if type(quality) not in (int, float) or not math.isfinite(quality) or not 0 < quality <= 1:
        return None
    if effective_age > max_age:
        return None
    expiry = contact.get('expiry_tick')
    if expiry is not None and (type(expiry) is not int or tick > expiry):
        return None
    return {**contact, 'observed_tick': observed, 'age_ticks': effective_age}


def contact_category(contact):
    """Preserve explicit object kinds; air domain alone never implies flight."""
    kind = contact.get('estimated_contact_kind')
    if kind in ('flight', 'platform', 'facility'):
        return kind
    if contact.get('estimated_domain') == 'static_facility':
        return 'facility'
    return 'unknown'
