import sys

import warnings
import ansible.constants
import ansible.utils
import ansible.errors

from ansible.plugins.callback import CallbackBase
from ansible.executor.task_queue_manager import TaskQueueManager
from ansible.playbook.play import Play
from ansible.cli.adhoc import AdHocCLI
from pytest_ansible.module_dispatcher.v2 import ModuleDispatcherV2
from pytest_ansible.results import AdHocResult
from pytest_ansible.errors import AnsibleConnectionFailure
from pytest_ansible.has_version import has_ansible_v28

if not has_ansible_v28:
    raise ImportError("Only supported with ansible-2.8 and newer")
else:
    from ansible.plugins.loader import module_loader


class ResultAccumulator(CallbackBase):
    """Fixme."""

    def __init__(self, *args, **kwargs):
        """Initialize object."""
        super(ResultAccumulator, self).__init__(*args, **kwargs)
        self.contacted = {}
        self.unreachable = {}

    def v2_runner_on_failed(self, result, *args, **kwargs):
        result2 = dict(failed=True)
        result2.update(result._result)
        self.contacted[result._host.get_name()] = result2

    def v2_runner_on_ok(self, result):
        self.contacted[result._host.get_name()] = result._result

    def v2_runner_on_unreachable(self, result):
        self.unreachable[result._host.get_name()] = result._result

    @property
    def results(self):
        return dict(contacted=self.contacted, unreachable=self.unreachable)


class ModuleDispatcherV28(ModuleDispatcherV2):
    """Pass."""

    required_kwargs = ('inventory', 'inventory_manager', 'variable_manager', 'host_pattern', 'loader')

    def has_module(self, name):
        # Make sure we parse module_path and pass it to the loader,
        # otherwise, only built-in modules will work.
        if 'module_path' in self.options:
            paths = self.options['module_path']
            if isinstance(paths, (list, tuple, set)):
                for path in paths:
                    module_loader.add_directory(path)
            else:
                module_loader.add_directory(paths)

        return module_loader.has_plugin(name)

    def _run(self, *module_args, **complex_args):
        """Execute an ansible adhoc command returning the result in a AdhocResult object."""
        # Assemble module argument string
        if module_args:
            complex_args.update(dict(_raw_params=' '.join(module_args)))

        # Assert hosts matching the provided pattern exist
        hosts = self.options['inventory_manager'].list_hosts()
        no_hosts = False
        if len(hosts) == 0:
            no_hosts = True
            warnings.warn("provided hosts list is empty, only localhost is available")

        self.options['inventory_manager'].subset(self.options.get('subset'))
        hosts = self.options['inventory_manager'].list_hosts(self.options['host_pattern'])
        if len(hosts) == 0 and not no_hosts:
            raise ansible.errors.AnsibleError("Specified hosts and/or --limit does not match any hosts")

        # Pass along cli options
        args = ['pytest-ansible']
        verbosity = None
        for verbosity_syntax in ('-v', '-vv', '-vvv', '-vvvv', '-vvvvv'):
            if verbosity_syntax in sys.argv:
                verbosity = verbosity_syntax
                break
        if verbosity is not None:
            args.append(verbosity_syntax)
        args.extend([self.options['host_pattern']])
        for argument in ('connection', 'user', 'become', 'become_method', 'become_user', 'module_path'):
            arg_value = self.options.get(argument)
            argument = argument.replace('_', '-')

            if arg_value in (None, False):
                continue

            if arg_value is True:
                args.append('--{0}'.format(argument))
            else:
                args.append('--{0}={1}'.format(argument, arg_value))

        # Use Ansible's own adhoc cli to parse the fake command line we created and then save it
        # into Ansible's global context
        adhoc = AdHocCLI(args)
        adhoc.parse()

        # And now we'll never speak of this again
        del adhoc

        # Initialize callback to capture module JSON responses
        cb = ResultAccumulator()

        kwargs = dict(
            inventory=self.options['inventory_manager'],
            variable_manager=self.options['variable_manager'],
            loader=self.options['loader'],
            stdout_callback=cb,
            passwords=dict(conn_pass=None, become_pass=None),
        )

        # create a pseudo-play to execute the specified module via a single task
        play_ds = dict(
            name="pytest-ansible",
            hosts=self.options['host_pattern'],
            become=self.options.get('become'),
            become_user=self.options.get('become_user'),
            gather_facts='no',
            tasks=[
                dict(
                    action=dict(
                        module=self.options['module_name'], args=complex_args
                    ),
                ),
            ]
        )
        play = Play().load(play_ds, variable_manager=self.options['variable_manager'], loader=self.options['loader'])

        # now create a task queue manager to execute the play
        tqm = None
        try:
            tqm = TaskQueueManager(**kwargs)
            tqm.run(play)
        finally:
            if tqm:
                tqm.cleanup()

        # Raise exception if host(s) unreachable
        # FIXME - if multiple hosts were involved, should an exception be raised?
        if cb.unreachable:
            raise AnsibleConnectionFailure("Host unreachable", dark=cb.unreachable, contacted=cb.contacted)

        # Success!
        return AdHocResult(contacted=cb.contacted)
