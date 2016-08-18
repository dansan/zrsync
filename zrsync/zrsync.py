#!/usr/bin/env python2.7

"""zrsync client: continuously synchronize a local directory to a [remote] directory.

Usage:
  zrsync [-ahilnrqstvp PORT] <server> <source-dir> <target-dir> [<source-dir> <target-dir>]...

Arguments:
  server      server to sync to: [user@]server[:port]
              ('user@' and ':port' automatically enable --ssh)
  source-dir  local directory to sync from
  target      directory on the server to sync to
              (not starting with '/' means relative to home of user running zrsyncd)

Options:
  -a --auto              enable -lrt (--install, --start and --shutdown, implies --ssh)
  -h --help              show this help message and exit
  -i --initial-only      quit after the initial sync
  -l --install           try to install zrsync on target (implies --ssh)
  -n --no-delete         do not delete any files or diretories on the target
  -p PORT --port=PORT    zrsyncd (not ssh) port to connect to [default: 24240]
  -r --start             start server (zrsyncd) on target if not already running (implies --ssh)
  -q --quiet             print only errors
  -s --ssh               tunnel connection through SSH.
  -t --shutdown          shutdown server when finished
  -v --verbose           print debug statements
  --version              show version and exit
"""

import logging
import re
import os
import pwd
import signal
import stat
import sys
from StringIO import StringIO
from multiprocessing import Process

import inotify.adapters
import inotify.constants
import zmq
import zmq.ssh
from docopt import docopt

from zrsyncd import ZRsyncBase, ZRequest, handle_oserror, setup_logging, MIN_BLOCK_DIFF_SIZE

FILE_IGNORE_REGEX = map(re.compile, [r"^$", r".swpx$", r".swp$", r"^\.svn$", r"^\.git.*", r"^\.", r"~$", r"^#.*#$",
    "\.dch$"])
HANDLE_EVENTS = ["IN_CLOSE_WRITE", "IN_ATTRIB", "IN_DELETE", "IN_CREATE", "IN_MOVED_FROM", "IN_MOVED_TO"]


