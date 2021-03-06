#!/usr/bin/python
# encoding: utf-8
from functools import wraps, partial
import logging
import sys
from time import sleep, time
import traceback
import types
import uuid

from cocaine.worker import Worker
from cocaine.futures import chain

sys.path.append('/usr/lib')

import json
import msgpack

import elliptics

import log
log.setup_logger()
logger = logging.getLogger('mm.init')


import balancer
import balancelogicadapter
from db.mongo.pool import MongoReplicaSetClient
import infrastructure
import jobs
import cache
import minions
import node_info_updater
from planner import Planner
from config import config
from manual_locks import manual_locker



i = iter(xrange(100))
logger.info("trace %d" % (i.next()))

nodes = config.get('elliptics', {}).get('nodes', []) or config["elliptics_nodes"]
logger.debug("config: %s" % str(nodes))

logger.info("trace %d" % (i.next()))
log = elliptics.Logger(str(config["dnet_log"]), config["dnet_log_mask"])

node_config = elliptics.Config()
node_config.io_thread_num = config.get('io_thread_num', 1)
node_config.nonblocking_io_thread_num = config.get('nonblocking_io_thread_num', 1)
node_config.net_thread_num = config.get('net_thread_num', 1)

logger.info('Node config: io_thread_num {0}, nonblocking_io_thread_num {1}, '
    'net_thread_num {2}'.format(node_config.io_thread_num, node_config.nonblocking_io_thread_num,
        node_config.net_thread_num))

n = elliptics.Node(log, node_config)

logger.info("trace %d" % (i.next()))

addresses = []
for node in nodes:
    try:
        addresses.append(elliptics.Address(
            host=str(node[0]), port=node[1], family=node[2]))
    except Exception as e:
        logger.error('Failed to connect to storage node: {0}:{1}:{2}'.format(
            node[0], node[1], node[2]))
        pass

try:
    n.add_remotes(addresses)
except Exception as e:
    logger.error('Failed to connect to any elliptics storage node: {0}'.format(
        e))
    raise ValueError('Failed to connect to any elliptics storage node')

logger.info("trace %d" % (i.next()))
meta_node = elliptics.Node(log, node_config)

addresses = []
for node in config["metadata"]["nodes"]:
    try:
        addresses.append(elliptics.Address(
            host=str(node[0]), port=node[1], family=node[2]))
    except Exception as e:
        logger.error('Failed to connect to meta node: {0}:{1}:{2}'.format(
            node[0], node[1], node[2]))
        pass

logger.info('Connecting to meta nodes: {0}'.format(config["metadata"]["nodes"]))

try:
    meta_node.add_remotes(addresses)
except Exception as e:
    logger.error('Failed to connect to any elliptics meta storage node: {0}'.format(
        e))
    raise ValueError('Failed to connect to any elliptics storage META node')


wait_timeout = config.get('wait_timeout', 5)
logger.info('sleeping for wait_timeout for nodes '
             'to collect data ({0} sec)'.format(wait_timeout))
sleep(wait_timeout)

meta_wait_timeout = config['metadata'].get('wait_timeout', 5)

meta_session = elliptics.Session(meta_node)
meta_session.set_timeout(meta_wait_timeout)
meta_session.add_groups(list(config["metadata"]["groups"]))
logger.info("trace %d" % (i.next()))
n.meta_session = meta_session

mrsc_options = config['metadata'].get('options', {})

meta_db = None
if config['metadata'].get('url'):
    meta_db = MongoReplicaSetClient(config['metadata']['url'], **mrsc_options)

balancelogicadapter.setConfig(config["balancer_config"])


logger.info("trace %d" % (i.next()))
logger.info("before creating worker")
W = Worker(disown_timeout=config.get('disown_timeout', 2))
logger.info("after creating worker")


b = balancer.Balancer(n)


