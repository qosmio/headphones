

from apscheduler.jobstores.base import BaseJobStore, JobLookupError, ConflictingIdError
from apscheduler.util import maybe_ref, datetime_to_utc_timestamp, utc_timestamp_to_datetime
from apscheduler.job import Job

try:
    import pickle as pickle
except ImportError:  # pragma: nocover
    import pickle

try:
    from bson.binary import Binary
    from pymongo.errors import DuplicateKeyError
    from pymongo import MongoClient, ASCENDING
except ImportError:  # pragma: nocover
    raise ImportError('MongoDBJobStore requires PyMongo installed')


class MongoDBJobStore(BaseJobStore):
    """
    Stores jobs in a MongoDB database. Any leftover keyword arguments are directly passed to pymongo's `MongoClient
    <http://api.mongodb.org/python/current/api/pymongo/mongo_client.html#pymongo.mongo_client.MongoClient>`_.

    Plugin alias: ``mongodb``

    :param str database: database to store jobs in
    :param str collection: collection to store jobs in
    :param client: a :class:`~pymongo.mongo_client.MongoClient` instance to use instead of providing connection
                   arguments
    :param int pickle_protocol: pickle protocol level to use (for serialization), defaults to the highest available
    """

    def __init__(self, database='apscheduler', collection='jobs', client=None,
                 pickle_protocol=pickle.HIGHEST_PROTOCOL, **connect_args):
        super(MongoDBJobStore, self).__init__()
        self.pickle_protocol = pickle_protocol

        if not database:
            raise ValueError('The "database" parameter must not be empty')
        if not collection:
            raise ValueError('The "collection" parameter must not be empty')

        if client:
            self.connection = maybe_ref(client)
        else:
            connect_args.setdefault('w', 1)
            self.connection = MongoClient(**connect_args)

        self.collection = self.connection[database][collection]
        self.collection.ensure_index('next_run_time', sparse=True)

    def lookup_job(self, job_id):
        document = self.collection.find_one(job_id, ['job_state'])
        return self._reconstitute_job(document['job_state']) if document else None

    def get_due_jobs(self, now):
        timestamp = datetime_to_utc_timestamp(now)
        return self._get_jobs({'next_run_time': {'$lte': timestamp}})

    def get_next_run_time(self):
        document = self.collection.find_one({'next_run_time': {'$ne': None}}, fields=['next_run_time'],
                                            sort=[('next_run_time', ASCENDING)])
        return utc_timestamp_to_datetime(document['next_run_time']) if document else None

    def get_all_jobs(self):
        return self._get_jobs({})

    def add_job(self, job):
        try:
            self.collection.insert({
                '_id': job.id,
                'next_run_time': datetime_to_utc_timestamp(job.next_run_time),
                'job_state': Binary(pickle.dumps(job.__getstate__(), self.pickle_protocol))
            })
        except DuplicateKeyError:
            raise ConflictingIdError(job.id)

    def update_job(self, job):
        changes = {
            'next_run_time': datetime_to_utc_timestamp(job.next_run_time),
            'job_state': Binary(pickle.dumps(job.__getstate__(), self.pickle_protocol))
        }
        result = self.collection.update({'_id': job.id}, {'$set': changes})
        if result and result['n'] == 0:
            raise JobLookupError(id)

    def remove_job(self, job_id):
        result = self.collection.remove(job_id)
        if result and result['n'] == 0:
            raise JobLookupError(job_id)

    def remove_all_jobs(self):
        self.collection.remove()

    def shutdown(self):
        self.connection.disconnect()

    def _reconstitute_job(self, job_state):
        job_state = pickle.loads(job_state)
        job = Job.__new__(Job)
        job.__setstate__(job_state)
        job._scheduler = self._scheduler
        job._jobstore_alias = self._alias
        return job

    def _get_jobs(self, conditions):
        jobs = []
        failed_job_ids = []
        for document in self.collection.find(conditions, ['_id', 'job_state'], sort=[('next_run_time', ASCENDING)]):
            try:
                jobs.append(self._reconstitute_job(document['job_state']))
            except:
                self._logger.exception('Unable to restore job "%s" -- removing it', document['_id'])
                failed_job_ids.append(document['_id'])

        # Remove all the jobs we failed to restore
        if failed_job_ids:
            self.collection.remove({'_id': {'$in': failed_job_ids}})

        return jobs

    def __repr__(self):
        return '<%s (client=%s)>' % (self.__class__.__name__, self.connection)
