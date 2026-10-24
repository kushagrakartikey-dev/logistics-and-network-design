"""Solver selection for PuLP models.

PuLP 2.x bundles CBC; PuLP >= 3/4 may ship without it. Preference order:
HiGHS (``highspy``) -> CBC -> HiGHS command line -> CP-SAT.

Note: OR-Tools wheels embed their own HiGHS symbols. In a process that has
already imported OR-Tools (routing), ``highspy`` becomes unavailable to PuLP,
and importing ``highspy`` first breaks OR-Tools. PuLP then only offers CP-SAT,
which silently rounds continuous variables, so CP-SAT is used only for models
declared pure-integer.
"""
from __future__ import annotations

import pulp


def get_mip_solver(time_limit_s: int = 120, msg: bool = False, pure_integer: bool = False) -> pulp.LpSolver:
    """Return the best available MIP solver instance.

    Args:
        pure_integer: the model has no continuous variables, so CP-SAT is an
            acceptable fallback.
    """
    available = set(pulp.listSolvers(onlyAvailable=True))
    if "HiGHS" in available:
        return pulp.HiGHS(msg=msg, timeLimit=time_limit_s)
    if "PULP_CBC_CMD" in available:
        return pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit_s)
    if "HiGHS_CMD" in available:
        return pulp.HiGHS_CMD(msg=msg, timeLimit=time_limit_s)
    if "CPSAT" in available and pure_integer:
        return pulp.CPSAT(msg=msg, timeLimit=time_limit_s)
    raise RuntimeError(f"No suitable MIP solver for PuLP (pure_integer={pure_integer}). "
                       f"Install highspy or CBC. Found: {sorted(available)}")


def new_var(prob: pulp.LpProblem, name: str, low: float | None = None, up: float | None = None,
            cat: str = "Continuous") -> pulp.LpVariable:
    """Create a decision variable on PuLP 2.x (``LpVariable``) or 4.x (``prob.add_variable``)."""
    if hasattr(prob, "add_variable"):
        return prob.add_variable(name, lowBound=low, upBound=up, cat=cat)
    return pulp.LpVariable(name, lowBound=low, upBound=up, cat=cat)


def solve(prob: pulp.LpProblem, solver: pulp.LpSolver) -> str:
    """Solve and return a normalised status string (``Optimal``, ``Infeasible``, ...)."""
    result = prob.solve(solver)
    status = getattr(result, "status", None)
    if status is not None and hasattr(status, "name"):       # PuLP >= 4: LpSolveStats
        return str(status.name)
    return str(pulp.LpStatus[prob.status])                   # PuLP 2.x/3.x
