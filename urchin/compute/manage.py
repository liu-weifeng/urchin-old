import sys
import eventlet.event
import contextlib
from urchin.utils.log import log as logging
from urchin import exception
from urchin.virt import virtapi
from urchin import network
from urchin import image
from urchin import volume
from urchin.virt import driver
from urchin import utils
from urchin.compute import vm_states
from urchin.compute import power_state
from urchin.virt import event as virtevent


LOG = logging.getLogger(__name__)

class InstanceEvents(object):
    def __init__(self):
        self._events = {}

    @staticmethod
    def _lock_name(instance):
        return '%s-%s' % (instance.uuid, 'events')

    def prepare_for_instance_event(self, instance, event_name):
        """Prepare to receive an event for an instance.

        This will register an event for the given instance that we will
        wait on later. This should be called before initiating whatever
        action will trigger the event. The resulting eventlet.event.Event
        object should be wait()'d on to ensure completion.

        :param instance: the instance for which the event will be generated
        :param event_name: the name of the event we're expecting
        :returns: an event object that should be wait()'d on
        """
        if self._events is None:
            # NOTE(danms): We really should have a more specific error
            # here, but this is what we use for our default error case
            raise exception.NovaException('In shutdown, no new events '
                                          'can be scheduled')

        @utils.synchronized(self._lock_name(instance))
        def _create_or_get_event():
            instance_events = self._events.setdefault(instance.uuid, {})
            return instance_events.setdefault(event_name,
                                              eventlet.event.Event())
        LOG.debug('Preparing to wait for external event %(event)s',
                  {'event': event_name}, instance=instance)
        return _create_or_get_event()

    def pop_instance_event(self, instance, event):
        """Remove a pending event from the wait list.

        This will remove a pending event from the wait list so that it
        can be used to signal the waiters to wake up.

        :param instance: the instance for which the event was generated
        :param event: the nova.objects.external_event.InstanceExternalEvent
                      that describes the event
        :returns: the eventlet.event.Event object on which the waiters
                  are blocked
        """
        no_events_sentinel = object()
        no_matching_event_sentinel = object()

        @utils.synchronized(self._lock_name(instance))
        def _pop_event():
            if not self._events:
                LOG.debug('Unexpected attempt to pop events during shutdown',
                          instance=instance)
                return no_events_sentinel
            events = self._events.get(instance.uuid)
            if not events:
                return no_events_sentinel
            _event = events.pop(event.key, None)
            if not events:
                del self._events[instance.uuid]
            if _event is None:
                return no_matching_event_sentinel
            return _event

        result = _pop_event()
        if result is no_events_sentinel:
            LOG.debug('No waiting events found dispatching %(event)s',
                      {'event': event.key},
                      instance=instance)
            return None
        elif result is no_matching_event_sentinel:
            LOG.debug('No event matching %(event)s in %(events)s',
                      {'event': event.key,
                       'events': self._events.get(instance.uuid, {}).keys()},
                      instance=instance)
            return None
        else:
            return result

    def clear_events_for_instance(self, instance):
        """Remove all pending events for an instance.

        This will remove all events currently pending for an instance
        and return them (indexed by event name).

        :param instance: the instance for which events should be purged
        :returns: a dictionary of {event_name: eventlet.event.Event}
        """
        @utils.synchronized(self._lock_name(instance))
        def _clear_events():
            if self._events is None:
                LOG.debug('Unexpected attempt to clear events during shutdown',
                          instance=instance)
                return dict()
            return self._events.pop(instance.uuid, {})
        return _clear_events()

    def cancel_all_events(self):
        our_events = self._events
        # NOTE(danms): Block new events
        self._events = None

        for instance_uuid, events in our_events.items():
            for event_name, eventlet_event in events.items():
                LOG.debug('Canceling in-flight event %(event)s for '
                          'instance %(instance_uuid)s',
                          {'event': event_name,
                           'instance_uuid': instance_uuid})
                name, tag = event_name.rsplit('-', 1)
                event = objects.InstanceExternalEvent(
                    instance_uuid=instance_uuid,
                    name=name, status='failed',
                    tag=tag, data={})
                eventlet_event.send(event)


