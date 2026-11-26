"""Approved public-window adapter, shared by both arms. No referee imports."""
import copy
import json
import os
from pathlib import Path
from omd_competition.intercept_planner import AirLanePlanner as FrozenPlanner

def normalize_brief(brief):
    result=copy.deepcopy(brief)
    changed=[]
    for i,wave in enumerate(result.get('planned_waves',[])):
        if 'spawn_tick' in wave:continue
        window=wave.get('tick_window')
        if not isinstance(window,list) or len(window)!=2 or not all(isinstance(x,int) for x in window) or window[0]>window[1]:
            raise ValueError('Missing deterministic tick or valid public tick_window')
        wave['spawn_tick']=window[1]
        changed.append({'wave':i,'public_window':window,'executor_release_assumption':window[1]})
    if os.environ.get('OPENMD_LLM_AUDIT'):
        path=Path(os.environ['OPENMD_LLM_AUDIT']).parent/'brief-compat.jsonl'
        with path.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'adapter':'public-window-end@1','normalized_windows':changed,
                                'source':'participant_public_brief_only','not_true_spawn_times':True})+'\n')
    return result

class CompatiblePlanner(FrozenPlanner):
    def __init__(self,brief,mode):super().__init__(normalize_brief(brief),mode)
