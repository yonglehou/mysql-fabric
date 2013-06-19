import Queue
import threading
import logging
import uuid as _uuid
import traceback
import time

from weakref import WeakValueDictionary

import mysql.fabric.persistence as _persistence
import mysql.fabric.errors as _errors
import mysql.fabric.scheduler as _scheduler
import mysql.fabric.checkpoint as _checkpoint

from mysql.fabric.utils import Singleton

_LOGGER = logging.getLogger(__name__)

class Procedure(object):
    """Defines the context within which an operation is executed. Explicitly,
    an operation is a code block to be executed and is named a job.

    Any job must belong to a procedure whereas a procedure may have several
    jobs associated to it. When job is created and is about to be scheduled,
    it is added to a set of scheduled jobs. Upon the end of its execution,
    it is moved from the aforementioned set to a list of executed jobs.
    During the execution of a job, new jobs may be scheduled in the context
    of the current procedure.

    A procedure is marked as finished (i.e. complete) when its last job
    finishes. Specifically, when a job finishes and there is no scheduled
    job on behalf of the procedure.

    This class is mainly used to keep track of requests and to provide the
    necessary means to build a synchronous execution.
    """
    def __init__(self, uuid=None):
        """Create a Procedure object.
        """
        assert(uuid is None or isinstance(uuid, _uuid.UUID))
        self.__uuid = uuid or _uuid.uuid4()
        self.__lock = threading.Condition()
        self.__complete = False
        self.__result = True
        self.__scheduled_jobs = set()
        self.__executed_jobs = []
        self.__status = []

        _LOGGER.debug("Created procedure (%s, %s).",
            self.__uuid, time.time()
        )

    def get_lock_objects(self):
        """Return the objects that need to be locked before this procedure
        starts being executed.

        :return: List of objects to be locked.
        :rtype: List
        """
        # TODO: Create a routine to automatically determine the set of objects
        #       to be locked.
        return set(["lock"])

    def get_priority(self):
        """Return whether this procedure should have higher priority over
        other procedures that require access to a common subset of objects.

        :return: Whether the procedure has high priority or not.
        :rtype: Boolean
        """
        return False

    def is_complete(self):
        """Return whether the procedure has finished or not.

        :return: Whether the procedure has finished or not.
        :rtype: Boolean
        """
        with self.__lock:
            return self.__complete

    def get_scheduled_jobs(self):
        """Return the set of jobs has been scheduled on behalf of the
        procedure.

        :return: List of scheduled jobs.
        :rtype: List
        """
        with self.__lock:
            return list(self.__scheduled_jobs)

    def add_scheduled_job(self, job):
        """Register that a job has been scheduled on behalf of the
        procedeure.

        :param job: Scheduled job.
        """
        with self.__lock:
            assert(not self.__complete)
            assert(job not in self.__scheduled_jobs)
            assert(job not in self.__executed_jobs)
            assert(job.procedure == self)

            self.__scheduled_jobs.add(job)

    def add_executed_job(self, job):
        """Register that a job has been executed on behalf of the
        procedure.

        :param job: Executed job.
        """
        with self.__lock:
            assert(not self.__complete)
            assert(job in self.__scheduled_jobs)
            assert(job not in self.__executed_jobs)
            assert(job.procedure == self)

            self.__scheduled_jobs.remove(job)
            self.__executed_jobs.append(job)

            if job.result is not None:
                self.__result = job.result
            self.__status.extend(job.status)

            if not self.__scheduled_jobs:
                self.__complete = True
                self.__lock.notify_all()
                _checkpoint.Checkpoint.remove(job.checkpoint)
                _LOGGER.debug("Complete procedure (%s, %s).",
                    self.__uuid, time.time()
                )

    @property
    def uuid(self):
        """Return the procedure's uuid.
        """
        return self.__uuid

    @property
    def status(self):
        """Return the procedure's status which is a list of the
        statuses of all processes executed.
        """
        with self.__lock:
            assert(self.__complete)
            return self.__status

    @property
    def result(self):
        """Return the procedure's result which is the result of the
        last process executed on behalf of the procedure.
        """
        with self.__lock:
            assert(self.__complete)
            return self.__result

    def wait(self):
        """Wait until the procedure finishes its execution.
        """
        self.__lock.acquire()
        while not self.__complete:
            self.__lock.wait()
        self.__lock.release()


