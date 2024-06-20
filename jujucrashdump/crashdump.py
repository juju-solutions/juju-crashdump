#!/usr/bin/env python3

# you also might need to $ sudo apt install python-apport

import argparse
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import yaml
import concurrent.futures
import logging
import ssh_agent_setup

from collections import defaultdict
from os.path import expanduser

try:
    import apport
    import apport.crashdb
    import apport.hookutils

    APPORT = True
except ImportError:
    APPORT = False

from textwrap import dedent
from jujucrashdump.addons import ADDONS_FILE_PATH, do_addons, FNULL


MAX_FILE_SIZE = 5000000  # 5MB max for files
DIRECTORIES = [
    # '/var/lib/juju',  # Added below, if --small not passed.
    "/etc/alternatives",
    "/etc/aodh",
    "/etc/barbican",
    "/etc/ceilometer",
    "/etc/ceph",
    "/etc/cinder",
    "/etc/cloud",
    "/etc/corosync",
    "/etc/designate",
    "/etc/glance",
    "/etc/gnocchi",
    "/etc/heat",
    "/etc/ironic",
    "/etc/keystone",
    "/etc/magnum",
    "/etc/manila",
    "/etc/masakari",
    "/etc/netplan",
    "/etc/network",
    "/etc/neutron",
    "/etc/nova",
    "/etc/octavia",
    "/etc/placement",
    "/etc/quantum",
    "/etc/swift",
    "/etc/watcher",
    "/etc/udev/rules.d",
    "/lib/udev/rules.d",
    "/opt/nedge/var/log",
    "/run/cloud-init",
    "/usr/share/lxc/config",
    "/var/lib/charm",
    "/var/lib/libvirt/filesystems/plumgrid-data/log",
    "/var/lib/libvirt/filesystems/plumgrid/var/log",
    "/var/lib/cloud/seed",
    "/var/log",
    "/var/snap/simplestreams/common/sstream-mirror-glance.log",
    "/var/crash",
    "/var/snap/juju-db/common/logs/",
    "/var/lib/mysql/*-mysql-router",
    "/tmp/juju-exec*/script.sh",
    "/var/snap/lxd/common/lxd/logs/",
]

SSH_PARM = " -o StrictHostKeyChecking=no"

SSH_CMD = "timeout -v 5m ssh" + SSH_PARM
SCP_CMD = "timeout -v 5m scp" + SSH_PARM


def retrieve_single_unit_tarball(tuple_input):
    unique, machine, alias_group, all_machines, unit_dump_location = tuple_input
    unit_unique = uuid.uuid4()
    for ip in all_machines[machine]:
        if run_cmd(
            "{scp} {ip}:{dump_location}/{unique}/juju-dump-{unique}.tar"
            " {unit_unique}.tar".format(
                scp=SCP_CMD,
                ip=ip,
                dump_location=unit_dump_location,
                unique=unique,
                unit_unique=unit_unique,
            )
        ):
            break
    if "/" not in machine:
        machine += "/baremetal"
    run_cmd("mkdir -p %s || true" % machine)
    try:
        run_cmd("tar -pxf %s.tar -C %s" % (unit_unique, machine))
        run_cmd("rm %s.tar" % unit_unique)
    except IOError:
        # If you are running crashdump as a machine is coming
        # up, or scp fails for some other reason, you won't
        # have a tarball to move. In that case, skip, and try
        # fetching the tarball for the next machine.
        logging.warning("Unable to retrieve tarball for %s. Skipping." % machine)
    for alias in alias_group:
        os.symlink("%s" % machine, "%s" % alias.replace("/", "_"))


