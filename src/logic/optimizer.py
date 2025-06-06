import random
import pandas as pd
import pulp as pl
from collections import Counter
from typing import Dict, List, Any, Tuple

from src.utils.helpers import Settings, Props
from src.logic.data_loader import DataLoader
from src.utils.cache_utils import get_or_compute_achievable


class CoefficientCalculator:
    """
    Вычисляет "сырые" коэффициенты по заданным правилам (props.yaml).
    Используем нормализацию через achievable_max.
    """

    def __init__(self, props: Props, df: pd.DataFrame) -> None:
        self.props = props.data
        self.df = df
        self.N = len(df)
        self.coef: Dict[str, List[float]] = {}

    @staticmethod
    def _value_from_row(row: pd.Series, meta: Dict[str, Any]) -> float:
        if "column" in meta:
            return meta.get("sign", 1) * row[meta["column"]]
        return meta.get("sign", 1) * (row[meta["col_out"]] - row[meta["col_in"]])

    def compute(self) -> None:
        for prop_name, meta in self.props.items():
            if "expr" in meta:
                self.coef[prop_name] = [float(meta["expr"])] * self.N
            elif "column" in meta:
                self.coef[prop_name] = [
                    self._value_from_row(r, meta) for _, r in self.df.iterrows()
                ]
            else:
                self.coef[prop_name] = [
                    sum(self._value_from_row(r, m) for m in meta["group"])
                    for _, r in self.df.iterrows()
                ]