class Job(object):
    """Encapuslate a code block and is scheduled through the executor within
    the context of a procedure.
    """
    ERROR, SUCCESS = range(1, 3)
    EVENT_OUTCOME = [ERROR, SUCCESS]
    EVENT_OUTCOME_DESCRIPTION = {
        ERROR : "Error",
        SUCCESS : "Success"
    }

    ENQUEUED, PROCESSING, COMPLETE = range(3, 6)
    EVENT_STATE = [ENQUEUED, PROCESSING, COMPLETE]
    EVENT_STATE_DESCRIPTION = {
        ENQUEUED : "Enqueued",
        PROCESSING : "Processing",
        COMPLETE : "Complete"
    }

    def __init__(self, procedure, action, description,
                 args, kwargs, uuid=None):
        """Create a Job object.
        """
        if not callable(action):
            raise _errors.NotCallableError("Callable expected")
        elif not _checkpoint.Checkpoint.is_recoverable(action):
            # Currently we only print out a warning message. In the future,
            # we may decide to change this and raise an error.
            _LOGGER.warning(
                "(%s) is not recoverable. So after a failure Fabric may "
                "not be able to restore the system to a consistent state.",
                action
            )

        assert(uuid is None or isinstance(uuid, _uuid.UUID))
        self.__uuid = uuid or _uuid.uuid4()
        self.__action = action
        self.__args = args or []
        self.__kwargs = kwargs or {}
        self.__status = []
        self.__result = None
        self.__complete = False
        self.__procedure = procedure
        self.__is_recoverable = _checkpoint.Checkpoint.is_recoverable(action)
        self.__jobs = []
        self.__action_fqn = action.__module__ + "." + action.__name__

        self.__checkpoint = _checkpoint.Checkpoint(
            self.__procedure.uuid, self.__uuid, self.__action_fqn, args, kwargs
        )

        self._add_status(Job.SUCCESS, Job.ENQUEUED, description)
        self.__procedure.add_scheduled_job(self)

    @property
    def uuid(self):
        """Return the job's uuid.
        """
        return self.__uuid

    @property
    def procedure(self):
        """Return a reference to the procedure which the job is
        associated to.
        """
        return self.__procedure

    @property
    def status(self):
        """Return the status of the execution phases (i.e. scheduled,
        processing, completed).

        A status has the following format::

          status = {
            "when": time,
            "state" : state,
            "success" : success,
            "description" : description,
            "diagnosis" : "" if not diagnosis else \\
                          traceback.format_exc()
          }
        """
        assert(self.__complete)
        return self.__status

    @property
    def result(self):
        """Return the job's result.
        """
        assert(self.__complete)
        return self.__result

    @property
    def checkpoint(self):
        """Return the checkpoint associated with the job.
        """
        return self.__checkpoint

    @property
    def is_recoverable(self):
        """Return whether the job is recoverable or not.
        """
        return self.__is_recoverable

    def append_jobs(self, jobs):
        """Gather jobs that shall be scheduled after the current
        job is executed.

        :param jobs: List of jobs.
        """
        assert(isinstance(jobs, list))
        self.__jobs.extend(jobs)

    def _add_status(self, success, state, description, diagnosis=False):
        """Add a new status to this job.
        """
        assert(success in Job.EVENT_OUTCOME)
        assert(state in Job.EVENT_STATE)
        when = time.time()
        status = {
            "when" : when,
            "state" : state,
            "success" : success,
            "description" : description,
            "diagnosis" : "" if not diagnosis else traceback.format_exc(),
            }
        self.__status.append(status)

        _LOGGER.debug("%s job (%s, %s, %s, %s, %s).",
            Job.EVENT_STATE_DESCRIPTION[state],
            self.__procedure.uuid, self.__uuid, self.__action_fqn, when,
            Job.EVENT_OUTCOME_DESCRIPTION[success]
        )

    def execute(self, persister, scheduler, queue):
        """Execute the job.
        """
        try:
            # Update the job status.
            message = "Executing action ({0}).".format(self.__action.__name__)
            self._add_status(Job.SUCCESS, Job.PROCESSING, message)

            # Register that the job has started the execution.
            if self.__is_recoverable:
                self.__checkpoint.begin()

            # Start the job transactional context.
            persister.begin()

            # Execute the job.
            self.__result = self.__action(*self.__args, **self.__kwargs)

        except Exception as error: # pylint: disable=W0703
            # Report exception during execution.
            _LOGGER.exception(error)

            try:
                # Rollback the job transactional context.
                persister.rollback()
            except _errors.DatabaseError as db_error:
                _LOGGER.exception(db_error)

            # Update the job status.
            self.__result = False
            message = "Tried to execute action ({0}).".format(
                self.__action.__name__)
            self._add_status(Job.ERROR, Job.COMPLETE, message, True)

        else:
            try:
                # Register information jobs created within the context of the
                # current job.
                _checkpoint.register(self.__jobs, True)

                # Register that the job has finished the execution.
                if self.__is_recoverable:
                    self.__checkpoint.finish()

                # Commit the job transactional context.
                persister.commit()

                # Schedule jobs created within the context of the current
                # job.
                procedures = set()
                jobs = set()
                for job in self.__jobs:
                    if job.procedure != self.__procedure:
                        procedures.add(job.procedure)
                    else:
                        jobs.add(job)
                for procedure in procedures:
                    scheduler.enqueue_procedure(procedure)
                queue.schedule(list(jobs))
            except _errors.DatabaseError as db_error:
                _LOGGER.exception(db_error)

            # Update the job status.
            message = "Executed action ({0}).".format(self.__action.__name__)
            self._add_status(Job.SUCCESS, Job.COMPLETE, message)

        finally:
            # Mark the job as complete.
            self.__complete = True

            # Update the job status within the procedure.
            self.__procedure.add_executed_job(self)

    def __eq__(self,  other):
        """Two jobs are equal if they have the same uuid.
        """
        return isinstance(other, Job) and self.__uuid == other.uuid

    def __hash__(self):
        """A job is hashable through its uuid.
        """
        return hash(self.__uuid)

    def __str__(self):
        """Return a description on the job: <Job object: uuid=..., status=...>.
        """
        ret = "<Job object: " + \
               "uuid=" + str(self.__uuid) + ", " + \
               "status=" + str(self.__status) + \
               ">"
        return ret


