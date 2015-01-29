import logging

from jobs import JobBrokenError, TaskTypes
from minion_cmd import MinionCmdTask
import storage


logger = logging.getLogger('mm.jobs')


class RsyncBackendTask(MinionCmdTask):

    PARAMS = MinionCmdTask.PARAMS + ('node_backend',)

    def __init__(self, job):
        super(RsyncBackendTask, self).__init__(job)
        self.type = TaskTypes.TYPE_RSYNC_BACKEND_TASK

    def execute(self, minions):
        logger.info('Job {0}, task {1}: checking group {2} and node backend {3} '
            'state'.format(self.parent_job.id, self.id, self.group, self.node_backend))

        if not self.group in storage.groups:
            raise JobBrokenError('Group {0} is not found'.format(self.group))

        group = storage.groups[self.group]

        if self.node_backend in storage.node_backends:
            logger.info('Job {0}, task {1}: checking node backend status'.format(
                self.parent_job.id, self.id, self.node_backend))
            node_backend = storage.node_backends[self.node_backend]
            for nb in group.node_backends:
                if nb == node_backend and nb.status not in (storage.Status.STALLED, storage.Status.INIT):
                    raise JobBrokenError('Node backend {0} is still in group {1} and has status {2}, '
                        'expected {3}'.format(nb, group.group_id, nb.status, storage.Status.STALLED))
        elif len(group.node_backends) > 0:
            raise JobBrokenError('Group {0} is running on backend {1} which '
                'does not match {2}'.format(self.group, str(group.node_backends[0]),
                    self.node_backend))

        super(RsyncBackendTask, self).execute(minions)
