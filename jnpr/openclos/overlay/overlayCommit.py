'''
Created on Feb 15, 2016

@author: yunli
'''

import os
import traceback
import uuid
import logging
from threading import Thread, Event, RLock
import subprocess
import concurrent.futures
import Queue
from sqlalchemy.orm import exc
import time

from jnpr.openclos.overlay.overlayModel import OverlayFabric, OverlayTenant, OverlayVrf, OverlayNetwork, OverlaySubnet, OverlayDevice, OverlayL3port, OverlayL2port, OverlayAe, OverlayDeployStatus
from jnpr.openclos.dao import Dao
from jnpr.openclos.loader import defaultPropertyLocation, OpenClosProperty, DeviceSku, loadLoggingConfig
from jnpr.openclos.common import SingletonBase
from jnpr.openclos.exception import ConfigurationCommitFailed, DeviceRpcFailed, DeviceConnectFailed
from jnpr.openclos.deviceConnector import CachedConnectionFactory, NetconfConnection

DEFAULT_MAX_THREADS = 10
DEFAULT_DISPATCH_INTERVAL = 10
DEFAULT_DAO_CLASS = Dao

moduleName = 'overlayCommit'
loadLoggingConfig(appName=moduleName)
logger = logging.getLogger(moduleName)

class OverlayCommitJob():
    def __init__(self, parent, deployStatusObject):
        # Note we only hold on to the data from the deployStatusObject (deviceId, configlet, etc.). 
        # We are not holding reference to the deployStatusObject itself as it can become invalid when db session is out of scope
        self.parent = parent
        self.id = deployStatusObject.id
        self.deviceId = deployStatusObject.overlay_device.id
        self.deviceIp = deployStatusObject.overlay_device.address
        self.deviceUser = deployStatusObject.overlay_device.username
        self.devicePass = deployStatusObject.overlay_device.getCleartextPassword()
        self.configlet = deployStatusObject.configlet
        self.operation = deployStatusObject.operation
        self.queueId = '%s:%s' % (self.deviceIp, self.deviceId)

    def commit(self):
        try:
            # Note we don't want to hold the caller's session for too long since this function is potentially lengthy
            # that is why we don't ask caller to pass a dbSession to us. Instead we get the session inside this method
            # only long enough to update the status value
            logger.info("Job %s: starting commit on device [%s]", self.id, self.queueId)

            # first update the status to 'progress'
            try:
                with self.parent.dao.getReadWriteSession() as session:
                    statusObject = session.query(OverlayDeployStatus).filter(OverlayDeployStatus.id == self.id).one()
                    statusObject.update('progress', 'commit in progress', self.operation)
            except Exception as exc:
                logger.error("%s", exc)
                #logger.error('StackTrace: %s', traceback.format_exc())
                
            # now commit and set the result/reason accordingly
            result = 'success'
            reason = ''
            try:
                with CachedConnectionFactory.getInstance().connection(NetconfConnection,
                                                                      self.deviceIp,
                                                                      username=self.deviceUser,
                                                                      password=self.devicePass) as connector:
                    connector.updateConfig(self.configlet)
            except DeviceConnectFailed as exc:
                #logger.error("%s", exc)
                #logger.error('StackTrace: %s', traceback.format_exc())
                result = 'failure'
                reason = exc.message
            except DeviceRpcFailed as exc:
                #logger.error("%s", exc)
                #logger.error('StackTrace: %s', traceback.format_exc())
                result = 'failure'
                reason = exc.message
            except Exception as exc:
                #logger.error("%s", exc)
                #logger.error('StackTrace: %s', traceback.format_exc())
                result = 'failure'
                reason = str(exc)
            
            # commit is done so update the result and remove device id from cache
            try:
                with self.parent.dao.getReadWriteSession() as session:
                    statusObject = session.query(OverlayDeployStatus).filter(OverlayDeployStatus.id == self.id).one()
                    statusObject.update(result, reason, self.operation)
            except Exception as exc:
                logger.error("%s", exc)
                #logger.error('StackTrace: %s', traceback.format_exc())
                
            logger.info("Job %s: done with device [%s]", self.id, self.queueId)
            self.parent.markDeviceIdle(self.queueId)
        except Exception as exc:
            logger.error("Job %s: error '%s'", self.id, exc)
            logger.error('StackTrace: %s', traceback.format_exc())
            raise

