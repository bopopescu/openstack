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

"""An object describing a tree of resource providers and their inventories.

This object is not stored in the Nova API or cell databases; rather, this
object is constructed and used by the scheduler report client to track state
changes for resources on the hypervisor or baremetal node. As such, there are
no remoteable methods nor is there any interaction with the nova.db modules.
"""

import copy

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_utils import uuidutils

from nova.i18n import _

LOG = logging.getLogger(__name__)
_LOCK_NAME = 'provider-tree-lock'


class _Provider(object):
    """Represents a resource provider in the tree. All operations against the
    tree should be done using the ProviderTree interface, since it controls
    thread-safety.
    """
    def __init__(self, name, uuid=None, generation=None, parent_uuid=None):
        if uuid is None:
            uuid = uuidutils.generate_uuid()
        self.uuid = uuid
        self.name = name
        self.generation = generation
        self.parent_uuid = parent_uuid
        # Contains a dict, keyed by uuid of child resource providers having
        # this provider as a parent
        self.children = {}
        # dict of inventory records, keyed by resource class
        self.inventory = {}

    def find(self, search):
        if self.name == search or self.uuid == search:
            return self
        if search in self.children:
            return self.children[search]
        if self.children:
            for child in self.children.values():
                # We already searched for the child by UUID above, so here we
                # just check for a child name match
                if child.name == search:
                    return child
                subchild = child.find(search)
                if subchild:
                    return subchild
        return None

    def add_child(self, provider):
        self.children[provider.uuid] = provider

    def remove_child(self, provider):
        if provider.uuid in self.children:
            del self.children[provider.uuid]

    def has_inventory(self):
        """Returns whether the provider has any inventory records at all. """
        return self.inventory != {}

    def has_inventory_changed(self, new):
        """Returns whether the inventory has changed for the provider."""
        cur = self.inventory
        if set(cur) != set(new):
            return True
        for key, cur_rec in cur.items():
            new_rec = new[key]
            for rec_key, cur_val in cur_rec.items():
                if rec_key not in new_rec:
                    # Deliberately don't want to compare missing keys in the
                    # inventory record. For instance, we will be passing in
                    # fields like allocation_ratio in the current dict but the
                    # resource tracker may only pass in the total field. We
                    # want to return that inventory didn't change when the
                    # total field values are the same even if the
                    # allocation_ratio field is missing from the new record.
                    continue
                if new_rec[rec_key] != cur_val:
                    return True
        return False

    def update_inventory(self, inventory, generation):
        """Update the stored inventory for the provider along with a resource
        provider generation to set the provider to. The method returns whether
        the inventory has changed.
        """
        if generation != self.generation:
            msg_args = {
                'rp_uuid': self.uuid,
                'old': self.generation,
                'new': generation,
            }
            LOG.debug("Updating resource provider %(rp_uuid)s generation "
                      "from %(old)s to %(new)s", msg_args)
            self.generation = generation
        if self.has_inventory_changed(inventory):
            self.inventory = copy.deepcopy(inventory)
            return True
        return False


class ProviderTree(object):

    def __init__(self, cns=None):
        """Create a provider tree from an `objects.ComputeNodeList` object."""
        self.lock = lockutils.internal_lock(_LOCK_NAME)
        self.roots = []

        if cns:
            for cn in cns:
                # By definition, all compute nodes are root providers...
                p = _Provider(cn.hypervisor_hostname, cn.uuid)
                self.roots.append(p)

    def remove(self, name_or_uuid):
        """Safely removes the provider identified by the supplied name_or_uuid
        parameter and all of its children from the tree.

        :raises ValueError if name_or_uuid points to a non-existing provider.
        :param name_or_uuid: Either name or UUID of the resource provider to
                             remove from the tree.
        """
        with self.lock:
            found = self._find_with_lock(name_or_uuid)
            if found.parent_uuid:
                parent = self._find_with_lock(found.parent_uuid)
                parent.remove_child(found)
            else:
                self.roots.remove(found)

    def new_root(self, name, uuid, generation):
        """Adds a new root provider to the tree."""

        with self.lock:
            exists = True
            try:
                self._find_with_lock(uuid)
            except ValueError:
                exists = False

            if exists:
                err = _("Provider %s already exists as a root.")
                raise ValueError(err % uuid)

            p = _Provider(name, uuid, generation)
            self.roots.append(p)
            return p

    def _find_with_lock(self, name_or_uuid):
        for root in self.roots:
            found = root.find(name_or_uuid)
            if found:
                return found
        raise ValueError(_("No such provider %s") % name_or_uuid)

    def find(self, name_or_uuid):
        """Search for a provider with the given name or UUID.

        :raises ValueError if name_or_uuid points to a non-existing provider.
        :param name_or_uuid: Either name or UUID of the resource provider to
                             search for.
        """
        with self.lock:
            return self._find_with_lock(name_or_uuid)

    def exists(self, name_or_uuid):
        """Given either a name or a UUID, return True if the tree contains the
        provider, False otherwise.
        """
        try:
            self.find(name_or_uuid)
            return True
        except ValueError:
            return False

    def new_child(self, name, parent_uuid, uuid=None, generation=None):
        """Creates a new child provider with the given name and uuid under the
        given parent.

        :returns: the new provider

        :raises ValueError if parent_uuid points to a non-existing provider.
        """
        with self.lock:
            parent = self._find_with_lock(parent_uuid)
            p = _Provider(name, uuid, generation, parent_uuid)
            parent.add_child(p)
            return p

    def has_inventory(self, name_or_uuid):
        """Returns True if the provider identified by name_or_uuid has any
        inventory records at all.

        :raises: ValueError if a provider with uuid was not found in the tree.
        :param name_or_uuid: Either name or UUID of the resource provider
        """
        with self.lock:
            p = self._find_with_lock(name_or_uuid)
            return p.has_inventory()

    def has_inventory_changed(self, name_or_uuid, inventory):
        """Returns True if the supplied inventory is different for the provider
        with the supplied name or UUID.

        :raises: ValueError if a provider with name_or_uuid was not found in
                 the tree.
        :param name_or_uuid: Either name or UUID of the resource provider to
                             update inventory for.
        :param inventory: dict, keyed by resource class, of inventory
                          information.
        """
        with self.lock:
            provider = self._find_with_lock(name_or_uuid)
            return provider.has_inventory_changed(inventory)

    def update_inventory(self, name_or_uuid, inventory, generation):
        """Given a name or UUID of a provider and a dict of inventory resource
        records, update the provider's inventory and set the provider's
        generation.

        :returns: True if the inventory has changed.

        :note: The provider's generation is always set to the supplied
               generation, even if there were no changes to the inventory.

        :raises: ValueError if a provider with name_or_uuid was not found in
                 the tree.
        :param name_or_uuid: Either name or UUID of the resource provider to
                             update inventory for.
        :param inventory: dict, keyed by resource class, of inventory
                          information.
        :param generation: The resource provider generation to set
        """
        with self.lock:
            provider = self._find_with_lock(name_or_uuid)
            return provider.update_inventory(inventory, generation)
