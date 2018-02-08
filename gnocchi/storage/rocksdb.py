# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from __future__ import absolute_import

import fnmatch

import daiquiri
import itertools
from oslo_config import cfg

try:
    import rocksdb
except ImportError:
    rocksdb = None

from gnocchi import storage
from gnocchi import utils


OPTS = [
    cfg.StrOpt('rocksdb_path',
               default='/var/lib/gnocchi/storage.db',
               help='Path used to store gnocchi data files.'),
]

LOG = daiquiri.getLogger(__name__)

if rocksdb is not None:
    # TODO(sileht): Make this configurable ?
    # This random values come from the doc
    class UUIDPrefix(rocksdb.interfaces.SliceTransform):
        UUID_LEN = 36 + 1  # NOTE(sileht) +1 for the dot after the UUID

        def name(self):
            return b'uuid'

        def transform(self, src):
            return (0, self.UUID_LEN)

        def in_domain(self, src):
            return len(src) >= self.UUID_LEN

        def in_range(self, dst):
            return len(dst) == self.UUID_LEN


class RocksDBStorage(storage.StorageDriver):
    WRITE_FULL = True

    def __init__(self, conf):
        super(RocksDBStorage, self).__init__(conf)
        self.rocksdb_path = conf.rocksdb_path

        if rocksdb is None:
            raise RuntimeError("python-rocksdb module is unavailable")

        # NOTE(sileht): Depending of the used disk, the database configuration
        # should differ.
        # https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide#difference-of-spinning-disk
        # https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide#prefix-database-on-flash-storage
        # I pick recommanded options for flash storage
        options = rocksdb.Options()
        options.create_if_missing = True
        options.compression = "snappy_compression"
        options.max_open_files = -1

        options.compaction_style = 'level'
        options.write_buffer_size = 64 * 1024 * 1024
        options.max_write_buffer_number = 3
        options.target_file_size_base = 64 * 1024 * 1024
        options.level0_file_num_compaction_trigger = 10
        options.level0_slowdown_writes_trigger = 20
        options.level0_stop_writes_trigger = 40
        options.max_bytes_for_level_base = 512 * 1024 * 1024
        options.max_background_compactions = 1
        options.max_background_flushes = 1
        # memtable_prefix_bloom_bits=1024 * 1024 * 8,

        # Creates an index with metric ids
        options.prefix_extractor = UUIDPrefix()
        options.table_factory = rocksdb.BlockBasedTableFactory(
            index_type="hash_search",
            filter_policy=rocksdb.BloomFilterPolicy(10),
            block_size=4 * 1024,
            block_cache=rocksdb.LRUCache(512 * 1024 * 1024, 10),
        )

        try:
            self._db = rocksdb.DB(self.rocksdb_path, options)
        except rocksdb.errors.Corruption:
            LOG.warning("RocksDB database corrupted, trying to repair.")
            rocksdb.repair_db(self.rocksdb_path, options)
            self._db = rocksdb.DB(self.rocksdb_path, options)

    FIELD_SEP = '_'

    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, str(self.rocksdb_path))

    def _metric_key(self, metric, name):
        return ("%s.%s" % (str(metric.id), name)).encode()

    @staticmethod
    def _unaggregated_field(version=3):
        return 'none' + ("_v%s" % version if version else "")

    @classmethod
    def _aggregated_field_for_split(cls, aggregation, key, version=3,
                                    granularity=None):
        path = cls.FIELD_SEP.join([
            str(key), aggregation,
            str(utils.timespan_total_seconds(granularity or key.sampling))])
        return path + '_v%s' % version if version else path

    def _create_metric(self, metric):
        key = self._metric_key(metric, self._unaggregated_field())
        if self._db.key_may_exist(key)[0]:
            raise storage.MetricAlreadyExists(metric)
        self._db.put(key, '')

    def _store_unaggregated_timeserie(self, metric, data, version=3):
        key = self._metric_key(metric, self._unaggregated_field(version))
        self._db.put(key, data)

    def _get_unaggregated_timeserie(self, metric, version=3):
        key = self._metric_key(metric, self._unaggregated_field(version))
        data = self._db.get(key)
        if data is None:
            raise storage.MetricDoesNotExist(metric)
        return data

    def _list_split_keys(self, metric, aggregation, granularity, version=3):
        key = self._metric_key(metric)
        split_keys = [
            k.decode("utf8").split(self.FIELD_SEP, 1)
            for k in self._list_metric_keys(
                    metric,
                    match=self._aggregated_field_for_split(
                        aggregation, '*', version, granularity))
        ]
        if not split_keys and not self._db.key_may_exist(key)[0]:
            raise storage.MetricDoesNotExist(metric)
        print(split_keys)
        return split_keys

    def _delete_metric_measures(self, metric, key, aggregation, version=3):
        field = self._aggregated_field_for_split(aggregation, key, version)
        key = self._metric_key(metric, field)
        self._db.delete(key)

    def _store_metric_measures(self, metric, key, aggregation,
                               data, offset=None, version=3):
        field = self._aggregated_field_for_split(
            aggregation, key, version)
        key = self._metric_key(metric, field)
        self._db.put(key, data)

    def _list_metric_keys(self, metric, match=None):
        prefix = self._metric_key(metric, "")
        it = self._db.iterkeys()
        it.seek(prefix)

        if match is not None:
            match = self._metric_key(metric, match)

        def matcher(item):
            return (item[0].startswith(prefix)
                    if match is None
                    else fnmatch.fnmatch(item[0], match))

        return itertools.takewhile(matcher, it)

    def _delete_metric(self, metric):
        batch = rocksdb.WriteBatch()
        for key in self._list_metric_keys(metric):
            batch.delete(key)
        self._db.write(batch)

    def _get_measures(self, metric, keys, aggregation, version=3):
        if not keys:
            return []
        keys = [self._metric_key(metric,
                                 self._aggregated_field_for_split(
                                     aggregation, key, version))
                for key in keys]
        try:
            return list(self._db.multi_get(keys).values())
        except rocksdb.errors.NotFound:
            key = self._metric_key(metric, self._unaggregated_field())
            if not self._db.key_may_exist(key)[0]:
                raise storage.MetricDoesNotExist(metric)
            raise storage.AggregationDoesNotExist(
                metric, aggregation, key.sampling)
