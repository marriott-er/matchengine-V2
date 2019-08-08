from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections import defaultdict
from multiprocessing import cpu_count
from typing import TYPE_CHECKING, Iterable

import dateutil.parser
import pymongo

from matchengine.internals.database_connectivity.mongo_connection import MongoDBConnection
from matchengine.internals.match_criteria_transform import MatchCriteriaTransform
from matchengine.internals.match_translator import (
    extract_match_clauses_from_trial,
    create_match_tree,
    get_match_paths,
    translate_match_path
)
from matchengine.internals.typing.matchengine_types import (
    PoisonPill,
    Cache,
    QueryTask,
    UpdateTask,
    RunLogUpdateTask,
    CheckIndicesTask,
    IndexUpdateTask
)
from matchengine.internals.utilities.query import (
    execute_clinical_queries,
    execute_genomic_queries,
    get_docs_results,
    get_valid_genomic_reasons,
    get_valid_clinical_reasons
)
from matchengine.internals.utilities.task_utils import (
    run_query_task,
    run_poison_pill,
    run_update_task,
    run_run_log_update_task,
    run_check_indices_task, run_index_update_task
)
from matchengine.internals.utilities.update_match_utils import async_update_matches_by_protocol_no
from matchengine.internals.utilities.utilities import (
    find_plugins
)

if TYPE_CHECKING:
    from typing import (
        NoReturn,
        Any
    )
    from matchengine.internals.typing.matchengine_types import (
        Dict,
        Union,
        List,
        Set,
        ClinicalID,
        MultiCollectionQuery,
        MatchReason,
        ObjectId,
        Trial,
        QueryNode,
        TrialMatch,
        Task,
        QueryNodeContainer
    )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')