def service_unit_addresses(status):
    """From a given juju_status.yaml dict return a mapping of
    {'machine/container': ['<service1>', '<service2>', '<ip>']}."""
    out = defaultdict(set)
    ip_to_machine = dict()
    for m_id, m_info in status["machines"].items():
        if "dns-name" not in m_info:
            continue
        out[m_id].add(m_info["dns-name"])
        ip_to_machine[m_info["dns-name"]] = m_id
        for c_id, c_info in m_info.get("containers", {}).items():
            if "dns-name" not in c_info:
                continue
            out[c_id].add(c_info["dns-name"])
            ip_to_machine[c_info["dns-name"]] = c_id

    for _, a_info in status["applications"].items():
        if "subordinate-to" in a_info:
            continue
        for u_id, u_info in a_info.get("units", {}).items():
            if "public-address" not in u_info:
                continue
            machine = ip_to_machine[u_info["public-address"]]
            out[machine].add(u_id)
            if "subordinates" in u_info:
                for s_id, s_info in u_info["subordinates"].items():
                    if "public-address" not in s_info:
                        continue
                    machine = ip_to_machine[s_info["public-address"]]
                    out[machine].add(s_id)

    return out


def set_model(model):
    os.environ["JUJU_ENV"] = model
    os.environ["JUJU_MODEL"] = model


def run_cmd(command, fatal=False, to_file=None):
    logging.debug("Calling {}".format(command))
    try:
        output = subprocess.check_output(command, shell=True, stderr=FNULL)
        if to_file is not None:
            with open(to_file, "wb") as fd:
                fd.write(output)
    except subprocess.CalledProcessError as e:
        logging.warning('Command "%s" failed' % command)
        logging.warning(e)
        if fatal:
            sys.exit(1)
        return False
    logging.debug("Returned from {}".format(command))
    return True


def juju_cmd(command, *args, **kwargs):
    command_prefix = "juju "
    run_cmd(command_prefix + command, *args, **kwargs)


def juju_check():
    run_cmd("juju version", fatal=True)
    run_cmd("juju switch", fatal=True)


def juju_status():
    juju_cmd(" status --format=yaml", to_file="juju_status.yaml")
    juju_cmd(
        " status -m controller --format=yaml", to_file="juju_status_controller.yaml"
    )
    juju_cmd(
        " status --format=tabular --relations --storage", to_file="juju_status.txt"
    )


def juju_debuglog():
    juju_cmd("debug-log --date --replay --no-tail", to_file="debug_log.txt")


def juju_model_defaults():
    juju_cmd("model-config --format=yaml", to_file="model_config.yaml")


def juju_storage():
    juju_cmd("storage --format=yaml", to_file="storage.yaml")


def juju_storage_pools():
    juju_cmd("storage-pools --format=yaml", to_file="storage_pools.yaml")


def run_ssh(host, timeout, ssh_cmd, cmd):
    # Each host can have several interfaces and IP addresses.
    # This cycles through them and uses the first working.
    for ip in host:
        if run_cmd("timeout {}s {} {} '{}'".format(timeout, ssh_cmd, ip, cmd)):
            # If successful, no need to try the other hosts again
            host = [ip]
            break