class SyncClient(ZRsyncBase):
    def __init__(self, name, source_dir, server_addr, server_port, target_dir, ssh, no_delete, initial_only, log_level):
        super(SyncClient, self).__init__(name, server_addr, server_port, log_level)
        _loc = locals()
        del _loc["self"]
        self.logger.debug("Creating %s with %r.", self.__class__.__name__, _loc)
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.ssh = ssh
        self.no_delete = no_delete
        self.initial_only = initial_only
        self._error_tmp = dict()
        self._move_tmp = dict()
        self._previous_stat = dict()

    def do_connect(self):
        self._socket = self._context.socket(zmq.REQ)
        if self.ssh:
            socket_address = "tcp://127.0.0.1:{}".format(self.port)
            self.logger.info("Connecting to %r using SSH through %r...", socket_address, self.addr)
            zmq.ssh.tunnel_connection(self._socket, socket_address, self.addr)
        else:
            socket_address = "tcp://{}:{}".format(self.addr, self.port)
            self.logger.info("Connecting to %r...", socket_address)
            self._socket.connect(socket_address)

    def run(self):
        self.init()

        try:
            while True:
                self.initial_sync(self.source_dir, self.target_dir)
                if self.initial_only:
                    return

                self.watch_dir(self.source_dir)

                # restart, because of new non-empty directory
                self.logger.debug("Restarting initial_sync and watch_dir.")
        finally:
            self.cleanup()

    def initial_sync(self, src_dir, target_dir):
        """
        Request delta for directory src_dir. Replaces src_dir with target_dir
        in result.

        :param src_dir: str: path to the directory to sync on the client
        :param target_dir: str: path to the directory to sync on the server
        :return: None
        """
        if not os.path.isdir(src_dir) or os.path.islink(src_dir):
            self.logger.error("%r is not a directory, exiting.",  src_dir)
            sys.exit(1)
        self.logger.info("Starting initial sync from %r to %r.", src_dir, target_dir)
        tree = self.stat_dir(src_dir, target_dir)

        tree_s, total_file_size = self.dump_initial_tree(tree)
        self.logger.debug("Sending tree: \n%s", tree_s)
        self.logger.debug("Total file size: %d", total_file_size)

        request = ZRequest(cmd="sync_dir", name=target_dir, logger=self.logger, src_tree=tree, no_delete=self.no_delete)
        self.logger.info("Sending initial source tree...")
        meta_bytes, data_bytes = request.size
        self.send(request)

        reply = self.receive2()
        mb_, db_ = reply.size
        meta_bytes += mb_
        data_bytes += db_

        for msg in reply["tree_diff"]:
            cmd = msg["cmd"]
            remote_filename = msg["name"]
            local_filename = os.path.normpath(remote_filename.replace(target_dir, src_dir, 1))
            self.logger.info("%s: %s", cmd, local_filename)
            if cmd == "cmp":
                continue

            if msg.get("result", "") == "done":
                continue
            elif cmd == "diff":
                request = ZRequest(logger=self.logger, cmd="patch", name=remote_filename)
                data = ZRequest.decode_binary(msg["data"])
                signature = StringIO(data)
                request.add_diff(signature, local_filename).add_stat(local_filename).add_parent_time(local_filename).add_hash(local_filename)
            elif cmd == "full":
                request = ZRequest(logger=self.logger, cmd="full", name=remote_filename)
                request.add_full(local_filename).add_stat(local_filename).add_parent_time(local_filename).add_hash(local_filename)
            else:
                self.logger.error("Unknown 'cmd' in reply: %r.", cmd)
                sys.exit(1)

            mb_, db_ = request.size
            meta_bytes += mb_
            data_bytes += db_
            self.send(request)

            reply = self.receive2()
            # TODO: and now?

        self.logger.info("Initial sync done.")
        self.logger.info("Total data in source directory: %d bytes.", total_file_size)
        self.logger.info("Transfer used %d bytes for file data and %d bytes for meta data and protocol.", data_bytes,
                         meta_bytes)

    def watch_dir(self, base_dir):
        self.logger.info("Starting to watch %r.", base_dir)
        self.logger.debug("FILE_IGNORE_REGEX: %r", [regex.pattern for regex in FILE_IGNORE_REGEX])
        self.logger.debug("HANDLE_EVENTS: %r", HANDLE_EVENTS)
        mask = reduce(lambda x, y: x | y, [getattr(inotify.constants, ev) for ev in HANDLE_EVENTS], 0)
        for handler in self.logger.handlers:
            if not handler in inotify.adapters._LOGGER.handlers:
                inotify.adapters._LOGGER.addHandler(handler)
                inotify.adapters._LOGGER.setLevel(self.logger.level)
        notifyier = inotify.adapters.InotifyTree(base_dir, mask=mask)
        for event in notifyier.event_gen():
            if event is not None:
                header, type_names, watch_path, filename = event
                if not any([tn in HANDLE_EVENTS for tn in type_names]):
                    self.logger.debug("ignoring event type_names=%r", type_names)
                    continue
                if any([re.search(regex, filename) for regex in FILE_IGNORE_REGEX]):
                    self.logger.debug("ignoring filename=%r", filename)
                    continue
                self.logger.debug("WD=(%d) MASK=(%d) COOKIE=(%d) LEN=(%d) MASK->NAMES=%s WATCH-PATH=[%s] FILENAME=[%s]",
                                  header.wd, header.mask, header.cookie, header.len, type_names, watch_path, filename)
                res = self.handle_fs_event(type_names, watch_path, filename, header.cookie)
                if not res:
                    return

    def handle_fs_event(self, action, path, filename, cookie):
        """
        Handle a filesystem change event.

        :param action:
        :param path:
        :param filename:
        :param cookie:
        :return: bool: success? returns False to indicate an error
        """
        self.logger.debug("action=%r path=%r filename=%r cookie=%r", action, path, filename, cookie)
        local_filename = os.path.join(path, filename)
        remote_filename = local_filename.replace(self.source_dir, self.target_dir, 1)
        self.logger.debug("local_filename=%r remote_filename=%r", local_filename, remote_filename)
        request = ZRequest(cmd="none", logger=self.logger, name=remote_filename)

        if "IN_DELETE" not in action:
            request.add_stat(local_filename)

            if request.cmd in ["del", "ignore"]:
                if "IN_MOVED_FROM" in action:
                    request["cmd"] = "rm"
                    request["name"] = remote_filename
                else:
                    self.logger.debug("File %r vanished, ignoring.", local_filename)
                    return True

        if "IN_ISDIR" in action:
            if "IN_CREATE" in action:
                # This should be just a newly created, _empty_ directory, but
                # inotify fails to add watches for newly created directories
                # fast enough to catch for example 'mkdir -p'.
                # Lets see if this happend. We'd have to send a complete
                # tree_diff and reset the InotifyTree instance.
                if os.listdir(local_filename):
                    self.logger.warn("inotify failed to create watch and handle mkdir fast enough, restarting.")
                    return False
                else:
                    request["cmd"] = "mkdir"
            elif "IN_DELETE" in action:
                if self.no_delete:
                    self.logger.debug("Not deleting %r: no_delete is set.", local_filename)
                    return True
                else:
                    request["cmd"] = "rmdir"
        else:
            if "IN_CREATE" in action:
                self._previous_stat = request.copy()
                if request["st_size"] == 0:
                    request["cmd"] = "truncate"
                else:
                    request.add_full(local_filename).request.add_parent_time(local_filename).add_hash(local_filename)
            elif "IN_ATTRIB" in action:
                # prevent unnecessary request by detecting IN_ATTRIB following
                # IN_CREATE and IN_CLOSE_WRITE without stat change
                if request.has_same_stat(self._previous_stat):
                    return True
                else:
                    self.logger.debug("***** IN_ATTRIB in action: request.has_same_stat(self._previous_stat) = False")
                    self.logger.debug("***** request.to_dict()=%r", request.to_dict())
                    self.logger.debug("***** self._previous_stat=%r", self._previous_stat)
                    self._previous_stat = request.copy()
                    request["cmd"] = "touch"
            elif "IN_CLOSE_WRITE" in action:
                # prevent unnecessary request in case of an empty file
                if request.has_same_stat(self._previous_stat):
                    self.logger.debug("***** IN_CLOSE_WRITE in action:  request.has_same_stat(self._previous_stat) = True")
                    return True
                if request["st_size"] == 0:
                    if request.has_same_stat(self._previous_stat):
                        self.logger.debug("***** IN_CLOSE_WRITE in action: [st_size] = 0 AND request.has_same_stat(self._previous_stat) = True")
                        return True
                    else:
                        self.logger.debug("***** IN_CLOSE_WRITE in action: [st_size] = 0 AND request.has_same_stat(self._previous_stat) = False")
                        self.logger.debug("***** request.to_dict()=%r", request.to_dict())
                        self.logger.debug("***** self._previous_stat=%r", self._previous_stat)
                        self._previous_stat = request.copy()
                        request["cmd"] = "truncate"
                elif request["st_blocks"] < MIN_BLOCK_DIFF_SIZE:
                    request.add_full(local_filename).request.add_parent_time(local_filename).add_hash(local_filename)
                else:
                    request["cmd"] = "sig"
            elif "IN_DELETE" in action:
                if self.no_delete:
                    self.logger.debug("Not deleting %r: no_delete is set.", local_filename)
                    return True
                else:
                    request["cmd"] = "rm"
            elif "IN_MOVED_FROM" in action:
                if request.cmd == "rm":
                    # file was moved away, noticed by request.add_stat() above
                    pass
                else:
                    self._move_tmp[cookie] = remote_filename
                    return True
            elif "IN_MOVED_TO" in action:
                mv_src = self._move_tmp.pop("cookie", None)
                if mv_src:
                    request.update({
                        "cmd": "mv",
                        "from": mv_src
                    })
                else:
                    # moved in from outside of inotify-observed tree
                    request.add_full(local_filename).add_parent_time(local_filename).add_hash(local_filename)

        if request.cmd == "none":
            raise RuntimeError("request.cmd was not set with action='{}' path='{}' filename='{}' cookie='{}'.".format(action, path, filename, cookie))

        self.send(request)

        reply = self.receive2()

        if reply.get("result", "") == "done":
            self.logger.info("%s %s DONE", reply.cmd, reply.name)
            return True
        elif reply.cmd == "sig":
            # TODO: remove code duplication
            request = ZRequest(cmd="patch", logger=self.logger, name=remote_filename)
            request.add_stat(local_filename).add_parent_time(local_filename).add_hash(local_filename)
            self._previous_stat = request.copy()
            signature = StringIO(reply["data"])
            request.add_diff(signature, local_filename)

            # TODO: remove code duplication
            self.send(request)

            reply = self.receive2()

            if reply.get("result", "") == "done":
                self.logger.info("%s %s", reply.cmd, reply.name)
                return True
            elif "tree_diff" in reply:
                # {u'cmd': u'patch', u'result': u'done'}
                for msg in reply["tree_diff"]:
                    self.logger.info("%s %s", msg["cmd"], msg.get("result", "") or msg.get("name", "no result or name?"))
            else:
                self.logger.error("No result in reply: %r", ZRequest.shorten_data(reply))
        else:
            raise RuntimeError("Unknown 'cmd' in reply: %r".format(ZRequest.shorten_data(reply)))

        raise RuntimeError("We should not be here.")

    def stat_dir(self, dir_name, target_dir):
        """
        Create description of directory dir_name and its content. Replaces
        dir_name with target_dir in result.

        :param dir_name: str: path to analyse
        :param target_dir: str: replacement string
        :return: dict: structure describing a directory and its content
        """
        def handle_walk_error(exc):
            self.logger.exception("args=%r errno=%r filename=%r message=%r strerror=%r -> %r",
                exc.args, exc.errno, exc.filename, exc.message, exc.strerror, exc)
            self._error_tmp[exc.filename] = handle_oserror(exc)

        for dirpath, dirnames, filenames in os.walk(dir_name, onerror=handle_walk_error):
            res = dict(
                me=self.stat(dirpath),
                dirs=list(),
                files=list()
            )
            res["me"]["name"] = target_dir
            for dirname in dirnames:
                path = os.path.join(dirpath, dirname)
                target_path = os.path.join(target_dir, dirname)
                if not os.access(path, os.R_OK | os.X_OK):
                    self.logger.debug("Cannot read directory %r, ignoring.",  path)
                    a_dir_stat = dict(me=dict(cmd="ignore", name=target_path), dirs=list(), files=list())
                else:
                    a_dir_stat = self.stat_dir(path, target_path)
                    if not a_dir_stat:
                        a_dir_stat = dict(me=self._error_tmp[path], dirs=list(), files=list())
                    a_dir_stat["me"]["name"] = target_path
                res["dirs"].append(a_dir_stat)
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                target_path = os.path.join(target_dir, filename)
                if not os.access(path, os.R_OK):
                    self.logger.debug("Cannot read file %r, ignoring.",  path)
                    a_file_stat = dict(cmd="ignore", name=target_path)
                else:
                    a_file_stat = self.stat(path)
                    if not a_file_stat:
                        a_file_stat = self._error_tmp[path]
                    a_file_stat["name"] = target_path
                    if not any([
                        stat.S_ISDIR(a_file_stat["st_mode"]),
                        stat.S_ISLNK(a_file_stat["st_mode"]),
                        stat.S_ISREG(a_file_stat["st_mode"])]):
                            self.logger.debug("File type of %r not supported, ignoring.",  path)
                            a_file_stat = dict(cmd="ignore", name=target_path)
                res["files"].append(a_file_stat)
            return res

    @classmethod
    def dump_initial_tree(cls, tree, indent=0):
        """
        Print overview of the structure stat_dir() produces.

        :param tree: dict: stat_dir() output
        :param indent: int: space to add at start of lines
        :return: tuple: (str, int): ascii tree, file size
        """
        res = ""
        file_size = 0
        for k in ("me", "files", "dirs"):
            v = tree[k]
            if k == "me":
                res += "{} -> dir: {}\n".format(indent*" ", v.get("name", v))
                file_size += v["st_size"]
            elif k == "files":
                res += "    {}files: {}\n".format(indent*" ", [f.get("name", f) for f in v])
                file_size += reduce(lambda x, y: x + y, [x["st_size"] for x in v], 0)
            elif k == "dirs":
                res += "    {}dirs: {}\n".format(indent*" ", [d.get("me", {}).get("name", d) for d in v])
                for adir in v:
                    res_t, fsite_t = cls.dump_initial_tree(adir, indent + 4)
                    res += res_t
                    file_size += fsite_t
        return res, file_size


