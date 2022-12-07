"""Selective replication with model parallelism."""
from collections import namedtuple
from functools import partial
import logging
import multiprocessing
import time
from typing import List

import numpy as np
import ray

from alpa_serve.profiling import ParallelConfig
from alpa_serve.placement_policy.base_policy import (
    BasePlacementPolicy, ModelData, ClusterEnv, ModelPlacement,
    PlacementEvaluator, gen_train_workload,
    replica_placement_fast_greedy, replica_placement_beam_search,
    replica_placement_on_last_group, evolutionary_search)
from alpa_serve.simulator.controller import simulate_one_case
from alpa_serve.simulator.executable import Executable
from alpa_serve.simulator.workload import Workload, GammaProcess
from alpa_serve.trace import Trace
from alpa_serve.util import get_factors, ServingCase, eps


def compute_capability(model_data, parallel_config, max_bs):
    slo = model_data.slo
    latency_mem = model_data.profiling_result.para_dict.get(parallel_config, None)

    if latency_mem is None:
        return 0

    num_stages = parallel_config.pp
    max_cap = 0
    for b, ls in latency_mem.latency.items():
        if b > max_bs:
            continue

        # slo = sum(ls) + (n-1) * max(ls)
        # so, n = ceil((slo - sum(ls)) / max(ls)) + 1
        max_cap = max(max_cap, (slo - sum(ls)) // max(ls) + 1)

    return max_cap * (0.99 ** num_stages)


class ModelParallelismILP(BasePlacementPolicy):
    def __init__(self, verbose: int = 0):
        super().__init__(verbose=verbose)

        self.time_limit = 30
        self.sum_k = 1e-4
        self.max_bs = 1

        # Hard coded for now. Expose this as parameters later
        self.group_configs = [
            ParallelConfig(0, 0, 0),
            ParallelConfig(1, 1, 1),
            ParallelConfig(1, 1, 2),
            ParallelConfig(1, 1, 4),
            ParallelConfig(1, 1, 8),
        ]
        self.group_sizes = [
            np.prod(x) for x in self.group_configs
        ]

    def compute_max_stage_mem(self, model_data, parallel_config, mem_budget):
        latency_mem = model_data.profiling_result.para_dict.get(parallel_config, None)

        if latency_mem is None:
            return mem_budget * 2

        return max(latency_mem.weight_mem)

    def solve_placement(self,
                        model_datas: List[ModelData],
                        cluster_env: ClusterEnv,
                        train_workload: Workload = None):
        import pulp
        from pulp import LpVariable, LpProblem, LpMaximize, lpSum, LpStatus

        tic = time.time()

        # Load constants
        N = len(model_datas)
        M = cluster_env.num_devices
        C = cluster_env.mem_budget
        a = [x.rate for x in model_datas]
        c = [x.profiling_result.para_dict[ParallelConfig(1, 1, 1)].weight_mem[0]
             for x in model_datas]

        G = cluster_env.num_devices
        K = len(self.group_configs)
        g = self.group_sizes
        f = np.zeros((N, K))
        d = np.zeros((N, K))
        for i in range(N):
            model_data = model_datas[i]
            for k in range(K):
                parallel_config = self.group_configs[k]
                f[i][k] = compute_capability(model_data, parallel_config, self.max_bs)
                d[i][k] = self.compute_max_stage_mem(
                    model_data, parallel_config, cluster_env.mem_budget)

        # 1. Create variables
        p = LpVariable.matrix("p", (range(N), range(G)), cat="Binary")
        cap = [None] * N
        min_tolerance = LpVariable("min_tolerance", lowBound=0)
        sum_tolerance = LpVariable("sum_tolerance", lowBound=0)
        s = LpVariable.matrix("s", (range(G), range(K)), cat="Binary")
        pxs = LpVariable.matrix("pxs", (range(N), range(G), range(K)), cat="Binary")

        # 2. Objective
        prob = LpProblem("myProblem", LpMaximize)
        obj = min_tolerance + self.sum_k * sum_tolerance
        prob += obj

        # 3. Constraints
        # (a). memory budget on each GPU
        for j in range(G):
            prob += (lpSum(p[i][j] * (c[i] / C) for i in range(N)) <=
                     lpSum(s[j][k] * g[k] for k in range(K)))

        ## A more precise version, not used right now
        #for j in range(G):
        #    prob += (lpSum(pxs[i][j][k] * (d[i][k] / C)
        #                   for i in range(N) for k in range(K)) <= 1)

        # (b). capability
        for i in range(N):
            cap[i] = lpSum(pxs[i][j][k] * f[i][k]
                           for j in range(G) for k in range(K))

        # (c). min tolerance and sum tolerance
        for i in range(N):
            prob += min_tolerance <= cap[i] / a[i]

        prob += sum_tolerance == lpSum(cap[i] / a[i] for i in range(N))

        # (d). group size
        prob += lpSum(s[j][k] * g[k] for j in range(G) for k in range(K)) == M

        # (e). only one configuration
        for j in range(G):
            prob += lpSum(s[j][k] for k in range(K)) == 1

        # (f). linearization
        for i in range(N):
            for j in range(G):
                for k in range(K):
                    prob += pxs[i][j][k] <= p[i][j]
                    prob += pxs[i][j][k] <= s[j][k]
                    prob += pxs[i][j][k] >= p[i][j] + s[j][k] - 1

        assert "PULP_CBC_CMD" in pulp.listSolvers(onlyAvailable=True), (
            "Please install ILP solvers by 'sudo apt install coinor-cbc'")

        solver = pulp.PULP_CBC_CMD(mip=True,
                                   msg=False,
                                   timeLimit=self.time_limit,
                                   threads=multiprocessing.cpu_count())
        prob.solve(solver)

        status = prob.status
        objective = pulp.value(prob.objective)
        objective = float(objective) if objective is not None else -1.0
        if self.verbose >= 2:
            print(f"ILP Status: {LpStatus[status]}\tObjective: {objective}\t"
                  f"Time: {time.time() - tic}")

        if prob.status in [pulp.LpStatusInfeasible]:
            raise RuntimeError(
                "Cannot run the function under the given memory budget. "
                "Please increase the memory budget.")

        # Group configuration selection
        s_res = []
        for j in range(G):
            assert sum(pulp.value(s[j][k]) for k in range(K)) == 1
            for k in range(K):
                if pulp.value(s[j][k]):
                    s_res.append(k)

        # Placement
        p_res = np.zeros((N, G), dtype=np.int8)
        for i in range(N):
            for j in range(G):
                if pulp.value(p[i][j]):
                    p_res[i][j] = 1

        group_configs = []
        group_models = []
        for j in range(G):
            config_id = s_res[j]
            if self.group_sizes[config_id]:
                tmp = []
                for i in range(N):
                    if p_res[i][j]:
                        tmp.append(i)
                group_configs.append(self.group_configs[config_id])
                group_models.append(tmp)

        return ModelPlacement(group_configs, group_models), {"objective": objective}


class ModelParallelismGreedy(BasePlacementPolicy):

    def __init__(self, group_size: int = 2,
                 add_evo_search: bool = False,
                 verbose: int = 0):
        super().__init__(verbose=verbose)

        self.group_size = group_size
        self.add_evo_search = add_evo_search

    def solve_placement(self,
                        model_datas: List[ModelData],
                        cluster_env: ClusterEnv,
                        train_workload: Workload = None):
        # Generate workloads
        if train_workload is None:
            train_workload = gen_train_workload(model_datas)

        # Run greedy placement
        evaluator = PlacementEvaluator(model_datas, cluster_env, train_workload,
                                       "fast_simulator", False)

        assert cluster_env.num_devices % self.group_size == 0
        num_groups = cluster_env.num_devices // self.group_size
        sol = ModelPlacement([ParallelConfig(1,1,self.group_size)] * num_groups,
                             [[] for _ in range(num_groups)])
        sol = replica_placement_fast_greedy(
            sol, model_datas, cluster_env, train_workload,
            evaluator, self.verbose)

        if self.add_evo_search:
            sol = evolutionary_search([sol], model_datas, cluster_env,
                                      evaluator, 200, self.verbose)
        return sol, None


class ModelParallelismSearch(BasePlacementPolicy):

    def __init__(self,
                 max_bs: int = 1,
                 max_pp: int = 8,
                 max_op: int = 4,
                 add_evo_search: bool = False,
                 verbose: int = 0):
        super().__init__(verbose=verbose)

        self.max_bs = max_bs
        self.max_pp = max_pp
        self.max_op = max_op
        self.n_iter = 1
        self.seed = 0
        self.beam_size = 3
        self.add_evo_search = add_evo_search

        self.evaluator_method = "fast_simulator"
        self.parallel_evaluator = False
        self.parallel_initial_placement = False

        if ((self.parallel_evaluator or self.parallel_initial_placement)
            and not ray.is_initialized()):
            ray.init(address="auto", ignore_reinit_error=True)

    def solve_placement(self,
                        model_datas: List[ModelData],
                        cluster_env: ClusterEnv,
                        train_workload: Workload = None):
        # Generate workloads
        if train_workload is None:
            train_workload = gen_train_workload(model_datas)

        evaluator = PlacementEvaluator(model_datas, cluster_env, train_workload,
            self.evaluator_method, self.parallel_evaluator)

        # Get initial solutions
        initial_sols = self.enumerate_group_configs(cluster_env)
        #initial_sols = self.greedy_group_configs(
        #        model_datas, cluster_env, train_workload, evaluator)

        if self.parallel_initial_placement:
            func = ray.remote(replica_placement_fast_greedy).remote
            for i in range(len(initial_sols)):
                initial_sols[i] = func(
                    initial_sols[i], model_datas, cluster_env, train_workload, None,
                    self.verbose)
            initial_sols = ray.get(initial_sols)
        else:
            for i in range(len(initial_sols)):
                initial_sols[i] = replica_placement_fast_greedy(
                    initial_sols[i], model_datas, cluster_env, train_workload, evaluator,
                     self.verbose)
                #initial_sols[i] = replica_placement_beam_search(
                #    initial_sols[i], model_datas, cluster_env, train_workload, evaluator,
                #     self.beam_size, self.verbose)

        # Iterative search
        cur_sols = initial_sols
        best_score = -1
        best_sol = None

        it = 0
        tic = time.time()
        while it < self.n_iter:
            scores = evaluator.get_scores(cur_sols)

            tmp_best_idx = np.argmax(scores)

            if scores[tmp_best_idx] > best_score:
                best_score = scores[tmp_best_idx]
                best_sol = cur_sols[tmp_best_idx]

            if self.verbose >= 1:
                print(f"iter: {it}, best score: {best_score}, "
                      f"iter score: {scores[tmp_best_idx]}, "
                      f"iter #sol: {len(scores)}, "
                      f"elapsed: {time.time() - tic:.2f}, "
                      f"best placement: {best_sol}, ")

            if self.verbose >= 2:
                print("\n--- iter sols ---")
                for i in range(len(cur_sols)):
                    print(f"idx={i}")
                    print(f"placement={cur_sols[i]}")
                    print(f"score={scores[i]:.3f}\n")
                print("-----------------")

            # TODO: mutate solution
            it += 1

        if self.add_evo_search:
            best_sol = evolutionary_search(
                [best_sol], model_datas, cluster_env,
                evaluator, 200, self.verbose)
        return best_sol, {}

    def enumerate_group_configs(self, cluster_env):
        sols = []
        num_devices = cluster_env.num_devices
        num_devices_per_node = cluster_env.num_devices_per_node

        for group_size in get_factors(num_devices):
            if group_size > num_devices_per_node and group_size % num_devices_per_node != 0:
                continue

            for pp in get_factors(group_size):
                op = group_size // pp
                num_groups = num_devices // group_size

                if pp > self.max_pp or op > self.max_op:
                    continue

                sols.append(ModelPlacement([ParallelConfig(1, op, pp)] * num_groups,
                                           [[] for _ in range(num_groups)]))
        return sols

    def greedy_group_configs(self,
                             model_datas: List[ModelData],
                             cluster_env: ClusterEnv,
                             train_workload: Workload,
                             evaluator: PlacementEvaluator,
                             beam_size = 3):

        assert beam_size >= 1, "beam size should >= 1."

        num_devices = cluster_env.num_devices
        num_devices_per_node = cluster_env.num_devices_per_node

        beam_sols = [[ModelPlacement([], [])]]

        for cur_num in range(1, num_devices + 1):
            ## solve sols[cur_num]
            next_sols = []
            for last_group_size in range(1, (cur_num - 1) % num_devices_per_node + 1 + 1):
                ## solve from sols[cur_num - last_group_size]
                # print("last_group_size ", last_group_size)
                for pp in get_factors(last_group_size):
                    op = last_group_size // pp
                    if pp > self.max_pp or op > self.max_op:
                        continue

                    for sol in beam_sols[cur_num - last_group_size]:
                        pre_sol = sol.copy()
                        pre_sol.group_configs.append(ParallelConfig(1, op, pp))
                        pre_sol.group_models.append([])

                        #new_sol = replica_placement_on_last_group(
                        #new_sol = replica_placement_beam_search(
                        #              pre_sol, model_datas, cluster_env, train_workload,
                        #              evaluator, self.beam_size, self.verbose)
                        new_sol = replica_placement_fast_greedy(
                                      pre_sol, model_datas, cluster_env, train_workload,
                                      evaluator, self.verbose)
 
                        next_sols.append(new_sol)
            scores = evaluator.get_scores(next_sols)
            next_indices = np.argsort(scores)[::-1][:beam_size]
            beam_sols.append([])
            for i in range(len(next_indices)):
                beam_sols[cur_num].append(next_sols[next_indices[i]])

        return beam_sols[num_devices]
