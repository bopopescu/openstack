# Copyright (c) 2011 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
The FilterScheduler is for creating instances locally.
You can customize this scheduler by specifying your own Host Filters and
Weighing Functions.
"""

import random

from oslo_log import log as logging
from six.moves import range

import nova.conf
from nova import exception
from nova.i18n import _
from nova import rpc
from nova.scheduler import client
from nova.scheduler import driver

CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)


class FilterScheduler(driver.Scheduler):
    """Scheduler that can be used for filtering and weighing."""
    def __init__(self, *args, **kwargs):
        super(FilterScheduler, self).__init__(*args, **kwargs)
        self.notifier = rpc.get_notifier('scheduler')
        scheduler_client = client.SchedulerClient()
        self.placement_client = scheduler_client.reportclient

    def select_destinations(self, context, spec_obj, instance_uuids,
            alloc_reqs_by_rp_uuid, provider_summaries):
        """Returns a list of sorted lists of HostState objects (1 for each
        instance) that would satisfy the supplied request_spec. Each of those
        lists consist of [chosen_host, alternate1, ..., alternateN], where the
        'chosen_host' has already had its resources claimed in Placement,
        followed by zero or more alternates. The alternates are hosts that can
        satisfy the request, and are included so that if the build for the
        chosen host fails, the cell conductor can retry.

        :param context: The RequestContext object
        :param spec_obj: The RequestSpec object
        :param instance_uuids: List of UUIDs, one for each value of the spec
                               object's num_instances attribute
        :param alloc_reqs_by_rp_uuid: Optional dict, keyed by resource provider
                                      UUID, of the allocation_requests that may
                                      be used to claim resources against
                                      matched hosts. If None, indicates either
                                      the placement API wasn't reachable or
                                      that there were no allocation_requests
                                      returned by the placement API. If the
                                      latter, the provider_summaries will be an
                                      empty dict, not None.
        :param provider_summaries: Optional dict, keyed by resource provider
                                   UUID, of information that will be used by
                                   the filters/weighers in selecting matching
                                   hosts for a request. If None, indicates that
                                   the scheduler driver should grab all compute
                                   node information locally and that the
                                   Placement API is not used. If an empty dict,
                                   indicates the Placement API returned no
                                   potential matches for the requested
                                   resources.
        """
        self.notifier.info(
            context, 'scheduler.select_destinations.start',
            dict(request_spec=spec_obj.to_legacy_request_spec_dict()))

        # NOTE(sbauza): The RequestSpec.num_instances field contains the number
        # of instances created when the RequestSpec was used to first boot some
        # instances. This is incorrect when doing a move or resize operation,
        # so prefer the length of instance_uuids unless it is None.
        num_instances = (len(instance_uuids) if instance_uuids
                         else spec_obj.num_instances)
        selected_host_lists = self._schedule(context, spec_obj, instance_uuids,
            alloc_reqs_by_rp_uuid, provider_summaries)

        # Couldn't fulfill the request_spec
        if len(selected_host_lists) < num_instances:
            # NOTE(Rui Chen): If multiple creates failed, set the updated time
            # of selected HostState to None so that these HostStates are
            # refreshed according to database in next schedule, and release
            # the resource consumed by instance in the process of selecting
            # host.
            for host_list in selected_host_lists:
                host_list[0].updated = None

            # Log the details but don't put those into the reason since
            # we don't want to give away too much information about our
            # actual environment.
            LOG.debug('There are %(hosts)d hosts available but '
                      '%(num_instances)d instances requested to build.',
                      {'hosts': len(selected_host_lists),
                       'num_instances': num_instances})

            reason = _('There are not enough hosts available.')
            raise exception.NoValidHost(reason=reason)

        self.notifier.info(
            context, 'scheduler.select_destinations.end',
            dict(request_spec=spec_obj.to_legacy_request_spec_dict()))
        # NOTE(edleafe) - In this patch we only create the lists of [chosen,
        # alt1, alt2, etc.]. In a later patch we will change what we return, so
        # for this patch just return the selected hosts.
        selected_hosts = [sel_host[0] for sel_host in selected_host_lists]
        return selected_hosts

    def _schedule(self, context, spec_obj, instance_uuids,
            alloc_reqs_by_rp_uuid, provider_summaries):
        """Returns a list of hosts that meet the required specs, ordered by
        their fitness.

        These hosts will have already had their resources claimed in Placement.

        :param context: The RequestContext object
        :param spec_obj: The RequestSpec object
        :param instance_uuids: List of instance UUIDs to place or move.
        :param alloc_reqs_by_rp_uuid: Optional dict, keyed by resource provider
                                      UUID, of the allocation_requests that may
                                      be used to claim resources against
                                      matched hosts. If None, indicates either
                                      the placement API wasn't reachable or
                                      that there were no allocation_requests
                                      returned by the placement API. If the
                                      latter, the provider_summaries will be an
                                      empty dict, not None.
        :param provider_summaries: Optional dict, keyed by resource provider
                                   UUID, of information that will be used by
                                   the filters/weighers in selecting matching
                                   hosts for a request. If None, indicates that
                                   the scheduler driver should grab all compute
                                   node information locally and that the
                                   Placement API is not used. If an empty dict,
                                   indicates the Placement API returned no
                                   potential matches for the requested
                                   resources.
        """
        elevated = context.elevated()

        # Find our local list of acceptable hosts by repeatedly
        # filtering and weighing our options. Each time we choose a
        # host, we virtually consume resources on it so subsequent
        # selections can adjust accordingly.

        # Note: remember, we are using an iterator here. So only
        # traverse this list once. This can bite you if the hosts
        # are being scanned in a filter or weighing function.
        hosts = self._get_all_host_states(elevated, spec_obj,
            provider_summaries)

        # NOTE(sbauza): The RequestSpec.num_instances field contains the number
        # of instances created when the RequestSpec was used to first boot some
        # instances. This is incorrect when doing a move or resize operation,
        # so prefer the length of instance_uuids unless it is None.
        num_instances = (len(instance_uuids) if instance_uuids
                         else spec_obj.num_instances)

        # For each requested instance, we want to return a host whose resources
        # for the instance have been claimed, along with zero or more
        # alternates. These alternates will be passed to the cell that the
        # selected host is in, so that if for some reason the build fails, the
        # cell conductor can retry building the instance on one of these
        # alternates instead of having to simply fail. The number of alternates
        # is based on CONF.scheduler.max_attempts; note that if there are not
        # enough filtered hosts to provide the full number of alternates, the
        # list of hosts may be shorter than this amount.
        num_to_return = CONF.scheduler.max_attempts

        if (instance_uuids is None or
                not self.USES_ALLOCATION_CANDIDATES or
                alloc_reqs_by_rp_uuid is None):
            # We need to support the caching scheduler, which doesn't use the
            # placement API (and has USES_ALLOCATION_CANDIDATE = False) and
            # therefore we skip all the claiming logic for that scheduler
            # driver. Also, if there was a problem communicating with the
            # placement API, alloc_reqs_by_rp_uuid will be None, so we skip
            # claiming in that case as well. In the case where instance_uuids
            # is None, that indicates an older conductor, so we need to return
            # the older-style HostState objects without alternates.
            # NOTE(edleafe): moving this logic into a separate method, as this
            # method is already way too long. It will also make it easier to
            # clean up once we no longer have to worry about older conductors.
            include_alternates = (instance_uuids is not None)
            return self._legacy_find_hosts(num_instances, spec_obj, hosts,
                    num_to_return, include_alternates)

        # A list of the instance UUIDs that were successfully claimed against
        # in the placement API. If we are not able to successfully claim for
        # all involved instances, we use this list to remove those allocations
        # before returning
        claimed_instance_uuids = []

        # The list of hosts that have been selected (and claimed).
        claimed_hosts = []

        for num in range(num_instances):
            hosts = self._get_sorted_hosts(spec_obj, hosts, num)
            if not hosts:
                # NOTE(jaypipes): If we get here, that means not all instances
                # in instance_uuids were able to be matched to a selected host.
                # So, let's clean up any already-claimed allocations here
                # before breaking and returning
                self._cleanup_allocations(claimed_instance_uuids)
                break

            instance_uuid = instance_uuids[num]
            # Attempt to claim the resources against one or more resource
            # providers, looping over the sorted list of possible hosts
            # looking for an allocation_request that contains that host's
            # resource provider UUID
            claimed_host = None
            for host in hosts:
                cn_uuid = host.uuid
                if cn_uuid not in alloc_reqs_by_rp_uuid:
                    LOG.debug("Found host state %s that wasn't in "
                              "allocation_requests. Skipping.", cn_uuid)
                    continue

                alloc_reqs = alloc_reqs_by_rp_uuid[cn_uuid]
                if self._claim_resources(elevated, spec_obj, instance_uuid,
                        alloc_reqs):
                    claimed_host = host
                    break

            if claimed_host is None:
                # We weren't able to claim resources in the placement API
                # for any of the sorted hosts identified. So, clean up any
                # successfully-claimed resources for prior instances in
                # this request and return an empty list which will cause
                # select_destinations() to raise NoValidHost
                LOG.debug("Unable to successfully claim against any host.")
                self._cleanup_allocations(claimed_instance_uuids)
                return []

            claimed_instance_uuids.append(instance_uuid)
            claimed_hosts.append(claimed_host)

            # Now consume the resources so the filter/weights will change for
            # the next instance.
            self._consume_selected_host(claimed_host, spec_obj)

        # We have selected and claimed hosts for each instance. Now we need to
        # find alternates for each host.
        selections_to_return = self._get_alternate_hosts(
            claimed_hosts, spec_obj, hosts, num, num_to_return)
        return selections_to_return

    def _cleanup_allocations(self, instance_uuids):
        """Removes allocations for the supplied instance UUIDs."""
        if not instance_uuids:
            return
        LOG.debug("Cleaning up allocations for %s", instance_uuids)
        for uuid in instance_uuids:
            self.placement_client.delete_allocation_for_instance(uuid)

    def _claim_resources(self, ctx, spec_obj, instance_uuid, alloc_reqs):
        """Given an instance UUID (representing the consumer of resources), the
        HostState object for the host that was chosen for the instance, and a
        list of allocation_request JSON objects, attempt to claim resources for
        the instance in the placement API. Returns True if the claim process
        was successful, False otherwise.

        :param ctx: The RequestContext object
        :param spec_obj: The RequestSpec object
        :param instance_uuid: The UUID of the consuming instance
        :param cn_uuid: UUID of the host to allocate against
        :param alloc_reqs: A list of allocation_request JSON objects that
                           allocate against (at least) the compute host
                           selected by the _schedule() method. These
                           allocation_requests were constructed from a call to
                           the GET /allocation_candidates placement API call.
                           Each allocation_request satisfies the original
                           request for resources and can be supplied as-is
                           (along with the project and user ID to the placement
                           API's PUT /allocations/{consumer_uuid} call to claim
                           resources for the instance
        """
        LOG.debug("Attempting to claim resources in the placement API for "
                  "instance %s", instance_uuid)

        project_id = spec_obj.project_id

        # NOTE(jaypipes): So, the RequestSpec doesn't store the user_id,
        # only the project_id, so we need to grab the user information from
        # the context. Perhaps we should consider putting the user ID in
        # the spec object?
        user_id = ctx.user_id

        # TODO(jaypipes): Loop through all allocation_requests instead of just
        # trying the first one. For now, since we'll likely want to order the
        # allocation_requests in the future based on information in the
        # provider summaries, we'll just try to claim resources using the first
        # allocation_request
        alloc_req = alloc_reqs[0]

        return self.placement_client.claim_resources(instance_uuid,
            alloc_req, project_id, user_id)

    def _legacy_find_hosts(self, num_instances, spec_obj, hosts,
            num_to_return, include_alternates):
        """Some schedulers do not do claiming, or we can sometimes not be able
        to if the Placement service is not reachable. Additionally, we may be
        working with older conductors that don't pass in instance_uuids.
        """
        # The list of hosts selected for each instance
        selected_hosts = []
        # This the overall list of values to be returned. There will be one
        # item per instance, and when 'include_alternates' is True, that item
        # will be a list of HostState objects representing the selected host
        # along with alternates from the same cell. When 'include_alternates'
        # is False, the return value will be a list of HostState objects, with
        # one per requested instance.
        selections_to_return = []

        for num in range(num_instances):
            hosts = self._get_sorted_hosts(spec_obj, hosts, num)
            if not hosts:
                return []
            selected_host = hosts[0]
            selected_hosts.append(selected_host)
            self._consume_selected_host(selected_host, spec_obj)

        if include_alternates:
            selections_to_return = self._get_alternate_hosts(
                selected_hosts, spec_obj, hosts, num, num_to_return)
            return selections_to_return
        # No alternatives but we still need to return a list of lists of hosts
        return [[host] for host in selected_hosts]

    @staticmethod
    def _consume_selected_host(selected_host, spec_obj):
        LOG.debug("Selected host: %(host)s", {'host': selected_host})
        selected_host.consume_from_request(spec_obj)
        if spec_obj.instance_group is not None:
            spec_obj.instance_group.hosts.append(selected_host.host)
            # hosts has to be not part of the updates when saving
            spec_obj.instance_group.obj_reset_changes(['hosts'])

    def _get_alternate_hosts(self, selected_hosts, spec_obj, hosts, index,
                             num_to_return):
        # We only need to filter/weigh the hosts again if we're dealing with
        # more than one instance since the single selected host will get
        # filtered out of the list of alternates below.
        if index > 0:
            # The selected_hosts have all had resources 'claimed' via
            # _consume_selected_host, so we need to filter/weigh and sort the
            # hosts again to get an accurate count for alternates.
            hosts = self._get_sorted_hosts(spec_obj, hosts, index)
        # This is the overall list of values to be returned. There will be one
        # item per instance, and that item will be a list of HostState objects
        # representing the selected host along with alternates from the same
        # cell.
        selections_to_return = []
        for selected_host in selected_hosts:
            # This is the list of hosts for one particular instance.
            selected_plus_alts = [selected_host]
            cell_uuid = selected_host.cell_uuid
            # This will populate the alternates with many of the same unclaimed
            # hosts. This is OK, as it should be rare for a build to fail. And
            # if there are not enough hosts to fully populate the alternates,
            # it's fine to return fewer than we'd like. Note that we exclude
            # any claimed host from consideration as an alternate because it
            # will have had its resources reduced and will have a much lower
            # chance of being able to fit another instance on it.
            for host in hosts:
                if len(selected_plus_alts) >= num_to_return:
                    break
                if host.cell_uuid == cell_uuid and host not in selected_hosts:
                    selected_plus_alts.append(host)
            selections_to_return.append(selected_plus_alts)
        return selections_to_return

    def _get_sorted_hosts(self, spec_obj, host_states, index):
        """Returns a list of HostState objects that match the required
        scheduling constraints for the request spec object and have been sorted
        according to the weighers.
        """
        filtered_hosts = self.host_manager.get_filtered_hosts(host_states,
            spec_obj, index)

        LOG.debug("Filtered %(hosts)s", {'hosts': filtered_hosts})

        if not filtered_hosts:
            return []

        weighed_hosts = self.host_manager.get_weighed_hosts(filtered_hosts,
            spec_obj)
        # Strip off the WeighedHost wrapper class...
        weighed_hosts = [h.obj for h in weighed_hosts]

        LOG.debug("Weighed %(hosts)s", {'hosts': weighed_hosts})

        # We randomize the first element in the returned list to alleviate
        # congestion where the same host is consistently selected among
        # numerous potential hosts for similar request specs.
        host_subset_size = CONF.filter_scheduler.host_subset_size
        if host_subset_size < len(weighed_hosts):
            weighed_subset = weighed_hosts[0:host_subset_size]
        else:
            weighed_subset = weighed_hosts
        chosen_host = random.choice(weighed_subset)
        weighed_hosts.remove(chosen_host)
        return [chosen_host] + weighed_hosts

    def _get_all_host_states(self, context, spec_obj, provider_summaries):
        """Template method, so a subclass can implement caching."""
        # NOTE(jaypipes): provider_summaries being None is treated differently
        # from an empty dict. provider_summaries is None when we want to grab
        # all compute nodes, for instance when using the caching scheduler.
        # The provider_summaries variable will be an empty dict when the
        # Placement API found no providers that match the requested
        # constraints, which in turn makes compute_uuids an empty list and
        # get_host_states_by_uuids will return an empty tuple also, which will
        # eventually result in a NoValidHost error.
        compute_uuids = None
        if provider_summaries is not None:
            compute_uuids = list(provider_summaries.keys())
        return self.host_manager.get_host_states_by_uuids(context,
                                                          compute_uuids,
                                                          spec_obj)
