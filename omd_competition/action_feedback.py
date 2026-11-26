"""Controller-scoped delayed receipts and public-only reference retry control."""
from copy import deepcopy
from .surface_identification import public_receipt


class ActionReceiptRouter:
    def __init__(self):
        self.owners = {}

    def submitted(self, controller_slot, batch):
        for field, identity in (('persistent_commands', 'command_id'), ('discrete_actions', 'action_id')):
            for child in (batch or {}).get(field, ()):
                identifier = child[identity]
                owner = self.owners.get(identifier)
                if owner is not None and owner[0] != controller_slot:
                    raise ValueError('submitted child belongs to another controller')
                self.owners[identifier] = (controller_slot, field)

    def route(self, receipts):
        routed = {}
        for child in receipts:
            identifier = child['child_id']
            owner = self.owners.get(identifier)
            if owner is None:
                continue
            routed.setdefault(owner[0], []).append(public_receipt(child))
            status = child['status']
            if (owner[1] == 'discrete_actions' and status not in {'queued', 'accepted'}
                    or status in {'rejected', 'expired', 'superseded', 'cancelled', 'dropped'}):
                self.owners.pop(identifier)
        return routed


def validate_retry_policy(config):
    keys = {'maximum_retries', 'initial_backoff_ticks', 'maximum_backoff_ticks',
            'exhausted_backoff_ticks', 'receipt_timeout_ticks'}
    if (set(config) != keys or any(type(v) is not int or v < 0 for v in config.values())
            or any(config[k] == 0 for k in keys - {'maximum_retries'})):
        raise ValueError('invalid public fire feedback policy')
    if not config['initial_backoff_ticks'] <= config['maximum_backoff_ticks'] <= config['exhausted_backoff_ticks']:
        raise ValueError('fire feedback backoff intervals are inconsistent')


class FireFeedback:
    """Bound retries by known action IDs; rejection never classifies a contact."""
    def __init__(self, config=None):
        self.config = dict(config or {})
        if self.config:
            validate_retry_policy(self.config)
        self.pending = {}
        self.failures = {}

    def _rejected(self, key, tick):
        previous = self.failures.get(key, {})
        count = previous.get('count', 0) + 1
        p = self.config
        exhausted = count > p['maximum_retries']
        delay = (p['exhausted_backoff_ticks'] if exhausted else
                 min(p['maximum_backoff_ticks'], p['initial_backoff_ticks'] * 2 ** (count - 1)))
        self.failures[key] = dict(count=count, until=tick + delay, evidence_after=tick, exhausted=exhausted)

    def observe_receipt(self, receipt):
        if not self.config:
            return
        for child in public_receipt(receipt or {}).get('execution_receipts', ()):
            identifier = child.get('child_id')
            request = self.pending.get(identifier)
            if request is None or child.get('status') in {'queued', 'accepted'}:
                continue
            self.pending.pop(identifier)
            if child.get('status') == 'executed':
                self.failures.pop(request['key'], None)
            else:
                self._rejected(request['key'], child.get('tick', request['tick']))

    def apply(self, obs, batch):
        if not self.config:
            return batch
        tick = obs['tick']
        for identifier, request in list(self.pending.items()):
            if tick - request['tick'] > self.config['receipt_timeout_ticks']:
                self.pending.pop(identifier)
                self._rejected(request['key'], tick)
        if not batch:
            return batch
        result = deepcopy(batch)
        reports = {c['contact_id']: c for name in ('organic_contacts', 'shared_contacts') for c in obs.get(name, ())}
        retained = []
        for action in result.get('discrete_actions', ()):
            if action['action_type'] != 'fire_weapon':
                retained.append(action)
                continue
            key = (action['entity_id'], action['payload']['contact_id'])
            if any(p['key'] == key for p in self.pending.values()):
                continue
            failure = self.failures.get(key)
            if failure:
                if tick < failure['until']:
                    continue
                if failure['exhausted']:
                    if reports.get(key[1], {}).get('observed_tick', -1) <= failure['evidence_after']:
                        continue
                    self.failures.pop(key)
            retained.append(action)
            self.pending[action['action_id']] = dict(key=key, tick=tick)
        result['discrete_actions'] = retained
        return result