class ExecutorThread(threading.Thread):
    """Class representing an executor thread for executing jobs.

    The thread will repeatedly read from the scheduler and execute a
    job. Note that the scheduler is shared between all thread instances.

    Each thread will create a persister and register it with the
    persistance system so that objects manipulated as part of the job
    execution can be persisted to the persistent store.

    :param scheduler: Scheduler which is responsible for scheduling procedures
    and jobs.
    """
    def __init__(self, scheduler):
        "Constructor for ExecutorThread."
        super(ExecutorThread, self).__init__(name="Executor")
        self.__scheduler = scheduler
        self.__queue = ExecutorQueue()
        self.__persister = None
        self.__job = None
        self.__current_thread = None
        self.daemon = True

    def is_current_thread(self):
        """Check if the current thread is the same as the executor's thread.
        """
        return self.__current_thread == threading.current_thread()

    @property
    def current_job(self):
        """Return a reference to the current job.
        """
        assert(self.__current_thread == threading.current_thread())
        return self.__job

    def run(self):
        """Run the executor thread.

        This function will repeatedly read jobs from the scheduler and
        execute them.
        """
        _LOGGER.debug("Initializing Executor thread %s", self.name)
        self.__persister = _persistence.MySQLPersister()
        _persistence.PersistentMeta.init_thread(self.__persister)
        # TODO: When is the persister closed? Apparently, this is
        # not automatically done and we need to fix that.

        self.__current_thread = threading.current_thread()

        procedure = None
        while True:
            if procedure is None or procedure.is_complete():
                procedure = self._next_procedure(procedure)
                _LOGGER.debug("Reading procedure from queue, found %s.",
                              procedure)

            self.__job = self.__queue.get()
            _LOGGER.debug("Reading next job from queue, found %s.",
                          self.__job)

            if self.__job is None:
                self.__queue.done()
                break

            self.__job.execute(self.__persister, self.__scheduler,
                               self.__queue)
            self.__queue.done()

    def _next_procedure(self, prv_procedure):
        self.__scheduler.done(prv_procedure)
        procedure = self.__scheduler.next_procedure()
        jobs = procedure.get_scheduled_jobs() if procedure else [None]
        self.__queue.schedule(jobs)
        return procedure