class ComputeVirtAPI(virtapi.VirtAPI):
    def __init__(self, compute):
        super(ComputeVirtAPI, self).__init__()
        self._compute = compute

    def provider_fw_rule_get_all(self, context):
        return self._compute.conductor_api.provider_fw_rule_get_all(context)

    def _default_error_callback(self, event_name, instance):
        raise exception.NovaException(_('Instance event failed'))

    @contextlib.contextmanager
    def wait_for_instance_event(self, instance, event_names, deadline=300,
                                error_callback=None):
        """Plan to wait for some events, run some code, then wait.

        This context manager will first create plans to wait for the
        provided event_names, yield, and then wait for all the scheduled
        events to complete.

        Note that this uses an eventlet.timeout.Timeout to bound the
        operation, so callers should be prepared to catch that
        failure and handle that situation appropriately.

        If the event is not received by the specified timeout deadline,
        eventlet.timeout.Timeout is raised.

        If the event is received but did not have a 'completed'
        status, a NovaException is raised.  If an error_callback is
        provided, instead of raising an exception as detailed above
        for the failure case, the callback will be called with the
        event_name and instance, and can return True to continue
        waiting for the rest of the events, False to stop processing,
        or raise an exception which will bubble up to the waiter.

        :param instance: The instance for which an event is expected
        :param event_names: A list of event names. Each element can be a
                            string event name or tuple of strings to
                            indicate (name, tag).
        :param deadline: Maximum number of seconds we should wait for all
                         of the specified events to arrive.
        :param error_callback: A function to be called if an event arrives

        """

        if error_callback is None:
            error_callback = self._default_error_callback
        events = {}
        for event_name in event_names:
            if isinstance(event_name, tuple):
                name, tag = event_name
                event_name = objects.InstanceExternalEvent.make_key(
                    name, tag)
            try:
                events[event_name] = (
                    self._compute.instance_events.prepare_for_instance_event(
                        instance, event_name))
            except exception.NovaException:
                error_callback(event_name, instance)
                # NOTE(danms): Don't wait for any of the events. They
                # should all be canceled and fired immediately below,
                # but don't stick around if not.
                deadline = 0
        yield
        with eventlet.timeout.Timeout(deadline):
            for event_name, event in events.items():
                actual_event = event.wait()
                if actual_event.status == 'completed':
                    continue
                decision = error_callback(event_name, instance)
                if decision is False:
                    break