class ILPSolver:
    """
    Решает MILP с объективом на основе нормализации через achievable_max.
    Ограничения:
        • low_p  ≤ Σ coef_p_i * x_i ≤ high_p
        • 0 ≤ x_i ≤ max_copy
        • Σ x_i == num_slots
    """

    def __init__(self,
                 df: pd.DataFrame,
                 coef: Dict[str, List[float]],
                 props: Dict[str, Any],
                 settings: Settings,
                 fixed_artifacts: list[tuple[str, int]] | None = None
                 ) -> None:
        self.df = df
        self.coef = coef
        self.props = props
        self.set = settings
        self.N = len(df)
        self.fixed_artifacts = fixed_artifacts or []
        self.fixed_counts = Counter(self.fixed_artifacts)

    def _get_achievable_max(self, prop_name: str) -> float:
        if not hasattr(self, '_achievable_max_cache'):
            self._achievable_max_cache: Dict[str, float] = {}

        if prop_name not in self._achievable_max_cache:
            prob = pl.LpProblem(f"Max_{prop_name}", pl.LpMaximize)
            x_vars = {i: pl.LpVariable(f"x_{prop_name}_{i}", 0, self.set.max_copy, pl.LpInteger)
                      for i in range(self.N)}

            for p, meta in self.props.items():
                expr = pl.lpSum(self.coef[p][i] * x_vars[i] for i in range(self.N))
                if (low := meta.get('low')) is not None:
                    prob += expr >= low
                if (high := meta.get('high')) not in (None, 0):
                    prob += expr <= high

            prob += pl.lpSum(x_vars.values()) == self.set.num_slots
            prob += pl.lpSum(self.coef[prop_name][i] * x_vars[i] for i in range(self.N))
            prob.solve(pl.PULP_CBC_CMD(msg=False, timeLimit=5))
            max_val = (sum(self.coef[prop_name][i] * x_vars[i].value() for i in range(self.N))
                       if pl.LpStatus[prob.status] == 'Optimal' else 0.0)

            self._achievable_max_cache[prop_name] = max_val

        return self._achievable_max_cache[prop_name]

    def _get_achievable_min(self, prop_name: str) -> float:
        if not hasattr(self, '_achievable_min_cache'):
            self._achievable_min_cache: Dict[str, float] = {}

        if prop_name not in self._achievable_min_cache:
            prob = pl.LpProblem(f"Min_{prop_name}", pl.LpMinimize)
            x_vars = {i: pl.LpVariable(f"x_{prop_name}_{i}", 0, self.set.max_copy, pl.LpInteger)
                      for i in range(self.N)}

            for p, meta in self.props.items():
                expr = pl.lpSum(self.coef[p][i] * x_vars[i] for i in range(self.N))
                if (low := meta.get('low')) is not None:
                    prob += expr >= low
                if (high := meta.get('high')) not in (None, 0):
                    prob += expr <= high

            prob += pl.lpSum(x_vars.values()) == self.set.num_slots
            prob += pl.lpSum(self.coef[prop_name][i] * x_vars[i] for i in range(self.N))
            prob.solve(pl.PULP_CBC_CMD(msg=False, timeLimit=5))
            min_val = (sum(self.coef[prop_name][i] * x_vars[i].value() for i in range(self.N))
                       if pl.LpStatus[prob.status] == 'Optimal' else 0.0)

            self._achievable_min_cache[prop_name] = min_val

        return self._achievable_min_cache[prop_name]

    def _compute_all_achievable(self) -> Tuple[Dict[str, Any], Dict[str, float]]:
        maxima = {p: self._get_achievable_max(p) for p in self.props}
        return self.props, maxima

    def solve_balanced(self,
                       jitter: float = 0.0,
                       cuts: List[List[int]] | None = None,
                       ) -> Tuple[List[Tuple[str, int, int]], Dict[str, float], float]:

        self._achievable_max_cache = get_or_compute_achievable(self.set,
                                                               self.props,
                                                               lambda settings: self._compute_all_achievable()
                                                               )

        prob = pl.LpProblem("ArtifactBalanced", pl.LpMaximize)
        x = {i: pl.LpVariable(f"x{i}", 0, self.set.max_copy, pl.LpInteger)
             for i in range(self.N)}

        for i, row in self.df.iterrows():
            cnt_fixed = self.fixed_counts.get((row['Имя'], row['Тир']), 0)
            if cnt_fixed > 0:
                prob += x[i] == cnt_fixed

        for p, meta in self.props.items():
            if not meta.get("use", False):
                continue
            expr = pl.lpSum(self.coef[p][i] * x[i] for i in range(self.N))
            if (low := meta.get('low')) is not None:
                prob += expr >= low
            if (high := meta.get('high')) not in (None, 0):
                prob += expr <= high

        prob += pl.lpSum(x.values()) == self.set.num_slots

        if cuts:
            for cut in cuts:
                prob += pl.lpSum(x[i] for i in cut) <= self.set.num_slots - 1

        terms: List[Any] = []
        for p, meta in self.props.items():
            if not meta.get('use', False):
                continue
            prio = meta.get('priority', 0)
            if prio <= 0:
                continue
            raw_expr = pl.lpSum(self.coef[p][i] * x[i] for i in range(self.N))
            achievable = self._achievable_max_cache.get(p, 1.0)
            low_raw = meta.get('low')
            high_raw = meta.get('high')
            high_eff = min(high_raw, achievable) if high_raw not in (None, 0) else achievable
            low_eff = low_raw if low_raw is not None else 0.0
            span = high_eff - low_eff

            if span <= 0:
                span = 1.0

            val_norm = (raw_expr - low_eff) / span
            low_norm = 0.0
            high_norm = 1.0

            if low_raw is not None and high_raw not in (None, 0):
                target = (low_norm + high_norm) / 2
            elif low_raw is not None:
                target = (low_norm + 1.0) / 2
            elif high_raw not in (None, 0):
                target = high_norm / 2
            else:
                terms.append(prio * val_norm)
                continue
            delta = pl.LpVariable(f"delta_{p}", 0)
            prob += target - val_norm <= delta
            lam = prio * 0.5
            terms.append(prio * val_norm - lam * delta)

        prob += pl.lpSum(terms)
        prob.solve(pl.PULP_CBC_CMD(msg=False))

        if pl.LpStatus[prob.status] != 'Optimal':
            return [], {}, 0.0

        build = [
            (self.df.loc[i, 'Имя'], self.df.loc[i, 'Тир'], int(x[i].value()))
            for i in range(self.N) if x[i].value() > 0
        ]
        stats = {p: sum(self.coef[p][i] * x[i].value() for i in range(self.N)) for p in self.props}
        score = float(pl.value(prob.objective) or 0.0)

        return build, stats, score

    def solve_once(self,
                   jitter: float = 0.0,
                   cuts: List[List[int]] | None = None,
                   ) -> Tuple[List[Tuple[str, int, int]], Dict[str, float], float]:

        self._achievable_max_cache = get_or_compute_achievable(
            self.set,
            self.props,
            lambda settings: self._compute_all_achievable()
        )

        if not hasattr(self, '_base_model'):
            self._base_model = pl.LpProblem("ArtifactOptim", pl.LpMaximize)
            self._x = {i: pl.LpVariable(f"x{i}", 0, self.set.max_copy, pl.LpInteger) for i in range(self.N)}

            for i, row in self.df.iterrows():
                cnt_fixed = self.fixed_counts.get((row["Имя"], row["Тир"]), 0)
                if cnt_fixed > 0:
                    self._base_model += self._x[i] == cnt_fixed

            for p, meta in self.props.items():
                if not meta.get('use', False):
                    continue
                expr = pl.lpSum(self.coef[p][i] * self._x[i] for i in range(self.N))
                if (low := meta.get('low')) is not None:
                    self._base_model += expr >= low
                if (high := meta.get('high')) not in (None, 0):
                    self._base_model += expr <= high
            self._base_model += pl.lpSum(self._x.values()) == self.set.num_slots

        model = self._base_model.copy()

        if cuts:
            for cut in cuts:
                model += pl.lpSum(self._x[i] for i in cut) <= self.set.num_slots - 1
        terms = []

        for p, meta in self.props.items():
            if not meta.get("use", False):
                continue
            prio = meta.get("priority", 0)
            if prio <= 0:
                continue

            achievable = self._achievable_max_cache.get(p, 1.0)
            high_raw = meta.get("high")
            high_eff = min(high_raw, achievable) if high_raw not in (None, 0) else achievable
            low_raw = meta.get("low", 0.0)
            span = high_eff - low_raw

            if span <= 0:
                span = 1.0

            raw_expr = pl.lpSum(self.coef[p][i] * self._x[i] for i in range(self.N))
            norm_expr = (raw_expr - low_raw) / span

            weight = prio * (1 + jitter * random.uniform(-1, 1))
            terms.append(weight * norm_expr)

        model += pl.lpSum(terms)
        model.solve(pl.PULP_CBC_CMD(msg=False, timeLimit=1, gapRel=0.02))

        if pl.LpStatus[model.status] != 'Optimal':
            return [], {}, 0.0

        build = [
            (self.df.loc[i, 'Имя'], self.df.loc[i, 'Тир'], int(self._x[i].value()))
            for i in range(self.N)
            if self._x[i].value() > 0
        ]
        stats = {p: sum(self.coef[p][i] * self._x[i].value() for i in range(self.N)) for p in self.props}
        score = float(pl.value(model.objective) or 0.0)

        return build, stats, score


