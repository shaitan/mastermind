import datetime
import functools
import json
import logging
import random
import threading
import time
import traceback
import urllib

import elliptics
import msgpack
from tornado.httpclient import AsyncHTTPClient, HTTPClient, HTTPError
from tornado.simple_httpclient import SimpleAsyncHTTPClient
from tornado.ioloop import IOLoop

from config import config
import errors
import helpers as h
import keys
import timed_queue
import storage


logger = logging.getLogger('mm.minions')

MINIONS_CFG = config.get('minions', {})


class Minions(object):

    STATE_FETCH = 'state_fetch'
    STATE_FETCH_ACTIVE = 'state_fetch_active'
    CMD_ENTRY_FETCH = 'cmd_entry_%s_fetch'

    MAKE_IOLOOP = 'make_ioloop'
    STATE_URL_TPL = 'http://{host}:{port}/rsync/list/?finish_ts_gte={finish_ts_gte}'
    START_URL_TPL = 'http://{host}:{port}/rsync/start/'
    TERMINATE_URL_TPL = 'http://{host}:{port}/command/terminate/'

    def __init__(self, node):
        self.meta_session = node.meta_session.clone()

        self.commands = {}
        self.cmd_progress = {}
        self.active_hosts = []

        # self.pending_hosts = None
        # self.ready = False

        self.__tq = timed_queue.TimedQueue()

        self.__tq.add_task_in(self.MAKE_IOLOOP, 0,
            self._make_tq_thread_ioloop)

        self.__tq.add_task_in(self.STATE_FETCH,
            5, self._fetch_states)

        self.minion_headers = ({'X-Auth': config['minions']['authkey']}
                               if MINIONS_CFG.get('authkey') else
                               None)
        self.minion_port = MINIONS_CFG.get('port', 8081)

        self.__commands_lock = threading.Lock()
        self.__active_hosts_lock = threading.Lock()

    def _start_tq(self):
        self.__tq.start()

    def _make_tq_thread_ioloop(self):
        logger.debug('Minion states, creating thread ioloop')
        io_loop = IOLoop()
        io_loop.make_current()

    def _fetch_states(self, active_hosts=False):
        logger.info('Fetching minion states task started')
        try:
            states = {}

            if not active_hosts:
                hosts = storage.hosts.keys()
            else:
                if not self.active_hosts:
                    return
                with self.__active_hosts_lock:
                    hosts = self.active_hosts
                    self.active_hosts = []

            random.shuffle(hosts)

            # fetch tasks finished in last 24 hours or not finished at all
            min_finish_ts = int(time.time()) - 24 * 60 * 60
            for host in hosts:
                url = self.STATE_URL_TPL.format(host=host.addr,
                                                port=self.minion_port,
                                                finish_ts_gte=min_finish_ts)
                states[url] = host
            logger.debug('Starting async batch')
            responses = AsyncHTTPBatch(states.keys(),
                headers=self.minion_headers,
                timeout=MINIONS_CFG.get('commands_fetch_timeout', 15)).get()

            successful_hosts = set()
            for url, response in responses.iteritems():
                host = states[url]

                try:
                    data = self._get_response(host, response)
                except HTTPError as e:
                    logger.error('Minion tasks fetch error: {0}'.format(e))
                    continue
                try:
                    self._process_state(host.addr, data)
                except errors.MinionApiError:
                    continue

                successful_hosts.add(host.addr)

            logger.info('Finished fetching minion states task')
        except errors.NotReadyError as e:
            logger.warn('Failed to sync minions state: '
                'minions history is not fetched')
        except Exception as e:
            logger.error('Failed to sync minions state: %s\n%s' %
                         (e, traceback.format_exc()))
        finally:
            self.__tq.add_task_in(self.STATE_FETCH,
                MINIONS_CFG.get('commands_fetch_period', 120),
                self._fetch_states)

    def _process_state(self, addr, response, sync=False):

        response_data = self._unwrap_response(json.loads(response), addr)

        hostname = storage.hosts[addr].hostname
        logger.debug('Received {0} minion task states '
            'from host {1} ({2})'.format(len(response_data), hostname, addr))
        for uid, state in response_data.iteritems():
            state['uid'] = uid
            state['host'] = addr
            state['hostname'] = hostname

            if 'group' in state:
                group_id = int(state['group'])
                if group_id in storage.groups:
                    couple = storage.groups[group_id].couple
                    if couple:
                        state['couple'] = str(couple)

        with self.__commands_lock:
            self.commands.update(response_data)

        for uid, state in response_data.iteritems():
            if (self.cmd_progress.get(uid) is None or
                int(self.cmd_progress[uid]) != int(state['progress'])):

                if not sync:
                    logger.debug('Adding new task {0}'.format(self.CMD_ENTRY_FETCH % uid))
                    try:
                        self.__tq.add_task_in(self.CMD_ENTRY_FETCH % uid,
                            1, self._history_entry_update, state)
                    except Exception as e:
                        logger.info('Task for command {0} already '
                            'added to task queue'.format(uid))
                        continue
                else:
                    self._history_entry_update(state)

        for uid, state in response_data.iteritems():
            if state['progress'] < 1.0:
                with self.__active_hosts_lock:
                    self.active_hosts.append(storage.hosts[addr])

                try:
                    self.__tq.add_task_in(self.STATE_FETCH_ACTIVE,
                        MINIONS_CFG.get('active_fetch_period', 5),
                        self._fetch_states,
                        active_hosts=True)
                except Exception:
                    # task already exists
                    pass

                break

        return response_data

    def _get_response(self, host, response):
        if response.error:
            error = response.error
            if not isinstance(error, HTTPError):
                raise TypeError('Unexpected http error type "{0}": '
                    '{1}'.format(type(error).__name__, error))
            code = error.code
            if code == 599:
                error_msg = ('Failed to connect to minion '
                             'on host {0} ({1})'.format(host, error.message))
            else:
                error_msg = ('Minion http error on host {0}, '
                             'code {1} ({2})'.format(host, code, error.message))
            logger.error(error_msg)
            raise error

        return response.body

    def _unwrap_response(self, response, host):
        if response['status'] != 'success':
            logger.warn('Host: {0}, minion returned error: {1}'.format(
                host, response['error']))
            raise errors.MinionApiError('Minion error: {0}'.format(response['error']))
        return response['response']

    @h.concurrent_handler
    def get_command(self, request):
        try:
            uid = request[0]
            return self._get_command(uid)
        except ValueError:
            raise ValueError('Unknown command uid {0}'.format(uid))

    def _get_command(self, uid):
        return self._get_last_cmd_state(uid)

    @h.concurrent_handler
    def get_commands(self, request):
        return sorted(self.commands.itervalues(), key=lambda c: c['start_ts'])

    @h.concurrent_handler
    def execute_cmd(self, request):
        try:
            host, command, params = request[0:3]
        except ValueError:
            raise ValueError('Invalid parameters')

        if not host in storage.hosts:
            logger.debug('Host was not found: {0}, {1}'.format(host, type(host)))
            raise ValueError('Host {0} is not present in cluster'.format(host))

        return self._execute_cmd(host, command, params)

    def _execute_cmd(self, host, command, params):
        url = self.START_URL_TPL.format(host=host, port=self.minion_port)
        data = {'command': command}

        params_rename = {
            'success_codes': 'success_code'
        }

        def check_param(key, val):
            if not isinstance(val, (int, long, basestring)):
                logger.warn('Failed parameter {0}, value {1}'.format(key, val))
                raise ValueError('Only strings are accepted as command parameters')

        for k, v in params.iteritems():
            if k == 'command':
                raise ValueError('Parameter "command" is not accepted as command parameter')

            if isinstance(v, (list, tuple)):
                for val in v:
                    check_param(k, val)
            else:
                check_param(k, v)
            data[params_rename.get(k, k)] = v

        io_loop = IOLoop()
        client = SimpleAsyncHTTPClient(io_loop)
        response = io_loop.run_sync(functools.partial(
            client.fetch, url, method='POST',
                               headers=self.minion_headers,
                               body=urllib.urlencode(data, doseq=True),
                               request_timeout=MINIONS_CFG.get('request_timeout', 5.0),
                               allow_ipv6=True,
                               use_gzip=True))

        data = self._process_state(host, self._get_response(host, response), sync=True)
        return data

    @h.concurrent_handler
    def terminate_cmd(self, request):
        try:
            host, uid = request[0:2]
        except ValueError:
            raise ValueError('Invalid parameters')

        if not host in storage.hosts:
            logger.debug('Host was not found: {0}, {1}'.format(host, type(host)))
            raise ValueError('Host {0} is not present in cluster'.format(host))

        return self._terminate_cmd(host, uid)

    def _terminate_cmd(self, host, uid):
        url = self.TERMINATE_URL_TPL.format(host=host, port=self.minion_port)
        data = {'cmd_uid': uid}

        io_loop = IOLoop()
        client = SimpleAsyncHTTPClient(io_loop)
        response = io_loop.run_sync(functools.partial(
            client.fetch, url, method='POST',
                               headers=self.minion_headers,
                               body=urllib.urlencode(data),
                               request_timeout=MINIONS_CFG.get('request_timeout', 5.0),
                               allow_ipv6=True,
                               use_gzip=True))

        data = self._process_state(host, self._get_response(host, response), sync=True)
        return data

    def _history_entry_update(self, state):
        try:
            uid = state['uid']
            logger.debug('Started updating minion history entry '
                         'for command {0}'.format(uid))
            update_history_entry = False
            try:
                history_state = self._get_last_cmd_state(uid)
                self.cmd_progress[uid] = history_state['progress']
                # updating if process is finished
                if int(self.cmd_progress[uid]) != int(state['progress']):
                    update_history_entry = True
            except ValueError as e:
                logger.debug(e)
                update_history_entry = True

            if update_history_entry:
                if isinstance(uid, unicode):
                    uid = uid.encode('utf-8')
                eid = self.meta_session.transform(keys.MINION_HISTORY_ENTRY_KEY % uid)
                start = datetime.datetime.fromtimestamp(state['start_ts'])
                key = keys.MINION_HISTORY_KEY % start.strftime('%Y-%m')
                logger.info('Updating minion history entry for command {0} ({1})'.format(uid, key))
                self.meta_session.update_indexes(eid, [key], [self._serialize(state)]).get()
                self.cmd_progress[uid] = state['progress']

        except Exception as e:
            logger.error('Failed to update minion history entry for '
                         'uid {0}: {1}\n{2}'.format(uid, str(e), traceback.format_exc()))

    @staticmethod
    def _unserialize(data):
        return msgpack.unpackb(data)

    @staticmethod
    def _serialize(data):
        return msgpack.packb(data)

    def _get_last_cmd_state(self, uid):
        try:
            if isinstance(uid, unicode):
                uid = uid.encode('utf-8')
            eid = self.meta_session.transform(keys.MINION_HISTORY_ENTRY_KEY % uid)
            r = self.meta_session.list_indexes(eid).get()[0]
            state = self._unserialize(r.data)
        except (elliptics.NotFoundError, IndexError):
            raise ValueError('Unknown command uid {0}'.format(uid))

        return state


