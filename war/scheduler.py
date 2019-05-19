from itertools import product
import logging
import multiprocessing
import psutil
import time

from numpy import array, argsort, bincount, ceil, floor, prod, stack
from numpy.random import choice
import numpy
from scipy.optimize import minimize

from war.cformat import ColorFormat as CF


def optimize_task_config(available_slots, max_parallel_tasks,
                         max_validation_njobs, max_estimator_njobs):

    def _evaluate_task_config(x):
        return numpy.abs(prod(x) - available_slots)

    opt_res = minimize(
        _evaluate_task_config,
        (1, max_validation_njobs, max_estimator_njobs),
        method='SLSQP',
        bounds=(
            (1, max_parallel_tasks),
            (1, max_validation_njobs),
            (1, max_estimator_njobs)))
    fx, cx = floor(opt_res.x), ceil(opt_res.x)
    borders = stack([fx - 1, fx, cx, cx + 1])

    best_p, best = None, 0
    for on_task, on_cv, on_est in product(*borders.transpose().tolist()):
        total = on_task * on_cv * on_est
        if (on_task > max_parallel_tasks
            or on_cv > max_validation_njobs
            or on_est > max_estimator_njobs):
            continue
        if total == 0 or total > available_slots or total < best:
            continue
        best_p = dict(
            tasks=int(on_task),
            njobs_on_validation=int(on_cv),
            njobs_on_estimator=int(on_est))
        best = total

    if not best_p:
        raise ValueError('Could not solve optimization problem')
    return best_p