class ArtifactBuildManager:
    """
    Управляет подбором сборок: детермин. решение + альтернативы + отчёт.
    """

    def __init__(self, props: Props, settings: Settings, fixed_artifacts: List[Tuple[str, int]]):
        self.settings = settings
        self.props = props.data
        full_df = DataLoader(self.settings).load()
        base_df = full_df[
            (full_df["Тир"] == settings.tier) &
            (~full_df["Имя"].isin(settings.blacklist))
            ]

        fixed_rows: List[pd.DataFrame] = []
        for name, tier in fixed_artifacts:
            rows = full_df[(full_df["Имя"] == name) & (full_df["Тир"] == tier)]
            if not rows.empty:
                fixed_rows.append(rows)

        if fixed_rows:
            solver_df = pd.concat([base_df, *fixed_rows], ignore_index=True)
        else:
            solver_df = base_df.copy()

        solver_df = (
            solver_df
            .drop_duplicates(subset=["Имя", "Тир"])
            .sort_values(by=["Тир", "Имя"])
            .reset_index(drop=True)
        )
        self.df = solver_df

        calc = CoefficientCalculator(props, self.df)
        calc.compute()

        self.solver = ILPSolver(
            self.df,
            calc.coef,
            self.props,
            self.settings,
            fixed_artifacts
        )

        self.best: Dict[str, Any] = {}
        self.alts: List[Dict[str, Any]] = []
        self.fixed_artifacts = fixed_artifacts

    def run(self) -> None:
        best_list, stats, score = self.solver.solve_balanced()
        self.best = {"build": best_list, "stats": stats, "score": score}

        best_counts = {(n, t): c for n, t, c in best_list}
        det_cut = [
            idx for idx, (_, row) in enumerate(self.df.iterrows())
            if best_counts.get((row["Имя"], row["Тир"]), 0) > 0
        ]
        cuts: List[List[int]] = [det_cut]

        results: List[Dict[str, Any]] = []
        for _ in range(self.settings.alt_runs):
            alt_list, alt_stats, alt_score = self.solver.solve_once(
                jitter=self.settings.alt_jitter,
                cuts=cuts
            )
            if not alt_list:
                continue

            results.append({
                "run": len(results) + 1,
                "build": alt_list,
                "score": alt_score,
                **alt_stats
            })

            alt_counts = {(n, t): c for n, t, c in alt_list}
            cuts.append([
                idx for idx, (_, row) in enumerate(self.df.iterrows())
                if alt_counts.get((row["Имя"], row["Тир"]), 0) > 0
            ])

            if len(results) >= self.settings.alt_cnt:
                break

        self.alts = results


def compute_builds(_props: Props,
                   _settings: Settings,
                   fixed_artifacts: list[tuple[str, int]]):
    mgr = ArtifactBuildManager(_props, _settings, fixed_artifacts)
    mgr.run()
    return mgr.best, mgr.alts