class ExecutorQueue(object):
    """Queue where scheduled jobs are put.
    """
    def __init__(self):
        """Constructor for ExecutorQueue.
        """
        self.__lock = threading.Condition()
        self.__queue = Queue.Queue()

    def get(self):
        """Remove a job from the queue.

        :return: Job or None which indicates that the Executor must
                 stop.
        """
        with self.__lock:
            while True:
                try:
                    job = self.__queue.get(False)
                    self.__lock.notify_all()
                    return job
                except Queue.Empty:
                    self.__lock.wait()

    def schedule(self, jobs):
        """Atomically put a set of jobs in the queue.

        :param jobs: List of jobs to be scheduled.
        """
        assert(isinstance(jobs, list) or jobs is None)
        with self.__lock:
            for job in jobs:
                while True:
                    try:
                        self.__queue.put(job, False)
                        self.__lock.notify_all()
                        break
                    except Queue.Full:
                        self.__lock.wait()

    def done(self):
         self.__queue.task_done()

class Executor(Singleton):
    """Class responsible for dispatching execution of procedures.

    Procedures to be executed are queued into the scheduler and
    sequentially executed.
    """
    def __init__(self):
        super(Executor, self).__init__()
        self.__scheduler = _scheduler.Scheduler()
        self.__procedures_lock = threading.RLock()
        self.__procedures = WeakValueDictionary()
        self.__thread_lock = threading.RLock()
        self.__thread = None

    @property
    def thread(self):
        """Return a reference to the ExecutorThread.
        """
        return self.__thread

    def start(self):
        """Start the executor.
        """
        with self.__thread_lock:
            _LOGGER.info("Starting Executor")
            if not self.__thread:
                self.__thread = ExecutorThread(self.__scheduler)
                self.__thread.start()
                _LOGGER.info("Executor started")
            else:
                raise _errors.ExecutorError("Executor is already running.")

    def shutdown(self):
        """Shut down the executor.
        """
        _LOGGER.info("Shutting down Executor.")
        thread = None
        with self.__thread_lock:
            if self.__thread and self.__thread.is_alive():
                self.__scheduler.enqueue_procedure(None)
                thread = self.__thread
            self.__thread = None
        if thread:
            _LOGGER.debug("Waiting until the Executor stops.")
            thread.join()
        _LOGGER.info("Executor has stopped")

    # TODO: MERGE AND REORGANIZE FUNCTIONS: enqueue_* and create_procedures.
    # TODO: FIX THIS BEFORE RELEASE 0.2.2.
    def create_procedures(self, within_procedure, nactions):
        """Schedule a job on behalf of a procedured.

        :within_procedure: Define if a new procedure will be created or not.
        :nactions: Number of procedures that shall be created.
        :return: Return a set of procedure objects.

        If the within_procedure parameter is not set, a new procedure is
        created. Otherwise, the job is associated to a previously defined
        procedure. When the within_procedure parameter uses a boolean type,
        the current job's procedure is associated to it and when it uses a
        UUID type, the new procedure takes its id upon the within_parameter.

        It is only possible to schedule jobs within the context of the current
        job's procedure if the request comes from the job's code block. If
        this does not happen, the :class:`mysql.fabric.errors.ProgrammingError`
        exception is raised.
        """
        procedures = None
        thread = None

        with self.__thread_lock:
            if self.__thread and self.__thread.is_alive():
                thread = self.__thread
            else:
                raise _errors.ExecutorError("Executor is not running.")
        assert(thread is not None)

        assert(isinstance(within_procedure, bool) or \
               isinstance(within_procedure, _uuid.UUID))
        if within_procedure and isinstance(within_procedure, bool) and \
            not thread.is_current_thread():
            raise _errors.ProgrammingError(
                "One can only create a job within the context "
                "of the current procedure from a job that belongs "
                "to this procedure."
                )
        elif within_procedure and isinstance(within_procedure, _uuid.UUID) \
            and thread.is_current_thread():
            raise _errors.ProgrammingError(
                "One can only create a job within the context "
                "of an specific procedure while recovering."
                )
        elif within_procedure and isinstance(within_procedure, bool):
            procedures = []
            for number in range(0, nactions):
                procedures.append(thread.current_job.procedure)
        elif within_procedure and isinstance(within_procedure, _uuid.UUID):
            procedures = []
            for number in range(0, nactions):
                procedure = Procedure(within_procedure)
                procedures.append(procedure)
                with self.__procedures_lock:
                    self.__procedures[procedure.uuid] = procedure
        else:
            procedures = []
            for number in range(0, nactions):
                procedure = Procedure()
                procedures.append(procedure)
                with self.__procedures_lock:
                    self.__procedures[procedure.uuid] = procedure
        assert(procedures is not None)

        return procedures

    def enqueue_procedure(self, within_procedure, do_action, description,
                          *args, **kwargs):
        """Schedule a procedure.

        :within_procedure: Define if a new procedure will be created or not.
        :param action: Callable to execute.
        :param description: Description of the job.
        :param args: Non-keyworded arguments to pass to the job.
        :param kwargs: Keyworded arguments to pass to the job.
        :return: Reference to the procedure.
        :rtype: Procedure
        """
        procedures = self.enqueue_procedures(
            within_procedure, [(do_action, description, args, kwargs)]
            )
        return procedures[0]

    def enqueue_procedures(self, within_procedure, actions):
        """Schedule a set of procedures.

        :param within_procedure: Whether a new procedure will be created or
                                 not for each action.
        :param actions: Set of actions to be scheduled and each action
                        corresponds to a procedure.
        :type actions: Dictionary [{"job" : Job uuid, "action" :
                       (action, description, non-keyword arguments,
                       keyword arguments)}, ...]
        :return: Return a set of procedure objects.
        """
        jobs = []
        nactions = len(actions)
        procedures = self.create_procedures(within_procedure, nactions)
        for number in range(0, nactions):
            do_action, description, args, kwargs = actions[number]
            job = Job(procedures[number], do_action, description, args, kwargs)
            jobs.append(job)

        thread = self.__thread
        if thread.is_current_thread() and isinstance(within_procedure, bool):
            thread.current_job.append_jobs(jobs)
        else:
            _checkpoint.register(jobs, False)
            assert(len(set(procedures)) == len(procedures))
            for procedure in procedures:
                self.__scheduler.enqueue_procedure(procedure)
        return procedures

    def reschedule_procedure(self, proc_uuid, actions):
        """Recovers a procedure after a failure by rescheduling it.

        :param proc_uuid: Procedure uuid.
        :param actions: Set of actions to be scheduled on behalf of
                        the procedure.
        :type actions: Dictionary [{"job" : Job uuid, "action" :
                       (action, description, non-keyword arguments,
                       keyword arguments)}, ...]
        :return: Return a procedure object.
        """
        jobs = []
        nactions = len(actions)
        procedures = self.create_procedures(proc_uuid, 1)
        for number in range(0, nactions):
            do_action, description, args, kwargs = actions[number]["action"]
            job = Job(
                procedures[0], do_action, description, args, kwargs,
                actions[number]["job"]
                )
            jobs.append(job)
        self.__scheduler.enqueue_procedure(procedures[0])
        return procedures[0]

    def get_procedure(self, proc_uuid):
        """Retrieve a reference to a procedure.
        """
        assert(isinstance(proc_uuid, _uuid.UUID))
        _LOGGER.debug("Checking procedure (%s).", proc_uuid)
        try:
            with self.__procedures_lock:
                procedure = self.__procedures[proc_uuid]
        except (KeyError, ValueError) as error:
            _LOGGER.exception(error)
            procedure = None

        return procedure

    def wait_for_procedure(self, procedure):
        """Wait until the procedure finishes the execution of all
        its jobs.
        """
        thread = None
        with self.__thread_lock:
            if self.__thread and self.__thread.is_alive():
                thread = self.__thread
            else:
                raise _errors.ExecutorError("Executor is not running.")

        if thread.is_current_thread():
            raise _errors.ProgrammingError(
                "One cannot wait for the execution of a procedure from "
                "a job."
                )

        procedure.wait()