class MatchEngine(object):
    cache: Cache
    config: Dict
    match_criteria_transform: MatchCriteriaTransform
    protocol_nos: Union[List[str], None]
    sample_ids: Union[List[str], None]
    match_on_closed: bool
    match_on_deceased: bool
    debug: bool
    num_workers: int
    clinical_ids: Set[ClinicalID]
    _task_q: asyncio.queues.Queue
    _matches: Dict[str, Dict[str, List[Dict]]]
    _loop: asyncio.AbstractEventLoop
    _queue_task_count: int
    _workers: Dict[int, asyncio.Task]

    def __enter__(self):
        return self

    async def _async_exit(self):
        """
        Ensure that all async workers exit gracefully.
        """
        for _ in range(0, self.num_workers):
            self._task_q.put_nowait(PoisonPill())
        await self._task_q.join()

    def __exit__(self, exception_type, exception_value, exception_traceback):
        """
        Teardown database connections (async + synchronous) and async workers gracefully.
        """
        if self.db_init:
            self._async_db_ro.__exit__(exception_type, exception_value, exception_traceback)
            self._async_db_rw.__exit__(exception_type, exception_value, exception_traceback)
            self._db_ro.__exit__(exception_type, exception_value, exception_traceback)
        self._loop.run_until_complete(self._async_exit())
        self._loop.stop()
        self._loop.close()

    def __init__(
            self,
            cache: Cache = None,
            sample_ids: Set[str] = None,
            protocol_nos: Set[str] = None,
            match_on_deceased: bool = False,
            match_on_closed: bool = False,
            debug: bool = False,
            num_workers: int = cpu_count() * 5,
            visualize_match_paths: bool = False,
            fig_dir: str = None,
            config: Union[str, dict] = None,
            plugin_dir: str = None,
            db_init: bool = True,
            db_name: str = None,
            match_document_creator_class: str = "DFCITrialMatchDocumentCreator",
            query_node_transformer_class: str = "DFCIQueryNodeTransformer",
            query_node_subsetter_class: str = "DFCIQueryNodeClinicalIDSubsetter",
            query_node_container_transformer_class: str = "DFCIQueryContainerTransformer",
            db_secrets_class: str = None,
            report_all_clinical_reasons: bool = False,
            ignore_run_log: bool = False,
            skip_run_log_entry: bool = False,
            trial_match_collection: str = "trial_match",
            drop: bool = False,
            exit_after_drop: bool = False,
            drop_accept: bool = False
    ):

        self.trial_match_collection = trial_match_collection
        self.starttime = datetime.datetime.now()
        self.run_id = uuid.uuid4()
        self.run_log_entries = dict()
        self.ignore_run_log = ignore_run_log
        self.skip_run_log_entry = skip_run_log_entry
        self.clinical_run_log_entries = dict()
        self._protocol_nos_param = protocol_nos
        self._sample_ids_param = sample_ids

        if isinstance(config, str):
            with open(config) as config_file_handle:
                self.config = json.load(config_file_handle)
        else:
            self.config = config

        self.match_criteria_transform = MatchCriteriaTransform(self.config)

        self.plugin_dir = plugin_dir
        self.match_document_creator_class = match_document_creator_class
        self.query_node_transformer_class = query_node_transformer_class
        self.query_node_container_transformer_class = query_node_container_transformer_class
        self.query_node_subsetter_class = query_node_subsetter_class
        self.db_secrets_class = db_secrets_class
        find_plugins(self)

        self.db_init = db_init
        self._db_ro = MongoDBConnection(read_only=True, async_init=False, db=db_name) if self.db_init else None
        self.db_ro = self._db_ro.__enter__() if self.db_init else None
        self._db_rw = MongoDBConnection(read_only=False, async_init=False, db=db_name) if self.db_init else None
        self.db_rw = self._db_rw.__enter__() if self.db_init else None
        log.info(f"Connected to database {self.db_ro.name}")
        self._drop = drop
        if self._drop:
            log.info((f"Dropping all matches"
                      "\n\t"
                      f"{f'for trials: {protocol_nos}' if protocol_nos is not None else 'all trials'}"
                      "\n\t"
                      f"{f'for samples: {sample_ids}' if sample_ids is not None else 'all samples'}"
                      "\n"
                      f"{'and then exiting' if exit_after_drop else 'and then continuing'}"))
            try:
                assert drop_accept or input('Type "yes" without quotes in all caps to confirm: ') == "YES"
                self.drop_existing_matches(protocol_nos, sample_ids)
            except AssertionError:
                log.error("Your response was not 'YES'; exiting")
                exit(1)
            if exit_after_drop:
                exit(0)

        # A cache-like object used to accumulate query results
        self.cache = Cache() if cache is None else cache
        self.sample_ids = sample_ids
        self.protocol_nos = protocol_nos
        self.match_on_closed = match_on_closed
        self.match_on_deceased = match_on_deceased
        self.report_all_clinical_reasons = report_all_clinical_reasons
        self.debug = debug
        self.num_workers = num_workers
        self.visualize_match_paths = visualize_match_paths
        self.fig_dir = fig_dir
        self._queue_task_count = int()
        self._matches = defaultdict(lambda: defaultdict(list))

        self.trials = self.get_trials()
        self._trials_to_match_on = self._get_trials_to_match_on(self.trials)
        if self.protocol_nos is None:
            self.protocol_nos = list(self.trials.keys())

        self._clinical_data = self._get_clinical_data()
        self.clinical_mapping = self.get_clinical_ids_from_sample_ids()
        self.clinical_update_mapping = dict() if self.ignore_run_log else self.get_clinical_updated_mapping()
        self.clinical_run_log_mapping = (dict()
                                         if self.get_clinical_ids_from_sample_ids()
                                         else self.get_clinical_run_log_mapping())
        self.clinical_extra_field_mapping = self.get_extra_field_mapping(self._clinical_data, "clinical")
        self.clinical_extra_field_lookup = self.get_extra_field_lookup(self._clinical_data, "clinical")
        self.sample_mapping = {sample_id: clinical_id for clinical_id, sample_id in self.clinical_mapping.items()}
        self.clinical_ids = set(self.clinical_mapping.keys())
        if self.sample_ids is None:
            self.sample_ids = list(self.clinical_mapping.values())
        self._param_cache = dict()

        # instantiate a new async event loop to allow class to be used as if it is synchronous
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())

    async def _async_init(self):
        """
        Instantiate asynchronous db connections and workers.
        Create a task que which holds all matching and update tasks for processing via workers.
        """
        self._task_q = asyncio.queues.Queue()
        self._async_db_ro = MongoDBConnection(read_only=True)
        self.async_db_ro = self._async_db_ro.__enter__()
        self._async_db_rw = MongoDBConnection(read_only=False)
        self.async_db_rw = self._async_db_rw.__enter__()
        self._workers = {
            worker_id: self._loop.create_task(self._queue_worker(worker_id))
            for worker_id in range(0, self.num_workers)
        }
        self._task_q.put_nowait(CheckIndicesTask())
        await self._task_q.join()

    async def run_query(self,
                        multi_collection_query: MultiCollectionQuery,
                        initial_clinical_ids: Set[ClinicalID]) -> List[MatchReason]:
        """
        Execute a mongo query on the clinical and genomic collections to find trial matches.
        First execute the clinical query. If no records are returned short-circuit and return.
        """
        clinical_ids = set(initial_clinical_ids)
        if multi_collection_query.clinical:
            new_clinical_ids, clinical_match_reasons = await execute_clinical_queries(self,
                                                                                      multi_collection_query,
                                                                                      clinical_ids
                                                                                      if clinical_ids
                                                                                      else set(initial_clinical_ids))
            clinical_ids = new_clinical_ids
        else:
            clinical_match_reasons = list()
        if not clinical_ids:
            return list()

        if multi_collection_query.genomic:
            new_clinical_ids, genomic_ids, genomic_match_reasons = await (
                execute_genomic_queries(self,
                                        multi_collection_query,
                                        clinical_ids
                                        if clinical_ids
                                        else set(
                                            initial_clinical_ids))
            )
            clinical_ids = new_clinical_ids
        else:
            genomic_ids = set()
            genomic_match_reasons = list()

        needed_clinical = list(clinical_ids)
        needed_genomic = list(genomic_ids)
        results = await get_docs_results(self, needed_clinical, needed_genomic)

        # asyncio.gather returns [[],[]]. Save the resulting values on the cache for use when creating trial matches
        for outer_result in results:
            for result in outer_result:
                self.cache.docs[result["_id"]] = result

        valid_reasons = get_valid_genomic_reasons(genomic_match_reasons, clinical_ids, genomic_ids)
        valid_reasons.extend(get_valid_clinical_reasons(self, clinical_match_reasons, clinical_ids))

        return valid_reasons

    async def _queue_worker(self, worker_id: int) -> None:
        """
        Function which executes tasks placed on the task queue.
        """
        while True:
            # Execute update task
            task: Task = await self._task_q.get()
            args = (self, task, worker_id)
            if isinstance(task, PoisonPill):
                await run_poison_pill(*args)
                break

            elif isinstance(task, QueryTask):
                await run_query_task(*args)

            elif isinstance(task, UpdateTask):
                await run_update_task(*args)

            elif isinstance(task, RunLogUpdateTask):
                await run_run_log_update_task(*args)

            elif isinstance(task, CheckIndicesTask):
                await run_check_indices_task(*args)

            elif isinstance(task, IndexUpdateTask):
                await run_index_update_task(*args)

    def query_node_transform(self, query_node: QueryNode) -> NoReturn:
        """Stub function to be overriden by plugin"""
        pass

    def query_node_container_transform(self, query_node_container: QueryNodeContainer) -> NoReturn:
        """Stub function to be overriden by plugin"""
        pass

    def genomic_query_node_clinical_ids_subsetter(self,
                                                  query_node: QueryNode,
                                                  clinical_ids: Iterable[ClinicalID]) -> Set[ClinicalID]:
        """Stub function to be overriden by plugin"""
        return {clinical_id for clinical_id in clinical_ids}

    def clinical_query_node_clinical_ids_subsetter(self,
                                                   query_node: QueryNode,
                                                   clinical_ids: Iterable[ClinicalID]) -> Set[ClinicalID]:
        """Stub function to be overriden by plugin"""
        return {clinical_id for clinical_id in clinical_ids}

    def update_matches_for_protocol_number(self, protocol_no: str):
        """
        Updates all trial matches for a given protocol number
        """
        self._loop.run_until_complete(async_update_matches_by_protocol_no(self, protocol_no))

    def update_all_matches(self):
        """
        Synchronously iterates over each protocol number, updating the matches in the database for each
        """
        for protocol_number in self.protocol_nos:
            self.update_matches_for_protocol_number(protocol_number)

    def get_matches_for_all_trials(self) -> Dict[str, Dict[str, List]]:
        """
        Synchronously iterates over each protocol number, getting trial matches for each
        """
        for protocol_no in self.protocol_nos:
            if protocol_no not in self._trials_to_match_on:
                logging.info((f'Trial {protocol_no} '
                              f'has status {self.trials[protocol_no]["status"]}, skipping'))
                continue
            self.get_matches_for_trial(protocol_no)
        return self._matches

    def get_matches_for_trial(self, protocol_no: str):
        """
        Get the trial matches for a given protocol number
        """
        log.info(f"Begin Protocol No: {protocol_no}")
        task = self._loop.create_task(self._async_get_matches_for_trial(protocol_no))
        return self._loop.run_until_complete(task)

    async def _async_get_matches_for_trial(self, protocol_no: str) -> Dict[str, List[Dict]]:
        """
        Asynchronous function used by get_matches_for_trial, not meant to be called externally.
        Gets the matches for a given trial
        """
        # Get each match clause in the trial document
        trial = self.trials[protocol_no]
        match_clauses = extract_match_clauses_from_trial(self, protocol_no)

        clinical_ids_to_run = self.get_clinical_ids_for_protocol(protocol_no)
        if not self.skip_run_log_entry:
            self.create_run_log_entry(protocol_no, clinical_ids_to_run)

        # for each match clause, create the match tree, and extract each possible match path from the tree
        for match_clause in match_clauses:
            match_tree = create_match_tree(self, match_clause)
            match_paths = get_match_paths(match_tree)

            # for each match path, translate the path into valid mongo queries
            for match_path in match_paths:
                query = translate_match_path(self, match_clause, match_path)
                if self.debug:
                    log.info(f"Query: {query}")
                # put the query onto the task queue for execution
                self._task_q.put_nowait(QueryTask(trial,
                                                  match_clause,
                                                  match_path,
                                                  query,
                                                  clinical_ids_to_run))
        log.info(f"Submitted {self._task_q.qsize()} QueryTasks to queue")
        await self._task_q.join()
        logging.info(f"Total results: {len(self._matches[protocol_no])}")
        return self._matches[protocol_no]

    def _get_clinical_data(self):
        # if no sample ids are passed in as args, get all clinical documents
        query: Dict = {} if self.match_on_deceased else {"VITAL_STATUS": 'alive'}
        if self.sample_ids is not None:
            query.update({"SAMPLE_ID": {"$in": list(self.sample_ids)}})
        projection = {'_id': 1, 'SAMPLE_ID': 1}
        if not self.ignore_run_log:
            projection.update({'_updated': 1, 'run_history': 1})
        projection.update({
            item[0]: 1
            for item
            in self.config.get("extra_initial_mapping_fields", dict()).get("clinical", list())})
        projection.update({
            item[0]: 1
            for item
            in self.config.get("extra_initial_lookup_fields", dict()).get("clinical", list())})
        return {result['_id']: result
                for result in
                self.db_ro.clinical.find(query, projection)}

    def get_clinical_updated_mapping(self) -> Dict[ObjectId: datetime.datetime]:
        return {clinical_id: clinical_data.get('_updated', None) for clinical_id, clinical_data in
                self._clinical_data.items()}

    def get_clinical_run_log_mapping(self) -> Dict[ObjectId: ObjectId]:
        output = {
            result['clinical_id']: result['run_history'] if result['run_history'] else None
            for result
            in self.db_ro.get_collection(
                f"clinical_run_history_{self.trial_match_collection}"
            ).find({'clinical_id': {"$in": list(self.clinical_ids)}})
        }
        for not_present in self.clinical_ids - set(output.keys()):
            output[not_present] = None
        return output

    def get_clinical_ids_from_sample_ids(self) -> Dict[ClinicalID, str]:
        """
        Clinical ids are unique to sample_ids
        """
        return {clinical_id: clinical_data['SAMPLE_ID'] for clinical_id, clinical_data in self._clinical_data.items()}

    def get_trials(self) -> Dict[str, Trial]:
        """
        Gets all the trial documents in the database, or just the relevant trials (if protocol numbers supplied)
        """
        trial_find_query = dict()

        # matching criteria can be set and extended in config.json. for more details see the README
        projection = self.match_criteria_transform.trial_projection

        if self.protocol_nos is not None:
            trial_find_query['protocol_no'] = {"$in": [protocol_no for protocol_no in self.protocol_nos]}

        all_trials = {
            result['protocol_no']: result
            for result in
            self.db_ro.trial.find(trial_find_query, dict({"_updated": 1, "last_updated": 1}, **projection))
        }
        return all_trials

    def _get_trials_to_match_on(self, trials: Dict[str, Trial]) -> Set[str]:
        return {
            protocol_no
            for protocol_no, trial
            in trials.items()
            if self.match_on_closed or trial.get('status', "key not found").lower().strip() in {"open to accrual"}
        }

    def create_run_log_entry(self, protocol_no, clinical_ids: Set[ClinicalID]):
        """
        Create a record of a matchengine run by protocol no.
        Include clinical ids ran during run. 'all' meaning all sample ids in the db, or a subsetted list
        Include original arguments.
        """
        run_log_clinical_ids_new = dict()
        if self._sample_ids_param is None:
            run_log_clinical_ids_new['all'] = None
        else:
            run_log_clinical_ids_new['list'] = list(self.clinical_ids)

        self.run_log_entries[protocol_no] = {
            'protocol_no': protocol_no,
            'clinical_ids': run_log_clinical_ids_new,
            'run_id': self.run_id.hex,
            'run_params': {
                'trials': self._protocol_nos_param,
                'sample_ids': self._sample_ids_param,
                'match_on_deceased': self.match_on_deceased,
                'match_on_closed': self.match_on_closed,
                'report_clinical_reasons': self.report_all_clinical_reasons,
                'workers': self.num_workers,
                'ignore_run_log': self.ignore_run_log
            },
            '_created': self.starttime
        }
        self.clinical_run_log_entries[protocol_no] = clinical_ids

    def get_clinical_ids_for_protocol(self, protocol_no: str) -> Set(ObjectId):
        """
        Take protocol from args and lookup all run_log entries after protocol_no._updated_date.
        Subset self.clinical_ids which have already been run since the trial's been updated.

        """

        if self.ignore_run_log:
            return self.clinical_ids
        # run logs since trial has been last updated
        default_datetime = 'January 01, 0001'
        fmt_string = '%B %d, %Y'
        trial_last_update = datetime.datetime.strptime(self.trials[protocol_no].get('last_updated', default_datetime),
                                                       fmt_string)
        query = {"protocol_no": protocol_no, "_created": {'$gte': trial_last_update}}
        run_log_entries = list(
            self.db_ro[f"run_log_{self.trial_match_collection}"].find(query).sort([("_created", pymongo.DESCENDING)])
        )

        if not run_log_entries:
            return self.clinical_ids

        clinical_ids_to_not_run = set()
        clinical_ids_to_run = set()

        # Iterate over all run logs
        for run_log in run_log_entries:
            # All sample ids are accounted for, short circuit
            if clinical_ids_to_not_run.union(clinical_ids_to_run) == self.clinical_ids:
                break

            run_log_clinical_ids = run_log['clinical_ids']
            is_all = 'all' in run_log_clinical_ids
            run_log_created_at = run_log['_created']

            # Check if clinical_id has been updated since last run with current protocol.
            # If it has been updated, run.
            for clinical_id, updated_at in self.clinical_update_mapping.items():
                if updated_at is None:
                    clinical_ids_to_run.add(clinical_id)
                elif updated_at > run_log_created_at and clinical_id not in clinical_ids_to_not_run:
                    if is_all or clinical_id in run_log_clinical_ids['list']:
                        clinical_ids_to_run.add(clinical_id)

            if is_all:
                clinical_ids_to_not_run.update(self.clinical_ids - clinical_ids_to_run)
                continue

            elif 'list' in run_log_clinical_ids:
                run_prev = set(run_log_clinical_ids['list']).intersection(self.clinical_ids)
                run_now_not_run_prev = self.clinical_ids - run_prev
                clinical_ids_to_run.update(run_now_not_run_prev - clinical_ids_to_not_run)
                clinical_ids_to_not_run.update(run_prev - clinical_ids_to_run)

        # ensure that we have accounted for all clinical ids
        assert clinical_ids_to_run.union(clinical_ids_to_not_run) == self.clinical_ids

        return clinical_ids_to_run

    def create_trial_matches(self, trial_match: TrialMatch) -> Dict:
        """Stub function to be overriden by plugin"""
        return dict()

    @property
    def task_q(self):
        return self._task_q

    @property
    def loop(self):
        return self._loop

    @property
    def queue_task_count(self):
        return self._queue_task_count

    @queue_task_count.setter
    def queue_task_count(self, value):
        self._queue_task_count = value

    @property
    def matches(self):
        return self._matches

    def get_extra_field_mapping(self, raw_mapping: Dict[ObjectId, Any], key: str) -> Dict[Any: Set[ObjectId]]:
        fields = self.config.get("extra_initial_mapping_fields", dict()).get(key, list())
        mapping = defaultdict(lambda: defaultdict(set))
        for obj_id, raw_map in raw_mapping.items():
            for field_name, field_transform in fields:
                field_value = raw_map.get(field_name)
                if field_transform == "date":
                    if not isinstance(field_value, datetime.datetime):
                        try:
                            field_value = dateutil.parser.parse(raw_map.get(field_name))
                        except ValueError:
                            field_value = None
                mapping[field_name][field_value].add(obj_id)
        return mapping

    def get_extra_field_lookup(self, raw_mapping: Dict[ObjectId, Any], key: str) -> Dict[Any: Set[ObjectId]]:
        fields = self.config.get("extra_initial_lookup_fields", dict()).get(key, list())
        mapping = defaultdict(dict)
        for obj_id, raw_map in raw_mapping.items():
            for field_name, field_transform in fields:
                field_value = raw_map.get(field_name)
                if field_transform == "date":
                    if not isinstance(field_value, datetime.datetime):
                        try:
                            field_value = dateutil.parser.parse(raw_map.get(field_name))
                        except ValueError:
                            field_value = None
                if field_value is not None:
                    mapping[field_name][obj_id] = field_value
        return mapping

    def drop_existing_matches(self, protocol_nos: List[str] = None, sample_ids: List[str] = None):
        assert self.db_ro.name != "matchminer"
        drop_query = dict()
        if protocol_nos is not None:
            drop_query.update({'protocol_no': {'$in': protocol_nos}})
        if sample_ids is not None:
            drop_query.update({'sample_id': {'$in': sample_ids}})
        if protocol_nos is None and sample_ids is None:
            self.db_rw.get_collection(self.trial_match_collection).drop()
        else:
            self.db_rw.get_collection(self.trial_match_collection).remove(drop_query)