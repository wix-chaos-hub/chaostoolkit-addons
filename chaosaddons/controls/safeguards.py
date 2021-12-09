__doc__ = """
The safeguard control provides a mechanism to keep an eye on the system
while running an experiment to decide if the experiment ought to stop as soon
as possible or not.

For instance, let's say your system detects a dire condition that has nothing
to do with this experiment. It may decide it's time for the experiment to
terminate as it could create even more noise or problems.

To use this control, simply add the following to your global (or per
experiment) controls block:

```json
"controls": [
    {
        "name": "safeguard",
        "provider": {
            "type": "python",
            "module": "chaosaddons.controls.safeguards",
            "arguments": {
                "probes": [
                    {
                        "name": "safeguard_1",
                        "type": "probe",
                        "provider": {
                            "type": "python",
                            "module": "mymodule",
                            "func": "checkstuff"
                        },
                        "background": true,
                        "tolerance": true
                    },
                    {
                        "name": "safeguard_2",
                        "type": "probe",
                        "provider": {
                            "type": "python",
                            "module": "mymodule",
                            "func": "checkstuff"
                        },
                        "tolerance": true
                    },
                    {
                        "name": "safeguard_3",
                        "type": "probe",
                        "provider": {
                            "type": "python",
                            "module": "mymodule",
                            "func": "checkstuff"
                        },
                        "frequency": 2,
                        "tolerance": true
                    }
                ]
            }
        }
    }
],
```

In this example, we declare three safeguard probes. The first one will run
once in the background as soon as possible. The second one will run once
before the experiment starts. The third one will run repeatedly every 2
seconds.

If either of them doesn't meet its tolerance, the entire execution will
terminate as soon as possible and leave the status of the experiment to
`interrupted`.

Probes that do not declare the `background` or `frequency` properties are meant
to run before the experiment really starts and will block until they are all
finished. This offers a mechanism for pre-checking the system's health.

When the properties are set, the probes run as soon as possible but do not
block the experiment from carrying on.

Bear in mind that your probes can also block the process from exiting. This
means that while the experiment has ended, your probe could be not returning
and therefore blocking the process. Make sure your probe do not make blocking
calls for too long.

The safeguard may take an extra boolean argument, `interrupt_after_activity`,
that, when set to `true`, requests that the experiment only gets interrupted
after the current activity rather than immediatly. This can be better in cases
where trying to exit during a blocking activity may lead to random behaviors
due to how Python behaves. With this flag, no signals are emitted and threads
are not arbitrarely interrupted.
see: https://github.com/chaostoolkit/chaostoolkit/issues/210

"""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from functools import partial
import threading
import time
import os
import signal
import traceback
from typing import List, Optional

from logzero import logger

from chaoslib.activity import run_activity
from chaoslib.caching import lookup_activity
from chaoslib.control import controls
from chaoslib.exceptions import ActivityFailed, InterruptExecution
from chaoslib.exit import exit_gracefully
from chaoslib.hypothesis import within_tolerance
from chaoslib.types import Activity, Configuration, \
    Experiment, Probe, Run, Secrets, Settings


from .synchronization import experiment_finished

guardian_lock = threading.Lock()


class State:
    def __init__(self) -> None:
        self._interrupted = False

    @property
    def interrupted(self) -> bool:
        """
        Flag that says one of our safeguards raised an execution interruption
        """
        with guardian_lock:
            return self._interrupted

    @interrupted.setter
    def interrupted(self, value: bool) -> None:
        """
        Set the interruption flag on.
        """
        with guardian_lock:
            self._interrupted = value


