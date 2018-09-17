import json
import logging
import os

import diagnostics.config as config
import sdk_cmd
import sdk_diag
from sdk_utils import groupby

from diagnostics.bundle import Bundle
import diagnostics.agent as agent

log = logging.getLogger(__name__)


class ServiceBundle(Bundle):
    DOWNLOAD_FILES_WITH_PATTERNS = ["^stdout(\.\d+)?$", "^stderr(\.\d+)?$"]

    def __init__(self, package_name, service_name, scheduler_tasks, service, output_directory):
        self.package_name = package_name
        self.service_name = service_name
        self.scheduler_tasks = scheduler_tasks
        self.service = service
        self.framework_id = service.get("id")
        self.output_directory = output_directory

    @config.retry
    def install_cli(self):
        sdk_cmd.run_cli(
            "package install {} --cli --yes".format(self.package_name), print_output=False
        )

    def tasks(self):
        return self.service.get("tasks") + self.service.get("completed_tasks")

    def tasks_with_state(self, state):
        return list(filter(lambda task: task["state"] == state, self.tasks()))

    def running_tasks(self):
        return self.tasks_with_state("TASK_RUNNING")

    def run_on_tasks(self, fn, task_ids):
        for task_id in task_ids:
            fn(task_id)

    def for_each_running_task_with_prefix(self, prefix, fn):
        task_ids = [t["id"] for t in self.running_tasks() if t["name"].startswith(prefix)]
        self.run_on_tasks(fn, task_ids)

    @config.retry
    def create_configuration_file(self):
        rc, stdout, stderr = sdk_cmd.svc_cli(
            self.package_name, self.service_name, "describe", print_output=False
        )

        if rc != 0 or stderr:
            log.error(
                "Could not get service configuration\nstdout: '{}'\nstderr: '{}'", stdout, stderr
            )
        else:
            self.write_file("service_configuration.json", stdout)

    @config.retry
    def create_pod_status_file(self):
        rc, stdout, stderr = sdk_cmd.svc_cli(
            self.package_name, self.service_name, "pod status --json", print_output=False
        )

        if rc != 0 or stderr:
            log.error(
                "Could not get pod status\nstdout: '{}'\nstderr: '{}'", stdout, stderr
            )
        else:
            self.write_file("service_pod_status.json", stdout)

    @config.retry
    def create_plan_status_file(self, plan):
        rc, stdout, stderr = sdk_cmd.svc_cli(
            self.package_name,
            self.service_name,
            "plan status {} --json".format(plan),
            print_output=False,
        )

        if rc != 0 or stderr:
            log.error(
                "Could not get pod status\nstdout: '{}'\nstderr: '{}'", stdout, stderr
            )
        else:
            self.write_file("service_plan_status_{}.json".format(plan), stdout)

    @config.retry
    def create_plans_status_files(self):
        rc, stdout, stderr = sdk_cmd.svc_cli(
            self.package_name, self.service_name, "plan list", print_output=False
        )

        if rc != 0 or stderr:
            log.error(
                "Could not get plan list\nstdout: '{}'\nstderr: '{}'", stdout, stderr
            )
        else:
            plans = json.loads(stdout)
            for plan in plans:
                self.create_plan_status_file(plan)

    def download_log_files(self):
        all_tasks = self.scheduler_tasks + self.tasks()

        tasks_by_agent_id = dict(groupby("slave_id", all_tasks))

        agent_id_by_task_id = dict(map(lambda task: (task["id"], task["slave_id"]), all_tasks))

        agent_executor_paths = {}
        for agent_id in tasks_by_agent_id.keys():
            agent_executor_paths[agent_id] = agent.debug_agent_files(agent_id)

        task_executor_sandbox_paths = {}
        for agent_id, tasks in tasks_by_agent_id.items():
            for task in tasks:
                task_executor_sandbox_paths[task["id"]] = sdk_diag._find_matching_executor_path(
                    agent_executor_paths[agent_id], sdk_diag._TaskEntry(task)
                )

        for task_id, task_executor_sandbox_path in task_executor_sandbox_paths.items():
            agent_id = agent_id_by_task_id[task_id]

            if task_executor_sandbox_path:
                agent.download_task_files(
                    agent_id,
                    task_executor_sandbox_path,
                    task_id,
                    os.path.join(self.output_directory, "tasks"),
                    self.DOWNLOAD_FILES_WITH_PATTERNS,
                )
            else:
                log.warn(
                    "Could not find executor sandbox path in agent '%s' for task '%s'",
                    agent_id,
                    task_id,
                )

    def create(self):
        self.install_cli()
        self.create_configuration_file()
        self.create_pod_status_file()
        self.create_plans_status_files()
        self.download_log_files()