class ComputeManager(object):
    """Manages the running instances from creation to destruction."""

    #target = messaging.Target(version='4.6')

    # How long to wait in seconds before re-issuing a shutdown
    # signal to an instance during power off.  The overall
    # time to wait is set by CONF.shutdown_timeout.
    SHUTDOWN_RETRY_INTERVAL = 10

    def __init__(self, compute_driver=None, *args, **kwargs):
        """Load configuration options and connect to the hypervisor."""
        self.virtapi = ComputeVirtAPI(self)
        self.network_api = network.API()
        self.volume_api = volume.API()
        self.image_api = image.API()


        # NOTE(russellb) Load the driver last.  It may call back into the
        # compute manager via the virtapi, so we want it to be fully
        # initialized before that happens.
        self.driver = driver.load_compute_driver(self.virtapi, compute_driver)
        self.use_legacy_block_device_info = \
                            self.driver.need_legacy_block_device_info

    def _instance_update(self, context, instance, **kwargs):
        """Update an instance in the database using kwargs as value."""

        for k, v in kwargs.items():
            setattr(instance, k, v)
        instance.save()
        self._update_resource_tracker(context, instance)

    def _nil_out_instance_obj_host_and_node(self, instance):
        # NOTE(jwcroppe): We don't do instance.save() here for performance
        # reasons; a call to this is expected to be immediately followed by
        # another call that does instance.save(), thus avoiding two writes
        # to the database layer.
        instance.host = None
        instance.node = None

    def _set_instance_obj_error_state(self, context, instance,
                                      clean_task_state=False):
        try:
            instance.vm_state = vm_states.ERROR
            if clean_task_state:
                instance.task_state = None
            instance.save()
        except exception.InstanceNotFound:
            LOG.debug('Instance has been destroyed from under us while '
                      'trying to set it to ERROR', instance=instance)

    def _get_instances_on_driver(self, context, filters=None):
        """Return a list of instance records for the instances found
        on the hypervisor which satisfy the specified filters. If filters=None
        return a list of instance records for all the instances found on the
        hypervisor.
        """
        if not filters:
            filters = {}
        try:
            driver_uuids = self.driver.list_instance_uuids()
            if len(driver_uuids) == 0:
                # Short circuit, don't waste a DB call
                return objects.InstanceList()
            filters['uuid'] = driver_uuids
            local_instances = objects.InstanceList.get_by_filters(
                context, filters, use_slave=True)
            return local_instances
        except NotImplementedError:
            pass

        # The driver doesn't support uuids listing, so we'll have
        # to brute force.
        driver_instances = self.driver.list_instances()
        instances = objects.InstanceList.get_by_filters(context, filters,
                                                        use_slave=True)
        name_map = {instance.name: instance for instance in instances}
        local_instances = []
        for driver_instance in driver_instances:
            instance = name_map.get(driver_instance)
            if not instance:
                continue
            local_instances.append(instance)
        return local_instances

    def _destroy_evacuated_instances(self, context):
        """Destroys evacuated instances.

        While nova-compute was down, the instances running on it could be
        evacuated to another host. Check that the instances reported
        by the driver are still associated with this host.  If they are
        not, destroy them, with the exception of instances which are in
        the MIGRATING, RESIZE_MIGRATING, RESIZE_MIGRATED, RESIZE_FINISH
        task state or RESIZED vm state.
        """

    def _is_instance_storage_shared(self, context, instance, host=None):
        shared_storage = True
        data = None
        try:
            data = self.driver.check_instance_shared_storage_local(context,
                                                       instance)
            if data:
                shared_storage = (self.compute_rpcapi.
                                  check_instance_shared_storage(context,
                                  instance, data, host=host))
        except NotImplementedError:
            LOG.debug('Hypervisor driver does not support '
                      'instance shared storage check, '
                      'assuming it\'s not on shared storage',
                      instance=instance)
            shared_storage = False
        except Exception:
            LOG.exception(_LE('Failed to check if instance shared'),
                      instance=instance)
        finally:
            if data:
                self.driver.check_instance_shared_storage_cleanup(context,
                                                                  data)
        return shared_storage

    def _complete_deletion(self, context, instance, bdms,
                           quotas, system_meta):
        if quotas:
            quotas.commit()

        # ensure block device mappings are not leaked
        for bdm in bdms:
            bdm.destroy()

        self._notify_about_instance_usage(context, instance, "delete.end",
                system_metadata=system_meta)

        self._clean_instance_console_tokens(context, instance)
        self._delete_scheduler_instance_info(context, instance.uuid)

    def _create_reservations(self, context, instance, project_id, user_id):
        """

        :param context:
        :param instance:
        :param project_id:
        :param user_id:
        :return:
        """

    def _init_instance(self, context, instance):
        '''Initialize this instance during service init.'''

        # NOTE(danms): If the instance appears to not be owned by this
        # host, it may have been evacuated away, but skipped by the
        # evacuation cleanup code due to configuration. Thus, if that
        # is a possibility, don't touch the instance in any way, but
        # log the concern. This will help avoid potential issues on
        # startup due to misconfiguration.


    def _retry_reboot(self, context, instance):
        """

        :param context:
        :param instance:
        :return:
        """

    def handle_lifecycle_event(self, event):
        """

        :param event:
        :return:
        """

    def handle_events(self, event):
        if isinstance(event, virtevent.LifecycleEvent):
            try:
                self.handle_lifecycle_event(event)
            except exception.InstanceNotFound:
                LOG.debug("Event %s arrived for non-existent instance. The "
                          "instance was probably deleted.", event)
        else:
            LOG.debug("Ignoring event %s", event)

    def init_virt_events(self):
        """

        :return:
        """

    def init_host(self):
        """Initialization for a standalone compute service."""

    def cleanup_host(self):
        self.driver.register_event_listener(None)
        self.instance_events.cancel_all_events()
        self.driver.cleanup_host(host=self.host)

    def pre_start_hook(self):
        """After the service is initialized, but before we fully bring
        the service up by listening on RPC queues, make sure to update
        our available resources (and indirectly our available nodes).
        """

    def _get_power_state(self, context, instance):
        """Retrieve the power state for the given instance."""
        LOG.debug('Checking state', instance=instance)
        try:
            return self.driver.get_info(instance).state
        except exception.InstanceNotFound:
            return power_state.NOSTATE

    """
    @periodic_task.periodic_task(spacing=CONF.scheduler_instance_sync_interval)
    def _sync_scheduler_instance_info(self, context):
        if not self.send_instance_updates:
            return
        context = context.elevated()
        instances = objects.InstanceList.get_by_host(context, self.host,
                                                     expected_attrs=[],
                                                     use_slave=True)
        uuids = [instance.uuid for instance in instances]
        self.scheduler_client.sync_instance_info(context, self.host, uuids)
    """