class Guardian(threading.local):
    def __init__(self) -> None:
        self._interrupt_after_activity = None

    def prepare(self, probes: List[Probe],
                interrupt_after_activity: Optional[bool] = None) -> None:
        """
        Configure the guardian so that it runs with the right amount of
        resources.
        """
        self._interrupt_after_activity = interrupt_after_activity

        once_count = 0
        repeating_count = 0
        now_count = 0
        for probe in probes:
            if probe.get("frequency"):
                repeating_count += 1
            elif probe.get("background"):
                once_count += 1
            else:
                now_count += 1

        self.repeating_until = threading.Event()
        self.now_all_done = threading.Barrier(parties=now_count + 1)
        self.now = ThreadPoolExecutor(max_workers=now_count or 1)
        self.once = ThreadPoolExecutor(max_workers=once_count or 1)
        self.repeating = ThreadPoolExecutor(max_workers=repeating_count or 1)

    def run(self, experiment: Experiment, probes: List[Probe],
            interrupt_after_activity: Optional[bool],
            configuration: Configuration, secrets: Secrets,
            settings: Settings) -> None:
        """
        Run the guardian safeguards in their own threads.

        If any probes is not flagged to run in the background (repeatedly
        or not), then this call blocks until all these pre-check safeguards
        are completed.
        """
        for p in probes:
            f = None
            if p.get("frequency"):
                f = self.repeating.submit(
                    run_repeatedly, experiment=experiment,
                    probe=p,
                    interrupt_after_activity=interrupt_after_activity,
                    configuration=configuration,
                    secrets=secrets, stop_repeating=self.repeating_until)
            elif p.get("background"):
                f = self.once.submit(
                    run_soon, experiment=experiment,
                    probe=p,
                    interrupt_after_activity=interrupt_after_activity,
                    configuration=configuration,
                    secrets=secrets)
            else:
                f = self.now.submit(
                    run_now, experiment=experiment,
                    probe=p,
                    interrupt_after_activity=interrupt_after_activity,
                    configuration=configuration,
                    secrets=secrets, done=self.now_all_done)

            if f is not None:
                f.add_done_callback(partial(self._log_finished, probe=p))

        # wait for all probes that must run first to complete
        # this allows the experiment to block until these are passed
        self.now_all_done.wait()

    def _log_finished(self, f: Future, probe: Probe) -> None:
        """
        Logs each safeguard when they terminated.
        """
        name = probe.get("name")
        x = f.exception()
        if x is not None:
            logger.debug(
                "Safeguard '{}' failed: {}".format(
                    name, str(x)), exc_info=x)
        else:
            logger.debug("Safeguard '{}' finished normally".format(name))

    def terminate(self) -> None:
        """
        Stop the guardian and all its safeguards.
        """
        self.repeating_until.set()
        self.now.shutdown(wait=True)
        self.repeating.shutdown(wait=True)
        self.once.shutdown(wait=True)

    def should_exit_before_activity(self) -> bool:
        return self._interrupt_after_activity and state.interrupted


guardian = Guardian()
state = State()


def configure_control(configuration: Configuration = None,
                      secrets: Secrets = None, settings: Settings = None,
                      experiment: Experiment = None,
                      probes: List[Probe] = None,
                      interrupt_after_activity: Optional[bool] = None) \
        -> None:
    guardian.prepare(probes, interrupt_after_activity)


def before_experiment_control(context: str,
                              configuration: Configuration = None,
                              secrets: Secrets = None,
                              settings: Settings = None,
                              experiment: Experiment = None,
                              probes: List[Probe] = None,
                              interrupt_after_activity: Optional[bool] = None) \
        -> None:
    guardian.run(
        experiment, probes, interrupt_after_activity, configuration,
        secrets, settings)


def after_experiment_control(**kwargs):
    guardian.terminate()


def after_activity_control(context: Activity, state: Run,
                           configuration: Configuration = None,
                           secrets: Secrets = None,
                           probes: List[Probe] = None,
                           interrupt_after_activity: Optional[bool] = None):
    # in case we are already finished, this shouldn't occur here though
    if experiment_finished.is_set():
        return

    if guardian.should_exit_before_activity():
        raise InterruptExecution(
            "Interrupting the experiment, after activity '{}', now as per "
            "your safeguards decision".format(context['name']))


