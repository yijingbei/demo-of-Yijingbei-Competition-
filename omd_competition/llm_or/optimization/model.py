"""One-round assignment model; costs are estimates for an obstacle-free workspace."""

from dataclasses import asdict, dataclass

import math

class SolutionError(ValueError):
    pass

@dataclass(frozen=True)
class Edge:
    agent_id: str
    task_id: str
    finish_tick: int
    duration_ticks: int
    energy: float
    lateness_ticks: int
    base_cost: int
    switch_cost: int

    @property
    def cost(self):
        return self.base_cost + self.switch_cost

@dataclass
class AssignmentProblem:
    snapshot: object
    plan: object
    edges: dict
    exclusions: list
    idle_costs: dict
    unserved_scale: int
    switch_penalty: int
    travel_model: str = 'euclidean'

    def evaluate(self, assignments):
        """Independent post-solve feasibility and integer-objective reconstruction."""
        aids = {a.agent_id for a in self.snapshot.agents}
        tids = {t.task_id for t in self.snapshot.tasks}
        if type(assignments) is not dict or set(assignments) - aids:
            raise SolutionError('assignment references an unknown agent')
        if any((type(tid) is not str or tid not in tids for tid in assignments.values())):
            raise SolutionError('assignment references an unknown task')
        if len(set(assignments.values())) != len(assignments):
            raise SolutionError('task assigned more than once')
        if any(((aid, tid) not in self.edges for aid, tid in assignments.items())):
            raise SolutionError('ineligible agent-task combination')
        missing = [t.task_id for t in self.snapshot.tasks if t.task_id not in assignments.values()]
        mandatory = {t.task_id for t in self.snapshot.tasks if t.mandatory}
        if mandatory.intersection(missing):
            raise SolutionError('mandatory task left unserved')
        base = sum((self.edges[aid, tid].base_cost for aid, tid in assignments.items()))
        switch = sum((self.edges[aid, tid].switch_cost for aid, tid in assignments.items()))
        switch += sum((cost for aid, cost in self.idle_costs.items() if aid not in assignments))
        weighted_unserved = sum((self.plan.task_priorities[tid] for tid in missing))
        return {'objective': base + switch + self.unserved_scale * weighted_unserved, 'estimated_execution_cost': base, 'switch_cost': switch, 'weighted_unserved': weighted_unserved, 'unassigned_tasks': missing}
