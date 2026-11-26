"""Archived optical-v1 score decoding plus identity-free receipt filtering.

No automatic identification service remains. New episodes never adjust scores.
The archived score reader allows old replay receipts to remain readable.
"""
from copy import deepcopy

# Compatibility key for archived-v1 receipt readers; never emitted by v2.
DENIAL = 'engagement.authorization_required'


def adjusted_score(native_score, audit, contract):
    """Preserve every native metric receipt and add an explicit points ledger."""
    score = deepcopy(native_score)
    score['engagement_adjustment'] = dict(
        schema_version='engagement-points@1', base_total=native_score['total'],
        violation_penalty_points=contract['violation_penalty_points'],
        third_party_loss_penalty_points=contract['third_party_loss_penalty_points'],
        violated_entity_ids=sorted(audit['violated_entity_ids']),
        attributable_third_party_loss_ids=sorted(audit['attributable_third_party_loss_ids']))
    penalty = (len(audit['violated_entity_ids']) * contract['violation_penalty_points']
               + len(audit['attributable_third_party_loss_ids']) * contract['third_party_loss_penalty_points'])
    score['total'] = max(0., native_score['total'] - penalty / 100.)
    return score


def public_receipt(receipt):
    """Keep useful action status/expenditure, remove native target/RNG evidence."""
    if receipt is None:
        return None
    if isinstance(receipt, list):
        return [public_receipt(x) for x in receipt]
    if not isinstance(receipt, dict):
        return receipt
    allowed = {'queue_receipt', 'execution_receipts', 'child_id', 'status', 'error_code',
               'batch_id', 'tick', 'operation_id', 'accepted', 'action_id', 'command_id'}
    return {k: public_receipt(v) for k, v in receipt.items() if k in allowed}
