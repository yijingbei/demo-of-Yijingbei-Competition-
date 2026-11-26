"""Public JSONL trajectory reference. Never reads a scenario or referee file."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from omd_competition.agent import PROTOCOL
from omd_competition.buoy_baselines import contact_policy


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('idle','naive','coordinated','greedy','adaptive'),default='adaptive')
    parser.add_argument('--waste-one-round',action='store_true')
    args=parser.parse_args();policy=None
    for line in sys.stdin:
        request=json.loads(line)
        if request['protocol']!=PROTOCOL:raise ValueError('unsupported protocol')
        if request['type']=='close':return
        if request['type']=='reset':
            policy=contact_policy(request['public_brief'],args.mode)
            policy.waste_one_round=args.waste_one_round
        if policy is None:raise ValueError('reset required')
        policy.observe_receipt(request.get('last_receipt'))
        print(json.dumps(dict(protocol=PROTOCOL,batch=policy.act(request['observation'])),allow_nan=False),flush=True)


if __name__=='__main__':main()