class AsyncHTTPBatch(object):

    def __init__(self, urls, headers=None, timeout=3):
        self.left = len(urls)
        self.urls = urls
        self.timeout = timeout
        self.headers = headers
        self.ioloop = IOLoop.current()
        logger.debug('Minion states, ioloop fetched: {0}'.format(self.ioloop))
        self.responses = {}

    def get(self, emergency_timeout=None):
        logger.debug('Minion states, creating async http clients')
        if not self.urls:
            logger.warn('Empty url list for async http batch')
            return self.responses
        max_clients = len(self.urls)
        [AsyncHTTPClient(max_clients=max_clients).fetch(url, callback=self._process,
            request_timeout=self.timeout, headers=self.headers, allow_ipv6=True)
         for url in self.urls]
        logger.debug('Minion states, starting ioloop')
        self.ioloop.start()
        return self.responses

    def _process(self, response):
        logger.debug('Minion states, received response from {0} '
                     '({1:.4f}s)'.format(response.request.url, response.request_time))
        self.responses[response.request.url] = response
        self.left -= 1
        if not self.left:
            logger.debug('Minion states, stopping loop')
            self.ioloop.stop()

    def _emergency_halt(self):
        logger.warn('Minion states, emergency exit. '
                    'Unprocessed requests: {0}'.format(self.left))
        self.ioloop.stop()

