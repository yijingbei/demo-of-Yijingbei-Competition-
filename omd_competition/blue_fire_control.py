"""Blue target reservations using owned launch receipts and measured contacts.

Travel times are estimates, never referee missile or enemy health state.
Opaque contact handles stay local to each observer; team association uses
fresh measured geometry, domains and motion, not decoded target identities.
"""
import math

from .contact_time_contract import validated_contact


class BlueFireControl:
    def __init__(self, config, facilities, facility_domains):
        self.config = config
        self.enabled = config.get('enabled', False)
        self.facilities = facilities
        self.facility_domains = facility_domains
        self.tracks = {}
        self.aliases = {}
        self.reservations = {}
        self.next_track = 0

    def _facility(self, contact):
        return next((key for key, point in self.facilities.items()
                     if math.dist(point, contact['estimated_position_m']) <= 1.), None)

    def _domain(self, contact, facility):
        if facility:
            return self.facility_domains.get(facility, 'land')
        domain = contact.get('estimated_domain')
        return 'land' if domain in {'static_facility', 'shore'} else domain

    def _track(self, entity, contact):
        alias = (entity, contact['contact_id'])
        stamp, position = contact['observed_tick'], tuple(contact['estimated_position_m'])
        facility = self._facility(contact)
        domain = self._domain(contact, facility)
        maximum_speed = self.config['maximum_target_speed_mps'].get(domain, 30.)
        identifier = self.aliases.get(alias)
        if identifier is None:
            choices = []
            for key, track in self.tracks.items():
                age = stamp - track['tick']
                if track['domain'] != domain or track['facility'] != facility:
                    continue
                if facility:
                    choices.append((0., key))
                    continue
                if abs(age) > self.config['association_max_age_ticks']:
                    continue
                # Two concurrent handles from the same observer are distinct
                # objects, even when they pass close to one another.
                if any(e == entity and c != alias[1] and stamp - last <= self.config['association_max_age_ticks']
                       for (e, c), last in track['aliases'].items()):
                    continue
                velocity = track['velocity'] or (0., 0., 0.)
                predicted = [p + v * age * self.config['tick_seconds'] for p, v in zip(track['position'], velocity)]
                distance = math.dist(position, predicted)
                uncertainty = 0. if track['velocity'] is not None else maximum_speed * abs(age) * self.config['tick_seconds']
                if distance <= self.config['association_radius_m'] + uncertainty:
                    choices.append((distance, key))
            if choices:
                identifier = min(choices)[1]
            else:
                identifier = self.next_track
                self.next_track += 1
                self.tracks[identifier] = dict(position=position, tick=stamp, velocity=None,
                                               domain=domain, facility=facility, aliases={})
            self.aliases[alias] = identifier
        track = self.tracks[identifier]
        track['aliases'][alias] = max(stamp, track['aliases'].get(alias, stamp))
        if stamp > track['tick']:
            gap = stamp - track['tick']
            velocity = [(p - q) / (gap * self.config['tick_seconds']) for p, q in zip(position, track['position'])]
            track['velocity'] = (velocity if gap <= self.config['motion_sample_max_gap_ticks']
                                 and math.sqrt(sum(v*v for v in velocity)) <= maximum_speed + 1e-6 else None)
            track.update(position=position, tick=stamp)
            # Replace the conservative unknown-motion estimate once measured
            # motion becomes available. A launcher loss never clears a shot.
            if track['velocity'] is not None:
                for shot in self.reservations.values():
                    if shot['track'] == identifier and shot['unknown_motion']:
                        shot['until'], shot['unknown_motion'] = self._assessment(shot, track)
        return identifier

    def observe(self, entity, contacts, tick):
        if not self.enabled:
            return
        for raw in contacts:
            if raw.get('estimated_contact_kind') == 'flight':
                continue
            contact = validated_contact(raw, tick, self.config['association_max_age_ticks'])
            if contact is not None:
                self._track(entity, contact)

    def _assessment(self, shot, track):
        profile = self.config['weapon_profiles'][shot['weapon']]
        if profile['delivery_model'] != 'guided_missile':
            return shot['tick'] + profile.get('impact_delay_ticks', 1) + self.config['assessment_ticks'], False
        offset = [p-q for p, q in zip(shot['target_position'], shot['launch_position'])]
        distance = math.sqrt(sum(v*v for v in offset))
        velocity = track['velocity']
        unknown = velocity is None and not track['facility']
        radial = (self.config['maximum_target_speed_mps'].get(track['domain'], 30.) if unknown else
                  sum(v*d for v, d in zip(velocity or (0., 0., 0.), offset)) / max(distance, 1.))
        closing = profile['cruise_speed_mps'] - max(0., radial)
        flight = (profile['max_flight_ticks'] if closing <= 1. else
                  min(profile['max_flight_ticks'], math.ceil(self.config['flight_time_margin'] * distance /
                      (closing * self.config['tick_seconds']))))
        return shot['tick'] + max(1, flight) + self.config['assessment_ticks'], unknown

    def available(self, entity, contact, tick, detail):
        if not self.enabled:
            return True
        identifier = self._track(entity, contact)
        holds = []
        for request, shot in list(self.reservations.items()):
            if shot['track'] != identifier:
                continue
            # Rejection releases immediately. An executed shot is only
            # reassessed after its flight window and a NEW measurement.
            if shot['status'] == 'pending' or tick < shot['until'] or contact['observed_tick'] <= shot['until']:
                holds.append(dict(contact_id=contact['contact_id'], request_id=request,
                                  reason='launch_pending' if shot['status'] == 'pending' else
                                  'estimated_attack_in_flight' if tick < shot['until'] else 'awaiting_new_measurement',
                                  assessment_tick=shot['until']))
            else:
                del self.reservations[request]
        if holds:
            detail.setdefault('fire_control_holds', []).extend(holds)
        return not holds

    def reserve(self, request, entity, own, contact, weapon, tick):
        if not self.enabled:
            return
        identifier = self._track(entity, contact)
        shot = dict(track=identifier, tick=tick, weapon=weapon, status='pending',
                    launch_position=tuple(own['position_m']), target_position=tuple(contact['estimated_position_m']))
        shot['until'], shot['unknown_motion'] = self._assessment(shot, self.tracks[identifier])
        self.reservations[request] = shot

    def confirm(self, request, status):
        if request not in self.reservations:
            return
        if status == 'executed':
            self.reservations[request]['status'] = 'executed'
        else:
            del self.reservations[request]