class CrashCollector(object):
    """A log file collector for juju and charms"""

    def __init__(
        self,
        model,
        max_size,
        extra_dirs,
        output_dir=None,
        uniq=None,
        addons=None,
        addons_file=None,
        exclude=None,
        compression="xz",
        timeout=45,
        journalctl=None,
        unit_dump_location="/tmp",
        as_root=False,
    ):
        if model:
            set_model(model)
        self.max_size = max_size
        self.extra_dirs = extra_dirs
        self.cwd = os.getcwd()
        self.uniq = uniq or uuid.uuid4()
        self.tempdir = tempfile.mkdtemp(dir=expanduser("~"))
        self.tardir = os.path.join(self.tempdir, str(self.uniq))
        os.mkdir(self.tardir)
        os.chdir(self.tardir)
        self.output_dir = output_dir or "."
        self.addons = addons
        self.addons_file = addons_file
        if exclude is None:
            exclude = tuple()
        self.exclude = exclude
        self.compression = compression
        self.timeout = timeout
        self.journalctl = journalctl
        self.unit_dump_location = unit_dump_location
        self.as_root = as_root
        self._machines = None
        ssh_agent_setup.setup()
        ssh_agent_setup.add_key(
            os.path.join(os.path.expanduser("~"), ".local/share/juju/ssh/juju_id_rsa")
        )

    def get_all(self):
        if self._machines:
            return self._machines
        machines = {}
        juju_status = self.status
        for machine, machine_data in juju_status["machines"].items():
            try:
                machines[machine] = [
                    "ubuntu@{}".format(ip) for ip in machine_data["ip-addresses"]
                ]
            except KeyError:
                # A machine in allocating may not have an IP yet.
                continue
            if "containers" in juju_status["machines"][machine]:
                containers = juju_status["machines"][machine]["containers"]
                for container, container_data in containers.items():
                    try:
                        machines[container] = [
                            "ubuntu@{}".format(ip)
                            for ip in container_data["ip-addresses"]
                        ]
                    except KeyError:
                        # Sometimes containers don't have ip-addresses, for
                        # example, when they are pending and haven't been
                        # full brought up yet.
                        pass

        # _machines is now a list of partial ssh commands, for example:
        # ["ubuntu@x.x.x.x", "-J ubuntu@y.y.y.y ubuntu@x.x.x.x"]
        # the proxy jumps are through the controller machines since the machines
        # themselves may not be accessible directly
        self._machines = self._add_proxy_jumps(machines)
        return self._machines

    def _add_proxy_jumps(self, machines):
        controller_ips = []
        for info in self.controller_status.get("machines", {}).values():
            controller_ips.extend(info.get("ip-addresses", []))

        for machine, ips in machines.items():
            for ip in ips[:]:
                for controller_ip in controller_ips:
                    machines[machine].append(
                        "-J ubuntu@{} {}".format(controller_ip, ip)
                    )

        return machines

    def _run_all(self, cmd):
        all_machines = self.get_all()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            [
                executor.submit(run_ssh, ips, self.timeout, SSH_CMD, cmd)
                for _, ips in all_machines.items()
            ]

    def run_addons(self):
        services = service_unit_addresses(self.status)
        machines = services.keys()
        if not machines:
            return
        units = [v for v in list(set.union(*list(services.values()))) if "/" in v]
        if self.addons_file is not None and self.addons is not None:
            return do_addons(
                self.addons_file,
                self.addons,
                machines,
                units,
                self.unit_dump_location,
                self.uniq,
                self.as_root,
            )

    def run_journalctl(self):
        for service in self.journalctl or []:
            logdir = "{dump_location}/{uniq}/addon_output/journalctl".format(
                dump_location=self.unit_dump_location, uniq=self.uniq
            )
            logfile = "{logdir}/{service}.log".format(logdir=logdir, service=service)
            self._run_all(
                "mkdir -p {logdir};"
                "journalctl -u {service} > {logfile};"
                '[ "$(head -1 {logfile})" = "-- No entries --" ]'
                " && rm {logfile};"
                "true".format(logdir=logdir, logfile=logfile, service=service)
            )

    def create_unit_tarballs(self):
        directories = list(DIRECTORIES)
        directories.extend(self.extra_dirs)
        directories.extend(
            ["/var/lib/lxd/containers/*/rootfs" + item for item in directories]
        )
        directories.append(".")

        tar_cmd = (
            "mkdir -p {dump_location}/{uniq}/addon_output; "
            "cd {dump_location}/{uniq}/addon_output; "
            "{sudo}find {dirs} -mount -type f -size -{max_size}c -o -size "
            "{max_size}c 2>/dev/null | {sudo}tar -pcf ../juju-dump-{uniq}.tar"
            "{excludes}"
            " --files-from - 2>/dev/null"
        ).format(
            dirs=" ".join(directories),
            max_size=self.max_size,
            excludes="".join([" --exclude {}".format(x) for x in self.exclude]),
            uniq=self.uniq,
            sudo="sudo " if self.as_root else "",
            dump_location=self.unit_dump_location,
        )
        self._run_all(
            "mkdir -p {dump_location}/{uniq}".format(
                dump_location=self.unit_dump_location, uniq=self.uniq
            )
        )

        self._run_all(tar_cmd)

    @property
    def status(self):
        juju_status = yaml.load(open("juju_status.yaml", "r"), Loader=yaml.FullLoader)
        return juju_status

    @property
    def controller_status(self):
        juju_status = yaml.load(
            open("juju_status_controller.yaml", "r"), Loader=yaml.FullLoader
        )
        return juju_status

    def retrieve_unit_tarballs(self):
        all_machines = self.get_all()
        aliases = service_unit_addresses(self.status)
        if not aliases:
            # Running against an empty model.
            logging.warning("0 machines found. No tarballs to retrieve.")
            return
        pool = multiprocessing.Pool()
        pool.map(
            retrieve_single_unit_tarball,
            [
                (self.uniq, key, value, all_machines, self.unit_dump_location)
                for key, value in aliases.items()
            ],
        )

    def get_caas_stuff(self):
        juju_status = self.status
        if juju_status["model"]["type"] != "caas":
            return

        if "KUBECONFIG" not in os.environ:
            return

        try:
            subprocess.check_output(["which", "kubectl"])
        except subprocess.CalledProcessError:
            return

        run_cmd(
            "kubectl -n %s get pods" % (juju_status["model"]["name"]),
            to_file="pods.txt",
        )

    def collect(self):
        juju_check()
        juju_status()
        if "debug_log.txt" not in self.exclude:
            juju_debuglog()
        if "model_config.yaml" not in self.exclude:
            juju_model_defaults()
        if "storage.yaml" not in self.exclude:
            juju_storage()
        if "storage_pools.yaml" not in self.exclude:
            juju_storage_pools()
        self.get_caas_stuff()
        self.run_addons()
        self.run_journalctl()
        self.create_unit_tarballs()
        self.retrieve_unit_tarballs()
        os.chdir(self.tempdir)
        tar_file = "juju-crashdump-%s.tar.%s" % (self.uniq, self.compression)
        run_cmd("tar -pacf %s * 2>/dev/null" % tar_file)
        os.chdir(self.cwd)
        shutil.move(os.path.join(self.tempdir, tar_file), self.output_dir)
        self.cleanup()
        return tar_file

    def cleanup(self):
        shutil.rmtree(self.tempdir)