def validate_args(args):
    if args["--auto"]:
        args["--install"] = args["--start"] = args["--shutdown"] = True

    if any([args["--install"], args["--start"], "@" in args["<server>"], ":" in args["<server>"]]):
        args["--ssh"] = True

    if args["--verbose"]:
        args["log_level"] = logging.DEBUG
    elif args["--quiet"]:
        args["log_level"] = logging.ERROR
    else:
        args["log_level"] = logging.INFO

    try:
        args["--port"] = int(args["--port"])
    except ValueError:
        print("'port' argument must be a number.")
        sys.exit(1)

    if len(args["<source-dir>"]) != len(args["<target-dir>"]):
        print("There must be as many <source-dir> entries as <target-dir> entries.")
        sys.exit(1)

    for adir in args["<source-dir>"]:
        if not os.path.isdir(adir):
            print("<source-dir> must be a directory, '{}' is not.".format(adir))
            sys.exit(1)
    args["<source-dir>"] = [os.path.normpath(adir) for adir in args["<source-dir>"]]

    return args


def main(args):
    logger = setup_logging(args["log_level"], os.getpid())

    # TODO: support remote client installation

    kwargs = dict(
        server_addr=args["<server>"],
        server_port=args["--port"],
        ssh=args["--ssh"],
        no_delete=args["--no-delete"],
        initial_only=args["--initial-only"],
        log_level=args["log_level"]
    )
    jobs = zip(args["<source-dir>"], args["<target-dir>"])
    processes = list()
    for src, trg in jobs:
        p_name = "zrsync client {}".format(src)
        kwargs.update(dict(
            name=p_name,
            source_dir=src,
            target_dir=trg
        ))
        client = SyncClient(**kwargs)
        p = Process(target=client.run, args=(), name=p_name)
        p.start()
        processes.append(p)
        logger.info("Started sync client %d/%d (PID %d).", len(processes), len(jobs), p.pid)

    for count, p in enumerate(processes):
        try:
            p.join()
        except KeyboardInterrupt:
            logger.info("Sending SIGINT to all workers...")
            for pr in processes:
                os.kill(pr.pid, signal.SIGINT)
            p.join()
        logger.info("Worker %d/%d with PID %d and name %r ended.", count+1, len(processes), p.pid, p.name)

    if args["--shutdown"]:
        logger.info("Shutting down server...")
        client = SyncClient(**kwargs).init()
        client.send({"cmd": "shutdown"})
        client.receive()
        client.cleanup()
    return 0


if __name__ == '__main__':
    args = docopt(__doc__, version='ZDirSync 0.1')
    args = validate_args(args)
    sys.exit(main(args))
