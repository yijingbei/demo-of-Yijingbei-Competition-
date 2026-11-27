"""Bounded exact search and polynomial matching; no native solver dependency."""

from dataclasses import asdict, dataclass
from enum import Enum
import math
import time
from .model import SolutionError

class ModelInvalid(SolutionError):
    pass

class Status(str, Enum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"

@dataclass
class Result:
    status: Status
    backend: str
    assignments: dict
    objective: int | None
    best_bound: float | None
    elapsed_seconds: float
    stats: dict

    def as_dict(self):
        return asdict(self) | {"status": self.status.value}

def validate_result(problem, result):
    if not isinstance(result.status, Status):
        raise SolutionError("invalid solver status")
    if type(result.elapsed_seconds) not in (int, float) or not math.isfinite(result.elapsed_seconds) or result.elapsed_seconds < 0:
        raise SolutionError("invalid solver duration")
    if result.status in (Status.OPTIMAL, Status.FEASIBLE):
        if type(result.objective) is not int:
            raise SolutionError("integer objective required")
        measured = problem.evaluate(result.assignments)
        if measured["objective"] != result.objective:
            raise SolutionError("solver objective differs from independently reconstructed cost")
        if result.best_bound is not None and (not math.isfinite(result.best_bound) or result.best_bound > result.objective + 1e-5):
            raise SolutionError("invalid minimization bound")
        return measured
    if result.assignments or result.objective is not None:
        raise SolutionError("solver without a feasible solution returned actions")
    return None

class ExactSolver:
    """Finite enumeration for up to 8 agents/16 tasks, with explicit resource limits."""
    name = "bounded-exact"

    def __init__(self, time_limit=2., node_limit=200000):
        if type(time_limit) not in (int, float) or not math.isfinite(time_limit) or time_limit <= 0:
            raise ValueError("time limit must be positive and finite")
        if type(node_limit) is not int or node_limit < 1:
            raise ValueError("node limit must be a positive integer")
        self.time_limit, self.node_limit = time_limit, node_limit

    def solve(self, problem):
        if len(problem.snapshot.agents) > 8 or len(problem.snapshot.tasks) > 16:
            raise ValueError("exact verification solver limited to 8 agents/16 tasks; use CP-SAT")
        started = time.monotonic()
        best, best_value, nodes, stopped = None, None, 0, False
        try:
            best_value, best = problem.evaluate({})["objective"], {}
        except SolutionError:
            pass
        agents = problem.snapshot.agents
        options = {a.agent_id: sorted([e for e in problem.edges.values() if e.agent_id == a.agent_id],
                    key=lambda e: (-problem.plan.task_priorities[e.task_id], e.cost, e.task_id)) for a in agents}

        def visit(index, selected, used, secondary):
            nonlocal best, best_value, nodes, stopped
            if stopped:
                return
            if nodes >= self.node_limit or time.monotonic() - started >= self.time_limit:
                stopped = True
                return
            nodes += 1
            if best_value is not None and secondary > best_value:
                return
            if index == len(agents):
                try:
                    value = problem.evaluate(selected)["objective"]
                except SolutionError:
                    return
                if best_value is None or value < best_value:
                    best_value, best = value, dict(selected)
                return
            aid = agents[index].agent_id
            for edge in options[aid]:
                if edge.task_id in used:
                    continue
                selected[aid] = edge.task_id
                used.add(edge.task_id)
                visit(index + 1, selected, used, secondary + edge.cost)
                used.remove(edge.task_id)
                del selected[aid]
            visit(index + 1, selected, used, secondary + problem.idle_costs[aid])

        visit(0, {}, set(), 0)
        status = (Status.FEASIBLE if stopped else Status.OPTIMAL) if best is not None else (Status.UNKNOWN if stopped else Status.INFEASIBLE)
        bound = (float(best_value) if not stopped else 0.) if best is not None else None
        result = Result(status, self.name, best or {}, best_value, bound,
                        time.monotonic() - started, {"nodes": nodes, "budget_exhausted": stopped})
        validate_result(problem, result)
        return result

class MatchingSolver:
    """Exact rectangular assignment in polynomial time, using integer costs.

    Each agent has one row; task columns and one idle column per agent allow
    partial matching. Unserved-task penalties are subtracted from eligible
    edges. A dominating penalty enforces mandatory coverage; feasibility is
    then checked by AssignmentProblem rather than trusting the transformed
    objective. No native dependency, enumeration cap or heuristic action is
    required for this one-round bipartite model.
    """
    name = "min-cost-matching"

    def solve(self, problem):
        started = time.monotonic()
        agents, tasks = problem.snapshot.agents, problem.snapshot.tasks
        n, m = len(agents), len(tasks)
        optional = sum(problem.unserved_scale * problem.plan.task_priorities[t.task_id]
                       for t in tasks if not t.mandatory)
        row_bounds = [max([abs(problem.idle_costs[a.agent_id]),
                           *[abs(e.cost) for e in problem.edges.values() if e.agent_id == a.agent_id]])
                      for a in agents]
        mandatory_penalty = optional + 2 * sum(row_bounds) + 1
        penalties = {t.task_id: mandatory_penalty if t.mandatory else
                     problem.unserved_scale * problem.plan.task_priorities[t.task_id] for t in tasks}
        costs = []
        for agent in agents:
            aid = agent.agent_id
            costs.append([problem.edges[aid, t.task_id].cost - penalties[t.task_id]
                          if (aid, t.task_id) in problem.edges else None for t in tasks]
                         + [problem.idle_costs[aid]] * n)
        # There are n idle columns, so a finite complete row matching always
        # exists. This bound makes any forbidden edge strictly more expensive
        # than every finite matching, even when permitted costs are negative.
        absolute_bound = sum(max((abs(v) for v in row if v is not None), default=0) for row in costs)
        forbidden = 2 * absolute_bound + 1
        costs = [[forbidden if v is None else v for v in row] for row in costs]
        columns = m + n
        u, v = [0] * (n + 1), [0] * (columns + 1)
        owner, previous = [0] * (columns + 1), [0] * (columns + 1)
        # Shortest augmenting paths with dual potentials (rectangular Hungarian).
        for row in range(1, n + 1):
            owner[0], column = row, 0
            distance, visited = [None] * (columns + 1), [False] * (columns + 1)
            while True:
                visited[column] = True
                current, delta, next_column = owner[column], None, 0
                for j in range(1, columns + 1):
                    if visited[j]:
                        continue
                    reduced = costs[current - 1][j - 1] - u[current] - v[j]
                    if distance[j] is None or reduced < distance[j]:
                        distance[j], previous[j] = reduced, column
                    if delta is None or distance[j] < delta:
                        delta, next_column = distance[j], j
                for j in range(columns + 1):
                    if visited[j]:
                        u[owner[j]] += delta
                        v[j] -= delta
                    elif distance[j] is not None:
                        distance[j] -= delta
                column = next_column
                if owner[column] == 0:
                    break
            while column:
                before = previous[column]
                owner[column] = owner[before]
                column = before
        assignments = {agents[owner[j] - 1].agent_id: tasks[j - 1].task_id
                       for j in range(1, m + 1) if owner[j]}
        if any(key not in problem.edges for key in assignments.items()):
            raise ModelInvalid("matching selected a forbidden edge")
        missing = {t.task_id for t in tasks if t.mandatory} - set(assignments.values())
        status = Status.INFEASIBLE if missing else Status.OPTIMAL
        value = None if missing else problem.evaluate(assignments)["objective"]
        result = Result(status, self.name, {} if missing else assignments, value,
                        None if missing else float(value), time.monotonic() - started,
                        {"agents": n, "tasks": m, "columns": columns, "augmentations": n})
        validate_result(problem, result)
        return result

class SizeAwareSolver(ExactSolver):
    """Preserve the original small-case search; match larger bipartite cases."""

    def solve(self, problem):
        if len(problem.snapshot.agents) > 8 or len(problem.snapshot.tasks) > 16:
            return MatchingSolver().solve(problem)
        return super().solve(problem)