def register_handle(h):
    @wraps(h)
    def wrapper(request, response):
        start_ts = time()
        req_uid = uuid.uuid4().hex
        try:
            data = yield request.read()
            data = msgpack.unpackb(data)
            logger.info(":{req_uid}: Running handler for event {0}, "
                "data={1}".format(h.__name__, str(data), req_uid=req_uid))
            #msgpack.pack(h(data), response)
            res = h(data)
            if isinstance(res, chain.Chain):
                res = yield res
            else:
                logger.error('Synchronous handler for {0} handle'.format(h.__name__))
            response.write(res)
        except Exception as e:
            logger.error(":{req_uid}: handler for event {0}, "
                "data={1}: Balancer error: {2}\n{3}".format(
                    h.__name__, str(data), e,
                    traceback.format_exc().replace('\n', '    '),
                    req_uid=req_uid))
            response.write({"Balancer error": str(e)})
        finally:
            logger.info(':{req_uid}: Finished handler for event {0}, '
                'time: {1:.3f}'.format(h.__name__, time() - start_ts, req_uid=req_uid))
        response.close()

    W.on(h.__name__, wrapper)
    logger.info("Registering handler for event %s" % h.__name__)
    return wrapper


def init_infrastructure():
    infstruct = infrastructure.infrastructure
    infstruct.init(n)
    register_handle(infstruct.shutdown_node_cmd)
    register_handle(infstruct.start_node_cmd)
    register_handle(infstruct.disable_node_backend_cmd)
    register_handle(infstruct.enable_node_backend_cmd)
    register_handle(infstruct.reconfigure_node_cmd)
    register_handle(infstruct.recover_group_cmd)
    register_handle(infstruct.defrag_node_backend_cmd)
    register_handle(infstruct.search_history_by_path)
    b._set_infrastructure(infstruct)
    return infstruct


def init_node_info_updater():
    logger.info("trace node info updater %d" % (i.next()))
    niu = node_info_updater.NodeInfoUpdater(n)
    niu.start()
    register_handle(niu.force_nodes_update)

    return niu


def init_cache():
    manager = cache.CacheManager()
    if 'cache' in config:
        manager.setup(n.meta_session, config['cache'].get('index_prefix', 'cached_files_'))
        [manager.add_namespace(ns) for ns in config['cache'].get('namespaces', [])]

    # registering cache handlers
    register_handle(manager.get_cached_keys)
    register_handle(manager.get_cached_keys_by_group)
    register_handle(manager.upload_list)

    return manager


def init_statistics():
    register_handle(b.statistics.get_flow_stats)
    register_handle(b.statistics.get_groups_tree)
    register_handle(b.statistics.get_couple_statistics)
    return b.statistics


def init_minions():
    m = minions.Minions(n)
    register_handle(m.get_command)
    register_handle(m.get_commands)
    register_handle(m.execute_cmd)
    register_handle(m.terminate_cmd)
    return m


def init_planner(job_processor, niu):
    planner = Planner(n.meta_session, meta_db, niu, job_processor)
    register_handle(planner.restore_group)
    register_handle(planner.move_group)
    register_handle(planner.move_groups_from_host)
    return planner


def init_job_processor(minions, niu):
    j = jobs.JobProcessor(n, meta_db, niu, minions)
    register_handle(j.create_job)
    register_handle(j.cancel_job)
    register_handle(j.approve_job)
    register_handle(j.stop_jobs)
    register_handle(j.get_job_list)
    register_handle(j.get_job_status)
    register_handle(j.get_jobs_status)
    register_handle(j.retry_failed_job_task)
    register_handle(j.skip_failed_job_task)
    return j

def init_manual_locker(manual_locker):
    register_handle(manual_locker.host_acquire_lock)
    register_handle(manual_locker.host_release_lock)
    return manual_locker


co = init_cache()
io = init_infrastructure()
niu = init_node_info_updater()
b.niu = niu
init_statistics()
m = init_minions()
j = init_job_processor(m, niu)
po = init_planner(j, niu)
j.planner = po
ml = init_manual_locker(manual_locker)


for handler in balancer.handlers(b):
    logger.info("registering bounded function %s" % handler)
    register_handle(handler)

logger.info('activating timed queues')
try:
    tq_to_activate = [co, io, b.niu, m, j, po]
    for tqo in tq_to_activate:
        tqo._start_tq()
except Exception as e:
    logger.error('failed to activate timed queue: {0}'.format(e))
    raise
logger.info('finished activating timed queues')

logger.info("Starting worker")
W.run()
logger.info("Initialized")