###############################################################################
# Internals
###############################################################################
def run_repeatedly(experiment: Experiment, probe: Probe,
                   interrupt_after_activity: Optional[bool],
                   configuration: Configuration, secrets: Secrets,
                   stop_repeating: threading.Event) -> None:
    wait_for = probe.get("frequency")
    while not stop_repeating.is_set():
        run = execute_activity(
            experiment=experiment, probe=probe,
            configuration=configuration, secrets=secrets)
        interrupt_experiment_on_unhealthy_probe(
            probe, interrupt_after_activity, run, configuration, secrets)
        stop_repeating.wait(timeout=wait_for)


def run_soon(experiment: Experiment, probe: Probe,
             interrupt_after_activity: Optional[bool],
             configuration: Configuration,
             secrets: Secrets) -> None:
    run = execute_activity(
        experiment=experiment, probe=probe,
        configuration=configuration, secrets=secrets)
    interrupt_experiment_on_unhealthy_probe(
        probe, interrupt_after_activity, run, configuration, secrets)


def run_now(experiment: Experiment, probe: Probe,
            interrupt_after_activity: Optional[bool],
            configuration: Configuration,
            secrets: Secrets, done: threading.Barrier) -> None:
    try:
        run = execute_activity(
            experiment=experiment, probe=probe,
            configuration=configuration, secrets=secrets)
    finally:
        done.wait()

    interrupt_experiment_on_unhealthy_probe(
        probe, interrupt_after_activity, run, configuration, secrets)


def interrupt_experiment_on_unhealthy_probe(
        probe: Probe, interrupt_after_activity: Optional[bool], run: Run,
        configuration: Configuration, secrets=Secrets) -> None:
    if experiment_finished.is_set():
        return

    tolerance = probe.get("tolerance")
    checked = within_tolerance(
        tolerance, run["output"], configuration=configuration,
        secrets=secrets)
    if not checked and not state.interrupted:
        state.interrupted = True
        if not experiment_finished.is_set():
            # we only immediately trigger the interrupt if not asked
            # to do it at the next activity instead
            if not interrupt_after_activity:
                logger.critical(
                    "Safeguard '{}' triggered the end of the "
                    "experiment".format(probe["name"]))
                exit_gracefully()
            else:
                logger.critical(
                    "Safeguard '{}' triggered the end of the "
                    "experiment. But we will exit only after the current "
                    "activity is completed".format(probe["name"]))
                os.kill(os.getpid(), signal.SIGKILL)


def execute_activity(experiment: Experiment, probe: Probe,
                     configuration: Configuration, secrets: Secrets) -> Run:
    """
    Low-level wrapper around the actual activity provider call to collect
    some meta data (like duration, start/end time, exceptions...) during
    the run.
    """
    ref = probe.get("ref")
    if ref:
        probe = lookup_activity(ref)
        if not probe:
            raise ActivityFailed(
                "could not find referenced activity '{r}'".format(r=ref))

    with controls(level="activity", experiment=experiment, context=probe,
                  configuration=configuration, secrets=secrets) as control:
        pauses = probe.get("pauses", {})
        pause_before = pauses.get("before")
        if pause_before:
            time.sleep(pause_before)

        start = datetime.utcnow()

        run = {
            "activity": probe.copy(),
            "output": None
        }

        result = None
        try:
            result = run_activity(probe, configuration, secrets)
            run["output"] = result
            run["status"] = "succeeded"
        except ActivityFailed as x:
            run["status"] = "failed"
            run["output"] = result
            run["exception"] = traceback.format_exception(type(x), x, None)
        finally:
            end = datetime.utcnow()
            run["start"] = start.isoformat()
            run["end"] = end.isoformat()
            run["duration"] = (end - start).total_seconds()

            pause_after = pauses.get("after")
            if pause_after:
                time.sleep(pause_after)

        control.with_state(run)

    return run