class OverlayCommitQueue(SingletonBase):
    def __init__(self):
        self.dao = DEFAULT_DAO_CLASS.getInstance()
        # event to stop from sleep
        self.stopEvent = Event()
        self.__lock = RLock()
        self.__devicesInProgress = set()
        self.__deviceQueues = {}
        self.maxWorkers = DEFAULT_MAX_THREADS
        self.dispatchInterval = DEFAULT_DISPATCH_INTERVAL
        self.thread = Thread(target=self.dispatchThreadFunction, args=())
        self.started = False
        
        conf = OpenClosProperty().getProperties()
        # iterate 'plugin' section of openclos.yaml and install routes on all plugins
        if 'plugin' in conf:
            plugins = conf['plugin']
            for plugin in plugins:
                if plugin['name'] == 'overlay':
                    maxWorkers = plugin.get('threadCount')
                    if maxWorkers is not None:
                        self.maxWorkers = maxWorkers
                    dispatchInterval = plugin.get('dispatchInterval')
                    if dispatchInterval is not None:
                        self.dispatchInterval = dispatchInterval
                    break
        
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.maxWorkers)

    def addJob(self, deployStatusObject):
        job = OverlayCommitJob(self, deployStatusObject)
        logger.debug("Job %s: added to device [%s]", job.id, job.queueId)
        with self.__lock:
            if job.queueId not in self.__deviceQueues:
                self.__deviceQueues[job.queueId] = Queue.Queue()
            self.__deviceQueues[job.queueId].put(job)
        return job
    
    '''
    To be used by unit test only
    '''
    def _getDeviceQueues(self):
        return self.__deviceQueues
        
    def runJobs(self):
        # check device queues (round robin)
        # Note we only hold on to the lock long enough to retrieve the job from the queue.
        # Then we release the lock before we do the actual commit
        with self.__lock:
            toBeDeleted = []
            for queueId, deviceQueue in self.__deviceQueues.iteritems():
                # find an idle device
                if queueId not in self.__devicesInProgress:
                    self.__devicesInProgress.add(queueId)
                    logger.debug("Device [%s] has NO commit in progress. Prepare for commit", queueId)
                    # retrieve the job
                    try:
                        job = deviceQueue.get_nowait()
                        # start commit progress 
                        self.executor.submit(job.commit)
                        deviceQueue.task_done()
                        if deviceQueue.empty():
                            logger.debug("Device [%s] job queue is empty", queueId)
                            # Note don't delete the empty job queues within the iteration.
                            toBeDeleted.append(queueId)
                    except Queue.Empty as exc:
                        logger.debug("Device [%s] job queue is empty", queueId)
                        # Note don't delete the empty job queues within the iteration.
                        toBeDeleted.append(queueId)
                else:
                    logger.debug("Device [%s] has commit in progress. Skipped", queueId)
            
            # Now it is safe to delete all empty job queues
            for queueId in toBeDeleted:
                logger.debug("Deleting job queue for device [%s]", queueId)
                del self.__deviceQueues[queueId]
    
    def markDeviceIdle(self, queueId):
        with self.__lock:
            self.__devicesInProgress.discard(queueId)
    
    def start(self):
        with self.__lock:
            if self.started:
                return
            else:
                self.started = True
                
        logger.info("Starting OverlayCommitQueue...")
        self.thread.start()
        logger.info("OverlayCommitQueue started")
   
    def stop(self):
        logger.info("Stopping OverlayCommitQueue...")
        self.stopEvent.set()
        self.executor.shutdown()
        with self.__lock:
            if self.started:
                self.thread.join()
        logger.info("OverlayCommitQueue stopped")
    
    def dispatchThreadFunction(self):
        try:
            while True:
                self.stopEvent.wait(self.dispatchInterval)
                if not self.stopEvent.is_set():
                    self.runJobs()
                else:
                    logger.debug("OverlayCommitQueue: stopEvent is set")
                    return
                
        except Exception as exc:
            logger.error("Encounted error '%s' on OverlayCommitQueue", exc)
            raise

# def main():        
    # conf = OpenClosProperty().getProperties()
    # dao = Dao.getInstance()
    # from jnpr.openclos.overlay.overlay import Overlay
    # overlay = Overlay(conf, Dao.getInstance())
    # with dao.getReadWriteSession() as session:
        # d1 = overlay.createDevice(session, 'd1', 'description for d1', 'spine', '192.168.48.201', '1.1.1.1')
        # d2 = overlay.createDevice(session, 'd2', 'description for d2', 'spine', '192.168.48.202', '1.1.1.2', 'test', 'foobar')
        # d1_id = d1.id
        # d2_id = d2.id
        # f1 = overlay.createFabric(session, 'f1', '', 65001, '2.2.2.2', [d1, d2])
        # f1_id = f1.id
        # f2 = overlay.createFabric(session, 'f2', '', 65002, '3.3.3.3', [d1, d2])
        # f2_id = f2.id
        # t1 = overlay.createTenant(session, 't1', '', f1)
        # t1_id = t1.id
        # t2 = overlay.createTenant(session, 't2', '', f2)
        # t2_id = t2.id
        # v1 = overlay.createVrf(session, 'v1', '', 100, '1.1.1.1', t1)
        # v1_id = v1.id
        # v2 = overlay.createVrf(session, 'v2', '', 101, '1.1.1.2', t2)
        # v2_id = v2.id
        # n1 = overlay.createNetwork(session, 'n1', '', v1, 1000, 100, False)
        # n1_id = n1.id
        # n2 = overlay.createNetwork(session, 'n2', '', v1, 1001, 101, False)
        # n2_id = n2.id
        
        # statusList = []
        # object_url = '/openclos/v1/overlay/fabrics/' + f1_id
        # statusList.append(OverlayDeployStatus('f1config', object_url, 'POST', d1, None))
        # statusList.append(OverlayDeployStatus('f1config', object_url, 'POST', d2, None))
        # object_url = '/openclos/v1/overlay/vrfs/' + v1_id
        # statusList.append(OverlayDeployStatus('v1config', object_url, 'POST', d1, v1))
        # statusList.append(OverlayDeployStatus('v1config', object_url, 'POST', d2, v1))
        # object_url = '/openclos/v1/overlay/networks/' + n1_id
        # statusList.append(OverlayDeployStatus('n1config', object_url, 'POST', d1, v1))
        # statusList.append(OverlayDeployStatus('n1config', object_url, 'POST', d2, v1))
        # dao.createObjects(session, statusList)

    # commitQueue = OverlayCommitQueue.getInstance()
    # commitQueue.dispatchInterval = 1
    # commitQueue.start()
    
    # with dao.getReadWriteSession() as session:
        # status_db = session.query(OverlayDeployStatus).all()
        # for s in status_db:
            # commitQueue.addJob(s)
            
    # raw_input("Press any key to stop...")
    # commitQueue.stop()

# if __name__ == '__main__':
    # main()