def upload_file_to_bug(bugnum, file_):
    if not APPORT:
        # We guard against this by checking for APPORT when the script
        # first runs (see bottom of this file). Just in case we get
        # here without apport, inform the user and skip this routine.
        logging.warning(
            "Apport not available in this environment. Skipping upload file to bug."
        )
        return
    crashdb = crashdb = apport.crashdb.get_crashdb(None)
    if not crashdb.can_update(bugnum):
        logging.warning(
            dedent(
                """
            You are not the reporter or subscriber of this problem report,
            or the report is a duplicate or already closed.

            Please create a new report on https://bugs.launchpad.net/charms.
            """
            )
        )
        return False

    is_reporter = crashdb.is_reporter(bugnum)

    report = apport.Report("Bug")
    apport.hookutils.attach_file(report, file_, overwrite=False)
    if len(report) != 0:
        logging.info("Starting upload to lp:%s" % bugnum)
        crashdb.update(
            bugnum,
            report,
            "apport information",
            change_description=is_reporter,
            attachment_comment="juju crashdump",
        )


class ShowDescription(argparse.Action):
    """Helper for implementing --description using argparse"""

    def __init__(self, *args, **kwargs):
        super(ShowDescription, self).__init__(*args, **kwargs)

    def __call__(self, parser, *args, **kwargs):
        print(CrashCollector.__doc__)
        sys.exit(0)


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "-d",
        "--description",
        nargs=0,
        action=ShowDescription,
        help="Output a short description of the plugin",
    )
    parser.add_argument("-m", "--model", default=None, help="Model to act on")
    parser.add_argument(
        "-f",
        "--max-file-size",
        default=MAX_FILE_SIZE,
        help="The max file size (bytes) for included files. " "(default: %(default)s)",
    )
    parser.add_argument(
        "-b",
        "--bug",
        default=None,
        help="Upload crashdump to the given launchpad bug #",
    )
    parser.add_argument(
        "extra_dir", nargs="*", default=[], help="Extra directories to snapshot"
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        help="Directories or files to exclude from capture.",
    )
    parser.add_argument(
        "-c",
        "--compression",
        default="xz",
        help="The compression type to use for result tarball. "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output-dir", help="Store the completed crash dump in this dir."
    )
    parser.add_argument(
        "-u",
        "--uniq",
        help="Unique id for this crashdump. "
        "We generate a uuid if this is not specified.",
    )
    parser.add_argument(
        "-s",
        "--small",
        action="store_true",
        help="Make a 'small' crashdump, by skipping the " "contents of /var/lib/juju.",
    )
    parser.add_argument(
        "-a",
        "--addon",
        action="append",
        help="Enable the addon with the given name.\n"
        "Buildin addons are: crm-status, juju-show-unit, juju-show-status-log, "
        "juju-show-machine, ps-mem, sosreport, config, engine-report",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default="45",
        help="Timeout in seconds for creating unit tarballs. " "(default: %(default)s)",
    )
    parser.add_argument(
        "--addons-file",
        action="append",
        help="Use this file for addon definitions. Addon files should be fomatted as:\n"
        "addon-name:\n # command to run locally (on the machine running juju crashdump"
        "),\n # all created files will be pushed to {location} on all units.\n local: "
        "echo 'example' > example.txt\n # command to run on every unit, all files "
        "created in {output} will be saved in the crashdump.\n remote: mv {location}/"
        "example.txt {output}/example.txt\n # local command to run for each {unit} or "
        "each {machine}. Std output will be saved.\n local-per-unit: echo 'example "
        "including {unit}'",
        default=[],
    )
    parser.add_argument(
        "-j",
        "--journalctl",
        action="append",
        help="Collect the journalctl logs for the systemd unit" " with the given name",
    )
    parser.add_argument(
        "-l",
        "--logging-level",
        type=str,
        default="info",
        help="logging level (default: %(default)s)",
    )
    parser.add_argument(
        "--unit-dump-location",
        type=str,
        default="/tmp",
        help="path to dump crashdump on units (default: %(default)s)",
    )
    parser.add_argument(
        "--as-root",
        action="store_true",
        help="Collect logs as root, may contain passwords etc. Addons with local "
        "commands will only run if this flag is enabled. (default: %(default)s)",
    )
    return parser.parse_args()


