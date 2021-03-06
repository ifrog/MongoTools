import re
import logging
from datetime import datetime
from collections import defaultdict

from pymongo.errors import OperationFailure
from pymongo.cursor import _QUERY_OPTIONS

from mongotools.sequence import Sequence

log = logging.getLogger(__name__)

class Channel(object):

    def __init__(self, db, name):
        self.db = db
        self.name = name
        self._seq = Sequence(db)
        self._callbacks = defaultdict(list)
        self._position = self._seq.cur(self.name)

    def __repr__(self): # pragma no cover
        return '<Channel %s.%s>' % (
            self.db.name,
            self.name)

    def ensure_channel(self, capacity=2**15, message_size=1024):
        if self.name not in self.db.collection_names():
            self.db.create_collection(
                self.name,
                size=capacity * message_size,
                capped=True,
                max=capacity,
                autoIndexId=False)

    def sub(self, pattern, callback=None):
        re_pattern = re.compile('^' + re.escape(pattern))
        self._callbacks[re_pattern] # ensure key exists
        def decorator(func):
            self._callbacks[re_pattern].append(func)
            return func
        if callback is None: return decorator
        return decorator(callback)

    def pub(self, key, data=None):
        doc = dict(
            ts=self._seq.next(self.name),
            k=key, data=data)
        self.db[self.name].insert(doc, manipulate=False)
        return doc

    def multipub(self, messages):
        last_ts = self._seq.next(self.name, len(messages))
        ts_values = range(last_ts - len(messages) + 1, last_ts + 1)
        docs = [
            dict(msg, ts=ts)
            for ts, msg in zip(ts_values, messages) ]
        self.db[self.name].insert(docs, manipulate=False)
        return docs

    def cursor(self, await=False):
        if not self._callbacks:
            return iter([])
        spec = { 'ts': { '$gt': self._position } }
        regex = '|'.join(cb.pattern for cb in self._callbacks)
        spec['k'] = re.compile(regex)
        if await:
            options = dict(
                tailable=True,
                await_data=True)
        else:
            options = {}
        q = self.db[self.name].find(spec, **options)
        q = q.hint([('$natural', 1)])
        if await:
            q = q.add_option(_QUERY_OPTIONS['oplog_replay'])
        return q

    def handle_ready(self, raise_errors=False, await=False):
        cursor = self.cursor(await)
        while True:
            try:
                msg = cursor.next()
            except StopIteration:
                break
            except OperationFailure, err:
                log.warning(
                    'Error getting messages, may have dropped some: %r',
                    err)
                break
            self._position = msg['ts']
            to_call = []
            for pattern, callbacks in self._callbacks.items():
                if pattern.match(msg['k']):
                    to_call += callbacks
            for cb in to_call:
                try:
                    cb(self, msg)
                except:
                    if raise_errors:
                        raise
                    log.exception('Error in callback handling %r(%r)',
                                  cb, msg)