class Scheduler:

    def __init__(self, strategies, nconsumers, max_threads_per_evaluation,
                 cooperate):
        self.strategies = dict()
        self.nconsumers = nconsumers
        self.max_slots = nconsumers
        self.max_threads_per_evaluation = max_threads_per_evaluation
        self.slots_running = 0
        self._populate(strategies)
        self.tasks_finished = 0
        self.report_at_ntasks = 100
        self.improved_since_last_report = False
        self.last_error = None
        self._cooperate = cooperate
        self.proc = psutil.Process()
        self.cpu_count = multiprocessing.cpu_count()
        self.last_coop_time = None
        self.proc_children = list(self.proc.children(recursive=True))
        self._init_proc()

    def _init_proc(self):
        self.proc.cpu_percent()
        for child in self.proc_children:
            child.cpu_percent()

    def _populate(self, strategy_list):
        for strat in strategy_list:
            self.strategies[strat] = {
                'cumulative_time': strat.cache['cumulative_time'],
                'best': strat.cache['best'],
                'finished': strat.cache['finished'],
                'running': 0,
                'slots': 0,
                'exhausted': False
            }

    @property
    def cooperation_mode(self):
        return self._cooperate

    def set_max_slots(self, value):
        if self.max_slots == value:
            return
        if value > self.nconsumers:
            raise ValueError('Maximum number of slots must be up to {}'.format(
                self.nconsumers))
        curr = self.max_slots
        self.max_slots = value
        logger = logging.getLogger('war.scheduler')
        logger.info(
            CF('Changed number of slots from %d to %d').yellow,
            curr, self.max_slots)
        excess = self.slots_running - self.max_slots
        if excess > 0:
            logger.info(CF('There are %d slots above the current limit. '
                           'Waiting for end of normal execution.').yellow,
                        excess)

    def collect(self, result):
        logger = logging.getLogger('war.scheduler')
        self.slots_running -= result.jobs
        assert self.slots_running >= 0
        for strat in self.strategies:
            if hash(strat) != result.task.strategy_id:
                continue
            self.tasks_finished += 1
            self.strategies[strat]['running'] -= 1
            self.strategies[strat]['slots'] -= result.jobs
            self.strategies[strat]['cumulative_time'] += result.elapsed_time
            self.strategies[strat]['finished'] += 1
            if result.status == 'FAILED':
                logger.error(
                    '%s task failed: %s', strat.name,
                    result.error_info['message'])
                logger.error('Task id: %s/%s', strat.__class__.__name__,
                             result.task.id())
                self.last_error = result
                return
            score = result.agg['avg']
            if self.strategies[strat]['best']['agg']['avg'] < score:
                logger.info(
                    (str(CF('%s').bold.green) +
                     str(CF(' improvement: %.4f -> %.4f').green)),
                    strat.name,
                    self.strategies[strat]['best']['agg']['avg'],
                    score
                )
                self.strategies[strat]['best'] = {
                    'agg': result.agg,
                    'scores': result.scores,
                }
                self.improved_since_last_report = True
            assert self.strategies[strat]['running'] >= 0
            return
        raise ValueError('Strategy not found not mark task as finished.')

    def strategy_by_id(self, idx):
        return list(self.strategies.keys())[idx - 1]

    def set_weight(self, idx, weight):
        strategy = self.strategy_by_id(idx)
        curr = strategy.weight
        strategy.weight = weight
        logger = logging.getLogger('war.scheduler')
        logger.info(
            'Strategy %s weight changed from %.4f to %.4f.',
            strategy.name,
            curr, strategy.weight)

    def report_best(self, idx):
        logger = logging.getLogger('war.scheduler')
        if idx > len(self.strategies):
            logger.error('No strategy was found at index %d', idx)
            return
        import pprint
        from pygments import highlight
        from pygments.formatters import TerminalFormatter
        from pygments.lexers import PythonLexer
        pp = pprint.PrettyPrinter()
        strategy = self.strategy_by_id(idx)
        print(CF(strategy.name).bold)
        code = pp.pformat(self.strategies[strategy])
        fmt = highlight(code, PythonLexer(), TerminalFormatter())
        print(fmt)

    def available_slots(self):
        return max(0, self.max_slots - self.slots_running)

    def next(self):
        #assert self.slots_running <= self.nconsumers
        logger = logging.getLogger('war.scheduler')

        if self.last_coop_time is None:
            self.last_coop_time = time.time()

        if self._cooperate:
            self.cooperate()

        # Estimate available slots (CPU cores to use).
        available_slots = self.available_slots()
        if not available_slots:
            return []
        logger.debug(CF('We have %d slots to use').light_gray,
                     available_slots)
        # Get best scores plus eps (to avoid division by zero)
        probs = self.probabilities()
        # Sample from discrete probability function.
        selected = choice(len(self.strategies), size=available_slots, p=probs)
        selected = bincount(selected)
        # logger.debug('Selected estimators: %s', ', '.join(map(str, selected)))
        # Get maximum of parallelization on a (cross-)valitation's fit.
        if self.max_threads_per_evaluation:
            max_per_val = self.max_threads_per_evaluation
        else:
            max_per_val = self.nconsumers - self.max_threads_per_evaluation
        task_list = list()
        # Generate tasks
        for slots, strat in zip(selected, self.strategies):
            if not slots:
                continue
            # Get maximum of parallelization on an estimator's fit.
            if strat.max_threads_per_estimator > 0:
                max_per_est = strat.max_threads_per_estimator
            else:
                max_per_est = self.nconsumers + strat.max_threads_per_estimator
            # Get maximum of parallelization on an estimator's fit.
            if strat.max_parallel_tasks > 0:
                max_tasks = strat.max_parallel_tasks
            else:
                max_tasks = self.nconsumers + strat.max_parallel_tasks
            config = optimize_task_config(
                available_slots=slots,
                max_parallel_tasks=max_tasks,
                max_validation_njobs=max_per_val,
                max_estimator_njobs=max_per_est)
            allocated_slots_per_task = config['njobs_on_validation'] * \
                                       config['njobs_on_estimator']
            allocated_slots = config['tasks'] * allocated_slots_per_task
            created = 0
            for tid in range(config['tasks']):
                if (config['njobs_on_estimator'] == 0
                    or config['njobs_on_validation'] == 0):
                    continue
                try:
                    task = strat.next(nthreads=config['njobs_on_estimator'])
                    if not task:
                        raise ValueError('no task received to execute')
                    task.n_jobs = config['njobs_on_validation']
                    task.total_jobs = allocated_slots_per_task
                    self.strategies[strat]['running'] += 1
                    self.strategies[strat]['slots'] += allocated_slots_per_task
                    created += 1
                    self.slots_running += allocated_slots_per_task
                    task_list.append(task)
                except StopIteration:
                    self.strategies[strat]['exhausted'] = True
                    logger.info(
                        CF('%s is exhausted').bold.bottle_green,
                        strat.name)
                    break
                except Exception as err:
                    logger.error(
                        'Failed to create a task for %s: %s',
                        strat.name,
                        '{}: {}'.format(type(err).__name__, err))
            if created:
                logger.info(
                    CF('New %d × %s cv=%d fit=%d').dark_gray,
                    created,
                    strat.name,
                    config['njobs_on_estimator'],
                    config['njobs_on_validation'])
        return task_list

    def toggle_cooperate(self):
        logger = logging.getLogger('war.scheduler')
        if self._cooperate:
            self._cooperate = False
            logger.info(CF('Cooperation has been disabled.').cyan.bold)
        else:
            self._cooperate = True
            logger.info(CF('Cooperation has been enabled.').cyan.bold)
            logger.info(CF('The current number of slots is %d.').cyan,
                        self.max_slots)
            logger.info(CF('Collecting information for analysis.').cyan)
            self.last_coop_time = time.time()
            self._init_proc()

    def probabilities(self):
        weights = list()
        min_score = min(max(0, info['best']['agg']['avg'] * strat.weight)
                        for strat, info in self.strategies.items()
                        if not info['exhausted'])
        max_score = max(max(1, info['best']['agg']['avg'] * strat.weight)
                        for strat, info in self.strategies.items()
                        if not info['exhausted'])
        for strat, info in self.strategies.items():
            max_tasks = strat.max_tasks
            exhausted, finished = info['exhausted'], info['finished']
            if not (max_tasks == -1 or max_tasks > finished) or exhausted:
                weights.append(0)
                continue
            best_avg_score = info['best']['agg']['avg'] * strat.weight
            best_score = min(1, max(0, best_avg_score))
            norm_score = (best_score - min_score) / (max_score - min_score)
            warm_up = 2 * (strat.warm_up - info['finished'])
            weight = max(0, max(norm_score + 1e-6, warm_up))
            weights.append(weight)
        weights = array(weights) + 1e-6
        probs = weights / sum(weights)
        return probs

    def _average_worker_cpu_usage(self):
        logger = logging.getLogger('war.scheduler')
        perc_expected = self.slots_running / self.cpu_count
        ratios = list()
        for child in self.proc_children:
            perc_usage = child.cpu_percent() / 100
            ratio = perc_usage / (perc_expected + 1e-6)
            logger.debug(CF('CPU Usage: %5.1f%%').light_gray,
                100 * ratio)
            if ratio > 0:
                ratios.append(ratio)
        if not ratios:
            return (0, 0)
        active = max(self.slots_running, len(ratios))
        return (len(ratios), numpy.sum(ratios) / (active + 1e-6))

    def report_worker_usage(self):
        logger = logging.getLogger('war.scheduler')
        nactive, ratio = self._average_worker_cpu_usage()
        logger.info(
            CF('%d active workers, %d slots, average CPU usage: %.0f%%').cyan,
            nactive, self.slots_running, ratio * 100)

    def cooperate(self, force=False):
        if not force and (time.time() - self.last_coop_time) < 60:
            return
        self.last_coop_time = time.time()
        logger = logging.getLogger('war.scheduler')
        nactive, ratio = self._average_worker_cpu_usage()
        logger.info(
            CF('%d active workers, %d slots, average CPU usage: %.0f%%').cyan,
            nactive, self.slots_running, ratio * 100)
        if ratio == 0:
            return
        if self.slots_running > self.max_slots:
            logger.info(
                CF(
                    ('There are %d slots running above current limit %d. '
                     'Waiting them to finish.')).cyan,
                self.slots_running - self.max_slots, self.max_slots)
            return
        if ratio < 0.95 and self.max_slots > max(2, self.nconsumers // 2):
            max_slots = int(max(ceil(self.max_slots * ratio), 2))
            if max_slots != self.max_slots:
                # It's possible the reduction will not happen when
                # working if few slots.
                logger.warning(
                    ('Average worker CPU usage is at %.0f%%, '
                     'decreasing slots from %d to %d.'),
                    ratio * 100,
                    self.max_slots, max_slots)
                self.max_slots = max_slots
        elif ratio > 1.10 and self.max_slots < self.nconsumers:
            max_slots = self.max_slots + 1
            logger.warning(
                ('It seems we can use more CPU. '
                 'Increasing slots from %d to %d.'),
                self.max_slots, max_slots)
            self.max_slots = max_slots