def main():
    opts = parse_args()
    numeric_level = getattr(logging, opts.logging_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % opts.logging_level)
    logging.basicConfig(format="%(asctime)s - %(message)s", level=numeric_level)
    logging.info("juju-crashdump started.")
    if opts.bug and not APPORT:
        logging.warning(
            "Apport not available in this environment.\n"
            + "You must 'apt install' apport to use the 'bug' option.\n"
            + "Aborting run."
        )
        return
    if not opts.small:
        DIRECTORIES.append("/var/lib/juju")
    if not opts.addons_file:
        opts.addons_file = []
    # We want to load the default addons first, and give the
    # option to overwrite them with newer addons if present.
    opts.addons_file.insert(0, ADDONS_FILE_PATH)
    if opts.as_root:
        opts.addon = (opts.addon if opts.addon else []) + ["listening", "psaux"]
        opts.addon = list(set(opts.addon))
    collector = CrashCollector(
        model=opts.model,
        max_size=opts.max_file_size,
        extra_dirs=opts.extra_dir,
        output_dir=opts.output_dir,
        uniq=opts.uniq,
        addons=opts.addon,
        addons_file=opts.addons_file,
        exclude=opts.exclude,
        compression=opts.compression,
        timeout=opts.timeout,
        journalctl=opts.journalctl,
        unit_dump_location=opts.unit_dump_location,
        as_root=opts.as_root,
    )
    filename = collector.collect()
    if opts.bug:
        upload_file_to_bug(opts.bug, filename)
    logging.info("juju-crashdump finished.")


if __name__ == "__main__":
    main()
